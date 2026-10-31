package jobs

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func deploymentQueueFixture(t *testing.T, budget time.Duration) (*Queue, *Job, string) {
	t.Helper()
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "agentbay")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
	root := t.TempDir()
	dir := filepath.Join(root, "jobs")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(root, "scrape.py")
	if err := os.WriteFile(script, []byte("import json\nfrom pathlib import Path\nPath('dispatched').touch()\nprint(json.dumps({'ok':True,'answer':'completed fixture answer'}))\n"), 0600); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	j := &Job{ID: "new-arrival", RequestID: "customer-request", Sites: []string{"doubao"}, Status: "queued", Prompt: "current question", CreatedAt: now.Format(time.RFC3339Nano), DeadlineAt: now.Add(budget).Format(time.RFC3339Nano), createdUnix: now.UnixMilli()}
	q := &Queue{GeoRoot: root, Python: "python3", Script: script, DataDir: dir, jobs: map[string]*Job{j.ID: j}, siteSem: make(chan struct{}, 2)}
	q.save(j)
	marker := filepath.Join(root, ".deployment-drain.json")
	raw, _ := json.Marshal(map[string]any{"expires_at": time.Now().Add(time.Minute).Unix(), "allow": []string{}})
	if err := os.WriteFile(marker, raw, 0600); err != nil {
		t.Fatal(err)
	}
	return q, j, marker
}

func TestDeploymentArrivalStaysQueuedAndSurvivesReloadBeforePython(t *testing.T) {
	q, j, marker := deploymentQueueFixture(t, 10*time.Second)
	done := make(chan struct{})
	go func() { q.run(j.ID); close(done) }()
	// The accepted job must stay queued even if no Python held marker exists.
	time.Sleep(50 * time.Millisecond)
	held, _ := q.Get(j.ID)
	if held.Status != "queued" {
		t.Errorf("maintenance arrival was dispatched: %s", held.Status)
	}
	loaded := &Queue{DataDir: q.DataDir, jobs: map[string]*Job{}}
	_, queued, _ := loaded.load()
	if len(queued) != 1 || loaded.jobs[j.ID].DeadlineAt != j.DeadlineAt || loaded.jobs[j.ID].RequestID != j.RequestID {
		t.Error("maintenance changed identity, deadline, or restart safety")
	}
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("queue did not resume")
	}
	finished, _ := q.Get(j.ID)
	if finished.Status != "succeeded" {
		t.Fatalf("resumed job failed: %s", finished.Status)
	}
}

func TestDeploymentWaitPastLegacyDeadlineSurvivesReloadAndExecutesWeb(t *testing.T) {
	q, j, marker := deploymentQueueFixture(t, 40*time.Millisecond)
	done := make(chan struct{})
	go func() { q.run(j.ID); close(done) }()
	time.Sleep(120 * time.Millisecond)
	held, _ := q.Get(j.ID)
	if held.Status != "queued" || held.ErrorCode != "" || len(held.Results) != 0 {
		t.Errorf("maintenance wait expired or dispatched an accepted request: %+v", held)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "dispatched")); !os.IsNotExist(err) {
		t.Error("old deadline allowed a request to cross the deployment hold")
	}
	loaded := &Queue{DataDir: q.DataDir, jobs: map[string]*Job{}}
	_, queued, _ := loaded.load()
	if len(queued) != 1 || loaded.jobs[j.ID].RequestID != j.RequestID || loaded.jobs[j.ID].CreatedAt != j.CreatedAt {
		t.Error("expired legacy deadline lost queued request identity during reload")
	}
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("held request did not resume after maintenance")
	}
	finished, _ := q.Get(j.ID)
	if finished.Status != "succeeded" || len(finished.Results) != 1 || finished.Results[0]["ok"] != true || finished.QueuedMS < 100 {
		t.Fatalf("legacy deadline prevented the accepted webpage request: %+v", finished)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "dispatched")); err != nil {
		t.Fatalf("resumed request never reached webpage fixture: %v", err)
	}
}
