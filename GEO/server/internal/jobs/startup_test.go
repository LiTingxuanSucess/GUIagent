package jobs

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeStartupJob(t testing.TB, dir string, job Job) string {
	t.Helper()
	raw, err := json.Marshal(job)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, job.ID+".json")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadDoesNotRewriteUnchangedHistory(t *testing.T) {
	dir := t.TempDir()
	stamp := time.Date(2024, 1, 2, 3, 4, 5, 0, time.UTC)
	before := make(map[string]os.FileInfo)
	contents := make(map[string][]byte)
	for _, status := range []string{"queued", "succeeded", "partial", "failed"} {
		job := Job{
			ID: status, Status: status, RequestID: "request-" + status, AK: "tenant-a",
			Prompt: "test prompt", Sites: []string{"doubao"},
			CreatedAt: stamp.Format(time.RFC3339Nano), UpdatedAt: stamp.Format(time.RFC3339Nano),
			DeadlineAt: time.Now().Add(time.Hour).Format(time.RFC3339Nano),
			Results:    []map[string]any{{"ok": true, "site_id": "doubao", "answer": "test answer", "account_id": "internal-account"}},
		}
		if status == "queued" {
			job.DeadlinePolicy = "web_first"
		}
		path := writeStartupJob(t, dir, job)
		if err := os.Chtimes(path, stamp, stamp); err != nil {
			t.Fatal(err)
		}
		before[path], _ = os.Stat(path)
		contents[path], _ = os.ReadFile(path)
	}
	q := &Queue{DataDir: dir, jobs: map[string]*Job{}}
	n, queued, interrupted := q.load()
	if n != 4 || len(queued) != 1 || queued[0] != "queued" || len(interrupted) != 0 {
		t.Fatalf("load counts: loaded=%d queued=%d interrupted=%d", n, len(queued), len(interrupted))
	}
	for path, info := range before {
		after, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if !os.SameFile(info, after) || !after.ModTime().Equal(info.ModTime()) || !bytes.Equal(raw, contents[path]) {
			t.Errorf("unchanged %s was rewritten", filepath.Base(path))
		}
	}
	if q.jobs["succeeded"].createdUnix != stamp.UnixMilli() {
		t.Fatal("creation time was not restored in memory")
	}
	duplicate, created, err := q.Submit("test prompt", []string{"doubao"}, "", "tenant-a", "request-succeeded")
	if err != nil || created || duplicate.ID != "succeeded" {
		t.Fatalf("persisted request identity was not restored: created=%t err=%v", created, err)
	}
	for _, id := range []string{"queued", "succeeded", "partial", "failed"} {
		public, ok := q.VisibleBody(id, "tenant-a", false)
		if !ok {
			t.Fatalf("missing startup public snapshot for %s", id)
		}
		var job Job
		if err := json.Unmarshal(public, &job); err != nil {
			t.Fatal(err)
		}
		if job.AK != "" || job.QueueLen != 1 || job.Results[0]["account_id"] != nil {
			t.Errorf("public startup snapshot exposes internal state or has wrong queue length for %s", id)
		}
		if _, ok := q.VisibleBody(id, "tenant-b", false); ok {
			t.Errorf("startup snapshot for %s crosses tenant boundary", id)
		}
		admin, ok := q.VisibleBody(id, "", true)
		if !ok || !bytes.Contains(admin, []byte("internal-account")) {
			t.Errorf("admin startup snapshot lost internal state for %s", id)
		}
	}
}

