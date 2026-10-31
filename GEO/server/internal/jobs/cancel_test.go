package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCancelPersistenceFailureStillPublishesAndReleasesScheduling(t *testing.T) {
	root := t.TempDir()
	dataDir := filepath.Join(root, "jobs")
	// A file at the directory path makes persistence fail, including as root.
	if err := os.WriteFile(dataDir, []byte("blocked"), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	key := fairAdmissionKey{"one", "doubao"}
	q := &Queue{GeoRoot: root, DataDir: dataDir,
		jobs:        map[string]*Job{"one": {ID: "one", AK: "owner", Status: "running", DeliveryPolicy: "eventual"}},
		runCancels:  map[string]context.CancelFunc{"one": stop},
		activeRuns:  map[string]bool{"one": true},
		fairTickets: map[fairAdmissionKey]*fairAdmissionTicket{key: {key: key}},
	}
	q.publish("one")
	if _, err := q.CancelVisible("one", "owner", false); err == nil {
		t.Fatal("persistence failure was not returned")
	}
	if ctx.Err() != context.Canceled || len(q.fairTickets) != 0 {
		t.Fatal("disk failure prevented execution or scheduling cancellation")
	}
	body, ok := q.VisibleBody("one", "owner", false)
	var visible Job
	if !ok || json.Unmarshal(body, &visible) != nil || visible.Status != "cancelled" {
		t.Fatalf("poll snapshot still advertises active work: %s", body)
	}
	if err := os.Remove(dataDir); err != nil {
		t.Fatal(err)
	}
	retired, err := q.CancelVisible("one", "owner", false)
	if err != nil || retired.FinishedAt != visible.FinishedAt {
		t.Fatalf("retry failed or changed cancellation time: %v", err)
	}
	raw, err := os.ReadFile(q.jobPath("one"))
	var saved Job
	if err != nil || json.Unmarshal(raw, &saved) != nil || saved.Status != "cancelled" {
		t.Fatalf("retry did not persist cancellation: %v", err)
	}
}

func TestCancelPreservesResultsOwnershipAndRestart(t *testing.T) {
	q := &Queue{DataDir: t.TempDir(), jobs: map[string]*Job{"one": {
		ID: "one", AK: "owner", Status: "queued", DeliveryPolicy: "eventual", RetryAt: "later",
		CallbackURL: "https://example.org/callback", Sites: []string{"deepseek", "doubao"},
		Results: []map[string]any{{"site_id": "deepseek", "ok": true, "answer": "saved answer"}},
	}}}
	for _, ak := range []string{"", "other"} {
		if _, err := q.CancelVisible("one", ak, false); !errors.Is(err, ErrJobNotFound) {
			t.Fatal(err)
		}
	}
	j, err := q.CancelVisible("one", "owner", false)
	if err != nil || j.Status != "cancelled" || j.ErrorCode != "request_cancelled" || j.AK != "" || j.RetryAt != "" || j.FinishedAt == "" || j.CallbackStatus != "skipped" {
		t.Fatalf("cancel result: %+v %v", j, err)
	}
	if len(j.Results) != 1 || j.Results[0]["answer"] != "saved answer" {
		t.Fatal("lost existing result")
	}
	q.patch("one", func(j *Job) { j.Status, j.Results = "succeeded", nil })
	again, _ := q.CancelVisible("one", "owner", false)
	if again.Status != "cancelled" || again.FinishedAt != j.FinishedAt {
		t.Fatal("late worker or replay changed cancellation")
	}
	restored := &Queue{DataDir: q.DataDir, jobs: map[string]*Job{}}
	_, queued, interrupted := restored.load()
	if len(queued)+len(interrupted) != 0 || restored.jobs["one"].Status != "cancelled" {
		t.Fatal("restart replayed cancelled work")
	}
	q.jobs["done"] = &Job{ID: "done", AK: "owner", Status: "succeeded"}
	if done, err := q.CancelVisible("done", "owner", false); err != nil || done.Status != "succeeded" {
		t.Fatal("rewrote terminal job")
	}
}

func TestCancelStopsExecutingScraperAndReleasesPermits(t *testing.T) {
	t.Setenv("GEO_BROWSER_MEMORY_HIGH_MB", "0")
	t.Setenv("GEO_BROWSER_MEMORY_MAX_MB", "0")
	for _, eventual := range []string{"0", "1"} {
		t.Run(eventual, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_REQUIRE_DELIVERY", eventual)
			t.Setenv("GEO_PLATFORM_DISPATCH", "1")
			t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
			t.Setenv("GEO_API_FALLBACK", "0")
			t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
			root := t.TempDir()
			script := filepath.Join(root, "server", "scripts", "run_scrape.py")
			if err := os.MkdirAll(filepath.Dir(script), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(script, []byte("import pathlib,time\npathlib.Path('started').touch()\ntime.sleep(30)\n"), 0600); err != nil {
				t.Fatal(err)
			}
			q := New(root, nil, nil, filepath.Join(root, "jobs"))
			j, _, err := q.Submit("cancel test", []string{"doubao"}, "", "owner", "cancel-id")
			if err != nil {
				t.Fatal(err)
			}
			deadline := time.Now().Add(5 * time.Second)
			for {
				if _, err := os.Stat(filepath.Join(root, "started")); err == nil {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("scraper never started")
				}
				time.Sleep(10 * time.Millisecond)
			}
			if _, err := q.CancelVisible(j.ID, "owner", false); err != nil {
				t.Fatal(err)
			}
			deadline = time.Now().Add(4 * time.Second)
			for {
				q.mu.Lock()
				active := q.activeRuns[j.ID]
				q.mu.Unlock()
				if !active {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("cancelled scraper did not stop")
				}
				time.Sleep(10 * time.Millisecond)
			}
			if len(q.siteSem) != 0 || len(q.cloudSem) != 0 {
				t.Fatal("leaked execution permit")
			}
			latest, _ := q.Get(j.ID)
			if latest.Status != "cancelled" || q.scheduleDeliveries(time.Now().Add(time.Hour)) != 0 {
				t.Fatal("cancelled job restarted")
			}
			raw, _ := os.ReadFile(q.jobPath(j.ID))
			var disk Job
			if json.Unmarshal(raw, &disk) != nil || disk.Status != "cancelled" {
				t.Fatal("late worker overwrote disk")
			}
		})
	}
}
