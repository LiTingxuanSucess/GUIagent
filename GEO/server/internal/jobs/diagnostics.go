package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"geo-server/internal/procguard"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type scrapeDiagnostic struct {
	ID, Dir, Site, JobID string
	Started              time.Time
	PID                  int
	ExitCode             *int
	MemoryBefore         *procguard.MemoryEvidence
}

func diagnosticTimeoutCode(d *scrapeDiagnostic) string {
	if d != nil {
		switch d.phase() {
		case "process_start", "login_restore", "browser_connect", "page_setup":
			return "session_setup_timeout"
		case "01_send":
			return "send_timeout"
		case "02_wait":
			return "generation_timeout"
		case "03_dom_extract":
			return "answer_extract_timeout"
		case "04_share_create", "05_share_verify":
			return "share_timeout"
		}
	}
	return "site_timeout"
}

func (d *scrapeDiagnostic) phase() string {
	raw, err := os.ReadFile(filepath.Join(d.Dir, "manifest.json"))
	if err != nil {
		return ""
	}
	var state struct {
		Phase string `json:"phase"`
	}
	if json.Unmarshal(raw, &state) != nil {
		return ""
	}
	return state.Phase
}

// Healthy search/generation keeps saving page checkpoints even when answer
// text has not grown. Only a stale, previously available page in an active
// input/wait phase indicates a stuck browser; startup and optional share
// work are not evidence that a generation should be killed.
func (d *scrapeDiagnostic) pageStalled(now time.Time) bool {
	seconds := envInt("GEO_PAGE_STALL_SEC", 90, 0, 120)
	if seconds == 0 {
		return false
	}
	raw, err := os.ReadFile(filepath.Join(d.Dir, "manifest.json"))
	if err != nil {
		return false
	}
	var state struct {
		BrowserAvailable bool   `json:"browser_available"`
		CheckpointAt     string `json:"checkpoint_at"`
		Phase            string `json:"phase"`
		Status           string `json:"status"`
		Outcome          string `json:"outcome"`
	}
	if json.Unmarshal(raw, &state) != nil || !state.BrowserAvailable || state.Outcome != "" || (state.Status != "" && state.Status != "running") {
		return false
	}
	if state.Phase != "01_send" && state.Phase != "02_wait" {
		return false
	}
	at, err := time.Parse(time.RFC3339Nano, state.CheckpointAt)
	return err == nil && !at.Before(d.Started) && now.Sub(at) >= time.Duration(seconds)*time.Second
}

func (q *Queue) diagnosticRoot() string {
	if value := strings.TrimSpace(os.Getenv("GEO_DIAGNOSTIC_DIR")); value != "" {
		return value
	}
	if q.DataDir == "" {
		return ""
	}
	if filepath.Base(q.DataDir) == "jobs" {
		return filepath.Join(filepath.Dir(q.DataDir), "diagnostics")
	}
	return filepath.Join(q.DataDir, "diagnostics")
}

func (q *Queue) beginDiagnostic(site, jobID string) *scrapeDiagnostic {
	root := q.diagnosticRoot()
	if root == "" || envInt("GEO_DIAGNOSTICS_ENABLED", 1, 0, 1) == 0 {
		return nil
	}
	safe := func(s string) string {
		return strings.Map(func(r rune) rune {
			if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' {
				return r
			}
			return '_'
		}, s)
	}
	d := &scrapeDiagnostic{ID: fmt.Sprintf("%s-%s-%d", safe(jobID), safe(site), time.Now().UnixNano()), Site: site, JobID: jobID, Started: time.Now(), MemoryBefore: procguard.BrowserMemoryEvidence()}
	d.Dir = filepath.Join(root, d.ID)
	if err := os.MkdirAll(d.Dir, 0o700); err != nil {
		return nil
	}
	_ = os.Chmod(root, 0o700)
	_ = os.Chmod(d.Dir, 0o700)
	return d
}

func diagnosticWrite(dir, name string, value any) {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return
	}
	path := filepath.Join(dir, name)
	if os.WriteFile(path+".go-tmp", raw, 0o600) == nil {
		_ = os.Rename(path+".go-tmp", path)
	}
}