func TestLoadMigratesLegacyQueuedPolicyOnlyOnce(t *testing.T) {
	dir := t.TempDir()
	created := time.Now().Add(-time.Hour).Format(time.RFC3339Nano)
	deadline := time.Now().Add(-time.Minute).Format(time.RFC3339Nano)
	job := Job{ID: "legacy-queued", Status: "queued", RequestID: "customer-request", CreatedAt: created, DeadlineAt: deadline}
	path := writeStartupJob(t, dir, job)
	q := &Queue{DataDir: dir, jobs: map[string]*Job{}}
	_, queued, interrupted := q.load()
	if len(queued) != 1 || len(interrupted) != 0 {
		t.Fatalf("legacy accepted request was expired during migration: queued=%v interrupted=%v", queued, interrupted)
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &job) != nil {
		t.Fatalf("could not read migrated queued request: %v", err)
	}
	if job.Status != "queued" || job.DeadlinePolicy != "web_first" || job.RequestID != "customer-request" || job.CreatedAt != created || job.DeadlineAt != deadline {
		t.Fatalf("queued migration changed identity or dispatched a request: %+v", job)
	}
	before, _ := os.Stat(path)
	reloaded := &Queue{DataDir: dir, jobs: map[string]*Job{}}
	reloaded.load()
	after, _ := os.Stat(path)
	current, _ := os.ReadFile(path)
	if !os.SameFile(before, after) || !after.ModTime().Equal(before.ModTime()) || !bytes.Equal(raw, current) {
		t.Fatal("already migrated queued request was rewritten on another restart")
	}
}

