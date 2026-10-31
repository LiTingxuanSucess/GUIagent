package jobs

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"geo-server/internal/sessions"
)

func TestManualBusyRetriesRefreshAndPreservesSameTask(t *testing.T) {
	t.Setenv("GEO_MANUAL_AUTO_RECOVERY", "1")
	q := attemptFixtureQueue(t, `import json,sys,pathlib
args=sys.argv
action=args[args.index('--recovery-action')+1]
with pathlib.Path('manual-events').open('a',encoding='utf-8') as f:f.write(action+'\n')
if action=='manual-refresh':
 print(json.dumps({'ok':True,'answer':'original question answer','answer_origin':'web'}))
else:
 print(json.dumps({'ok':False,'error':'busy','error_code':'platform_busy','retryable':True}))
`)
	job := &Job{ID: "busy-job", Prompt: "original question", Sites: []string{"kimi"}, Status: "running", ManualWorkflow: true,
		ManualStates: map[string]ManualState{"kimi": {Site: "kimi", Status: "automatic"}}}
	q.jobs = map[string]*Job{job.ID: job}
	result, err := q.manualScrape(context.Background(), webRecoveryExecution{Action: "manual-auto", StatePath: filepath.Join(q.GeoRoot, "journal.json"), AccountID: "fixture"}, "http://127.0.0.1:12345", "kimi", job, &sessions.Lease{Phone: "13800138000"})
	if err != nil || result["ok"] != true {
		t.Fatalf("refresh did not recover: %v %v", result, err)
	}
	raw, err := os.ReadFile(filepath.Join(q.GeoRoot, "manual-events"))
	if err != nil || strings.TrimSpace(string(raw)) != "manual-auto\nmanual-refresh" {
		t.Fatalf("unexpected sends: %s %v", raw, err)
	}
	current, _ := q.Get(job.ID)
	if current.ManualStates["kimi"].RefreshAttempts != 1 {
		t.Fatal("missing retry audit")
	}
}