func (d *scrapeDiagnostic) finish(ctx context.Context, result map[string]any, err error) {
	if d == nil {
		return
	}
	if _, statErr := os.Stat(d.Dir); statErr != nil {
		return // Python removes successful, unsampled evidence.
	}
	status, code := "succeeded", ""
	if err != nil {
		status = "failed"
		code, _, _ = publicScrapeError(err)
	}
	if ctx.Err() != nil {
		status, code = "terminated", "site_timeout"
		if err != nil {
			code, _, _ = publicScrapeError(err)
		}
		if result != nil && result["ok"] == true && result["web_answer_recovered"] == true {
			status, code = "partial", "share_timeout"
		}
	}
	// Do not retain command arguments, raw stdout/stderr, or signed CDP URLs.
	memoryAfter := procguard.BrowserMemoryEvidence()
	eventDeltas := map[string]uint64{}
	if memoryAfter != nil && d.MemoryBefore != nil {
		for key, value := range memoryAfter.Events {
			if before, ok := d.MemoryBefore.Events[key]; ok && value >= before {
				eventDeltas[key] = value - before
			}
		}
	}
	diagnosticWrite(d.Dir, "process.json", map[string]any{
		"diagnostic_id": d.ID, "job_id": d.JobID, "site_id": d.Site,
		"started_at": d.Started.Format(time.RFC3339Nano), "finished_at": time.Now().Format(time.RFC3339Nano),
		"status": status, "error_code": code, "elapsed_ms": time.Since(d.Started).Milliseconds(),
		"failure_stage": d.phase(), "failure_reason": scrapeFailureReason(err), "process_pid": d.PID, "process_exit_code": d.ExitCode,
		"browser_memory_before": d.MemoryBefore, "browser_memory_after": memoryAfter, "shared_memory_event_deltas": eventDeltas,
		"memory_evidence_note": "Shared browser cgroup counters are correlation evidence, not proof that this task caused an OOM.",
		"evidence_note":        "DOM and network evidence, if present, are the last Python checkpoint. Startup/termination may have no page evidence.",
	})
	_ = os.Remove(filepath.Join(d.Dir, ".active"))
	log.Printf("scrape diagnostic job=%s site=%s diagnostic_id=%s status=%s error_code=%s elapsed_ms=%d", d.JobID, d.Site, d.ID, status, code, time.Since(d.Started).Milliseconds())
}

func scrapeFailureReason(err error) string {
	if err == nil {
		return ""
	}
	text := strings.ToLower(err.Error())
	switch {
	case strings.Contains(text, "page crashed"), strings.Contains(text, "target crashed"):
		return "browser_page_crashed"
	case strings.Contains(text, "发送点击结果未知"):
		return "send_click_unconfirmed"
	case strings.Contains(text, "为空"), strings.Contains(text, "empty answer"):
		return "answer_empty"
	default:
		code, _, _ := publicScrapeError(err)
		return code
	}
}

func (q *Queue) attachDiagnostic(result map[string]any, site, jobID string) {
	if id, ok := q.diagnosticIDs.LoadAndDelete(jobID + "/" + site); ok && result != nil {
		if _, err := os.Stat(filepath.Join(q.diagnosticRoot(), id.(string))); err == nil {
			result["diagnostic_id"] = id
		}
	}
}

func (q *Queue) recordAPIDiagnostic(failure map[string]any, started time.Time, err error) {
	id := stringField(failure, "diagnostic_id")
	if id == "" || filepath.Base(id) != id || q.diagnosticRoot() == "" {
		return
	}
	status := "succeeded"
	if err != nil {
		status = "failed"
	}
	diagnosticWrite(filepath.Join(q.diagnosticRoot(), id), "api-fallback.json", map[string]any{
		"phase": "official_api_fallback", "status": status,
		"started_at": started.Format(time.RFC3339Nano), "finished_at": time.Now().Format(time.RFC3339Nano),
		"elapsed_ms": time.Since(started).Milliseconds(),
	})
}