func TestLoadPersistsOnlyRecoveredRunningJobs(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "jobs")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	created := time.Now().Add(-time.Minute).Format(time.RFC3339Nano)
	deadline := time.Now().Add(time.Hour).Format(time.RFC3339Nano)
	for _, id := range []string{"held", "interrupted", "callback"} {
		job := Job{ID: id, RequestID: "request-" + id, Status: "running", CreatedAt: created, DeadlineAt: deadline, StartedAt: created}
		if id == "held" {
			job.CompletedSites = 1
			job.Results = []map[string]any{{"web_skipped": true}}
		}
		if id == "callback" {
			job.CallbackURL = "https://example.com/callback"
			job.CallbackAttempts = 3
			job.CallbackStatus = "retrying"
		}
		writeStartupJob(t, dir, job)
	}
	drain, _ := json.Marshal(map[string]any{"expires_at": time.Now().Add(time.Minute).Unix(), "allow": []string{}})
	if err := os.WriteFile(filepath.Join(root, ".deployment-drain.json"), drain, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".deployment-held-job_held.json"), []byte(`{"question_id":"job_held"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	q := &Queue{DataDir: dir, jobs: map[string]*Job{}}
	n, queued, interrupted := q.load()
	if n != 3 || len(queued) != 1 || queued[0] != "held" || len(interrupted) != 1 || interrupted[0] != "callback" {
		t.Fatalf("recovery counts: loaded=%d queued=%v interrupted=%v", n, queued, interrupted)
	}
	for _, id := range []string{"held", "interrupted", "callback"} {
		raw, err := os.ReadFile(filepath.Join(dir, id+".json"))
		if err != nil {
			t.Fatal(err)
		}
		var job Job
		if err := json.Unmarshal(raw, &job); err != nil {
			t.Fatal(err)
		}
		if job.RequestID != "request-"+id || job.CreatedAt != created || job.DeadlineAt != deadline {
			t.Errorf("recovery changed request identity or deadline for %s", id)
		}
		if id == "held" {
			if job.Status != "queued" || job.StartedAt != "" || len(job.Results) != 0 || job.CompletedSites != 0 {
				t.Error("undispatched deployment-held job was not persisted back to queued")
			}
		} else if job.Status != "failed" || job.ErrorCode != "service_interrupted" || !job.Retryable || job.FinishedAt == "" {
			t.Errorf("interrupted job was not persisted as retryable failure for %s", id)
		}
		if id == "callback" && (job.CallbackStatus != "pending" || job.CallbackAttempts != 0) {
			t.Error("interrupted callback was not reset for delivery")
		}
	}
	reloaded := &Queue{DataDir: dir, jobs: map[string]*Job{}}
	_, queued, interrupted = reloaded.load()
	if len(queued) != 1 || queued[0] != "held" || len(interrupted) != 0 {
		t.Fatal("a second startup replayed dispatched work or interruption callbacks")
	}
}

func TestSnapshotBatchKeepsActivePositionsAndTenantVisibility(t *testing.T) {
	q := &Queue{jobs: map[string]*Job{}}
	for i := 0; i < 1000; i++ {
		id := fmt.Sprintf("terminal-%04d", i)
		q.jobs[id] = &Job{ID: id, Status: "succeeded", AK: "tenant-a", QueuePos: 9, QueueLen: 9}
	}
	for i, status := range []string{"running", "queued", "queued"} {
		id := fmt.Sprintf("active-%d", i)
		q.jobs[id] = &Job{ID: id, Status: status, AK: "tenant-a", CreatedAt: fmt.Sprintf("2024-01-02T03:04:0%dZ", i)}
	}
	q.publishAll()
	for id, original := range q.jobs {
		body, ok := q.VisibleBody(id, "tenant-a", false)
		if !ok {
			t.Fatalf("missing snapshot for %s", id)
		}
		var job Job
		if err := json.Unmarshal(body, &job); err != nil {
			t.Fatal(err)
		}
		position := 0
		if strings.HasPrefix(id, "active-") {
			_, _ = fmt.Sscanf(id, "active-%d", &position)
		}
		if job.QueueLen != 3 || job.QueuePos != position || job.Status != original.Status || job.AK != "" {
			t.Errorf("wrong batch queue metrics or visibility for %s", id)
		}
		if _, ok := q.VisibleBody(id, "tenant-b", false); ok {
			t.Errorf("snapshot for %s is visible to another tenant", id)
		}
	}
	q.snapMu.Lock()
	q.snaps["removed"] = &jobSnap{}
	q.snapMu.Unlock()
	q.publishIDs([]string{"removed", "terminal-0000", "terminal-0000"}, false)
	if _, ok := q.VisibleBody("removed", "", true); ok {
		t.Fatal("publication retained a deleted job snapshot")
	}
	q.mu.Lock()
	q.jobs["active-0"].Status = "succeeded"
	q.mu.Unlock()
	q.publish("active-0")
	for i := 1; i < 3; i++ {
		body, _ := q.VisibleBody(fmt.Sprintf("active-%d", i), "", true)
		var job Job
		if err := json.Unmarshal(body, &job); err != nil {
			t.Fatal(err)
		}
		if job.QueueLen != 2 || job.QueuePos != i-1 {
			t.Error("completion did not refresh remaining active queue positions")
		}
	}
}

// GEO_STARTUP_BENCH_FIXTURE may point to a frozen copy of persisted jobs.
// Each timed load uses a new private copy, so the source is never modified.
func BenchmarkLoadPersistedHistory(b *testing.B) {
	b.StopTimer()
	fixture := os.Getenv("GEO_STARTUP_BENCH_FIXTURE")
	if fixture == "" {
		fixture = b.TempDir()
		answer := strings.Repeat("test answer ", 600)
		for i := 0; i < 2243; i++ {
			writeStartupJob(b, fixture, Job{
				ID: fmt.Sprintf("history-%04d", i), Status: "succeeded", CreatedAt: "2024-01-02T03:04:05Z",
				Results: []map[string]any{{"site_id": "doubao", "answer": answer}},
			})
		}
	}
	paths, err := filepath.Glob(filepath.Join(fixture, "*.json"))
	if err != nil || len(paths) == 0 {
		b.Fatal("startup benchmark fixture contains no job JSON files")
	}
	var totalBytes int64
	var rewrites int
	for i := 0; i < b.N; i++ {
		dir := b.TempDir()
		before := make(map[string]os.FileInfo, len(paths))
		for _, path := range paths {
			raw, err := os.ReadFile(path)
			if err != nil {
				b.Fatal(err)
			}
			if i == 0 {
				totalBytes += int64(len(raw))
			}
			copied := filepath.Join(dir, filepath.Base(path))
			if err := os.WriteFile(copied, raw, 0o600); err != nil {
				b.Fatal(err)
			}
			before[copied], _ = os.Stat(copied)
		}
		q := &Queue{DataDir: dir, jobs: map[string]*Job{}}
		b.StartTimer()
		n, _, _ := q.load()
		b.StopTimer()
		if n != len(paths) || len(q.snaps) != len(paths) {
			b.Fatal("startup benchmark did not load and publish every fixture job")
		}
		for path, info := range before {
			after, err := os.Stat(path)
			if err != nil {
				b.Fatal(err)
			}
			if !os.SameFile(info, after) {
				rewrites++
			}
		}
	}
	b.SetBytes(totalBytes)
	b.ReportMetric(float64(rewrites)/float64(b.N), "rewrites/op")
	b.ReportMetric(float64(len(paths)), "jobs/op")
}
