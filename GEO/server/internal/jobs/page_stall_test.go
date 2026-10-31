package jobs

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestStuckPageProcessStopsBeforeRequestDeadline(t *testing.T) {
	t.Setenv("GEO_PAGE_STALL_SEC", "1")
	root := t.TempDir()
	script := filepath.Join(root, "stalled.py")
	body := `import datetime,json,os,time
from pathlib import Path
path=Path(os.environ['GEO_DIAGNOSTIC_RUN_DIR'])/'manifest.json'
path.write_text(json.dumps({'browser_available':True,'phase':'02_wait','status':'running','checkpoint_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}),encoding='utf-8')
time.sleep(30)
`
	if err := os.WriteFile(script, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	q := &Queue{GeoRoot: root, Python: "python3", Script: script, DataDir: filepath.Join(root, "jobs")}
	started := time.Now()
	_, err := q.scrapeForContext(context.Background(), "http://127.0.0.1:9", "doubao", "current question", "stalled", "", 20*time.Second)
	code, _, retryable := publicScrapeError(err)
	if code != "browser_unresponsive" || !retryable || time.Since(started) > 10*time.Second {
		t.Fatalf("stalled page consumed request deadline: code=%s retryable=%t elapsed=%s", code, retryable, time.Since(started))
	}
}

func TestPageStallDistinguishesFrozenBrowserFromSlowGeneration(t *testing.T) {
	t.Setenv("GEO_PAGE_STALL_SEC", "45")
	now := time.Now()
	for _, test := range []struct {
		name, phase string
		age         time.Duration
		available   bool
		status      string
		want        bool
	}{
		{"stale_wait", "02_wait", time.Minute, true, "running", true},
		{"stale_send", "01_send", time.Minute, true, "running", true},
		{"healthy_slow_generation", "02_wait", 10 * time.Second, true, "running", false},
		{"no_browser_yet", "02_wait", time.Minute, false, "running", false},
		{"optional_share", "04_share_create", time.Minute, true, "running", false},
		{"already_completed", "02_wait", time.Minute, true, "succeeded", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			d := &scrapeDiagnostic{Dir: t.TempDir(), Started: now.Add(-2 * time.Minute)}
			raw, _ := json.Marshal(map[string]any{"browser_available": test.available, "checkpoint_at": now.Add(-test.age).Format(time.RFC3339Nano), "phase": test.phase, "status": test.status})
			if err := os.WriteFile(filepath.Join(d.Dir, "manifest.json"), raw, 0600); err != nil {
				t.Fatal(err)
			}
			if got := d.pageStalled(now); got != test.want {
				t.Fatalf("stalled=%t want %t", got, test.want)
			}
			t.Setenv("GEO_PAGE_STALL_SEC", "0")
			if d.pageStalled(now) {
				t.Fatal("disabled watchdog still interrupts pages")
			}
		})
	}
}

func TestPageStallDefaultAndExplicitThresholdBoundaries(t *testing.T) {
	now := time.Date(2026, time.October, 17, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name, setting string
		age           time.Duration
		want          bool
	}{
		{"default_keeps_75_second_checkpoint", "", 75 * time.Second, false},
		{"default_stops_at_90_seconds", "", 90 * time.Second, true},
		{"explicit_45_keeps_before_boundary", "45", 44 * time.Second, false},
		{"explicit_45_stops_at_boundary", "45", 45 * time.Second, true},
		{"zero_disables_stale_checkpoint_watchdog", "0", 3 * time.Minute, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("GEO_PAGE_STALL_SEC", tc.setting)
			d := &scrapeDiagnostic{Dir: t.TempDir(), Started: now.Add(-5 * time.Minute)}
			raw, err := json.Marshal(map[string]any{
				"browser_available": true, "phase": "02_wait", "status": "running",
				"checkpoint_at": now.Add(-tc.age).Format(time.RFC3339Nano),
			})
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(d.Dir, "manifest.json"), raw, 0600); err != nil {
				t.Fatal(err)
			}
			if got := d.pageStalled(now); got != tc.want {
				t.Fatalf("setting=%q checkpoint_age=%s stalled=%t want=%t", tc.setting, tc.age, got, tc.want)
			}
		})
	}
}
