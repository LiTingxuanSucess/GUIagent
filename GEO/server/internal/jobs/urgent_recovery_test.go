package jobs

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCompletedAnswerSurvivesShareStageTimeout(t *testing.T) {
	root := t.TempDir()
	script := filepath.Join(root, "fixture.py")
	body := `import json,time
print(json.dumps({'web_answer_completed':True,'ok':True,'site_id':'yuanbao','prompt':'current question','question_id':'job_current','answer':'completed answer','answer_origin':'web','source':'api:conversation_detail','citations':['https://example.org/real'],'quality_issues':['share_generation_failed']}),flush=True)
time.sleep(30)
`
	if err := os.WriteFile(script, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	q := &Queue{GeoRoot: root, Python: "python3", Script: script, DataDir: filepath.Join(root, "jobs")}
	got, err := q.scrapeForContext(context.Background(), "http://127.0.0.1:9", "yuanbao", "current question", "current", "", 300*time.Millisecond)
	if err != nil || got["ok"] != true || got["answer"] != "completed answer" || got["web_answer_recovered"] != true || got["url"] != "" || got["share_error_code"] != "share_timeout" {
		t.Fatalf("completed answer was lost: %v %v", got, err)
	}
	refs := got["citations"].([]any)
	if len(refs) != 1 || refs[0] != "https://example.org/real" {
		t.Fatal("real sources were lost")
	}
	id, _ := q.diagnosticIDs.Load("current/yuanbao")
	raw, err := os.ReadFile(filepath.Join(q.diagnosticRoot(), id.(string), "process.json"))
	if err != nil || !strings.Contains(string(raw), `"status": "partial"`) {
		t.Fatalf("share timeout misreported as answer failure: %s %v", raw, err)
	}
}

func TestCompletedCheckpointMustMatchExactRequest(t *testing.T) {
	good := map[string]any{"web_answer_completed": true, "ok": true, "site_id": "yiyan", "prompt": "current question", "question_id": "job_current", "answer": "completed answer", "answer_origin": "web", "source": "dom:yiyan-task"}
	for _, key := range []string{"site_id", "prompt", "question_id", "answer_origin", "web_answer_completed", "ok", "answer", "source"} {
		t.Run(key, func(t *testing.T) {
			bad := map[string]any{}
			for k, v := range good {
				bad[k] = v
			}
			switch key {
			case "web_answer_completed", "ok":
				bad[key] = false
			case "answer":
				bad[key] = ""
			case "source":
				bad[key] = "official_api"
			default:
				bad[key] = "wrong"
			}
			raw, _ := json.Marshal(bad)
			if completedWebCheckpoint(raw, "yiyan", "current question", "current") != nil {
				t.Fatal("unrelated or incomplete answer accepted")
			}
		})
	}
}

func TestTransientCooldownProbesAndClearsOnWebRecovery(t *testing.T) {
	t.Setenv("GEO_SERVER_ROOT", t.TempDir())
	t.Setenv("GEO_YUANBAO_API_KEY", "fixture")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "1")
	t.Setenv("GEO_TRANSIENT_COOLDOWN_THRESHOLD", "3")
	t.Setenv("GEO_TRANSIENT_COOLDOWN_SEC", "120")
	q := &Queue{DataDir: t.TempDir()}
	for i := 0; i < 3; i++ {
		failure := siteError("yuanbao", "question", "platform_busy", "still generating", false, 100)
		q.recordSiteCooldown("yuanbao", failure)
		skipped := q.siteCooldown("yuanbao", "yuanbao", "question")
		if (skipped != nil) != (i == 2) {
			t.Fatalf("cooldown at wrong threshold: %d %v", i, skipped)
		}
	}
	raw, _ := os.ReadFile(q.cooldownPath("yuanbao"))
	var state siteCooldownState
	_ = json.Unmarshal(raw, &state)
	skipped := q.siteCooldown("yuanbao", "yuanbao", "question")
	q.recordSiteCooldown("yuanbao", skipped)
	after, _ := os.ReadFile(q.cooldownPath("yuanbao"))
	if string(after) != string(raw) {
		t.Fatal("skipped jobs extended the cooldown")
	}
	if q.siteCooldown("doubao", "doubao", "question") != nil {
		t.Fatal("unrelated site cooled down")
	}
	state.Until = time.Now().Add(-time.Second)
	expired, _ := json.Marshal(state)
	_ = os.WriteFile(q.cooldownPath("yuanbao"), expired, 0600)
	if q.siteCooldown("yuanbao", "yuanbao", "question") != nil {
		t.Fatal("recovery probe blocked after expiry")
	}
	q.recordSiteCooldown("yuanbao", map[string]any{"ok": true})
	if _, err := os.Stat(q.cooldownPath("yuanbao")); !os.IsNotExist(err) {
		t.Fatal("success did not reset the failed browser streak")
	}
	// Admission contention must not count as a failed provider interaction.
	for i := 0; i < 3; i++ {
		q.recordSiteCooldown("yuanbao", map[string]any{"ok": false, "error_code": "platform_busy", "web_skipped": true})
	}
	if _, err := os.Stat(q.cooldownPath("yuanbao")); !os.IsNotExist(err) {
		t.Fatal("admission contention created a provider cooldown")
	}
}

func TestThinkingBudgetAndWebWindowForUrgentProviders(t *testing.T) {
	t.Setenv("GEO_SERVER_ROOT", t.TempDir())
	t.Setenv("GEO_YIYAN_API_KEY", "fixture")
	t.Setenv("GEO_YIYAN_API_TIMEOUT_SEC", "180")
	t.Setenv("GEO_YIYAN_SINGLE_TOTAL_TIMEOUT_SEC", "300")
	t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "120")
	start := time.Now()
	ctx, cancel := context.WithDeadline(context.Background(), start.Add(requestBudgetForSites([]string{"wenxinyiyan"})))
	defer cancel()
	deadline := requestWebDeadline(ctx, "yiyan", start.Add(240*time.Second), start, 1)
	if deadline.Sub(start) != 120*time.Second || apiReserve("yiyan") != 180*time.Second {
		t.Fatal("deep thinking no longer has a full API window")
	}
}
