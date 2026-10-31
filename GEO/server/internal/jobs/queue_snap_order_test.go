package jobs

import (
	"encoding/json"
	"sync"
	"testing"
	"time"
)

// Pause an old publication after it captured the job and released q.mu,
// making the otherwise timing-sensitive stale-snapshot race controllable.
type snapshotMarshalBarrier struct {
	once    sync.Once
	entered chan struct{}
	release chan struct{}
}

func (b *snapshotMarshalBarrier) MarshalJSON() ([]byte, error) {
	b.once.Do(func() {
		close(b.entered)
		<-b.release
	})
	return []byte("null"), nil
}

func TestSnapshotPublicationCannotOverwriteNewerState(t *testing.T) {
	for _, change := range []string{"terminal", "extended_deadline"} {
		t.Run(change, func(t *testing.T) {
			oldDeadline := time.Now().Add(time.Minute).Format(time.RFC3339Nano)
			newDeadline := time.Now().Add(5 * time.Minute).Format(time.RFC3339Nano)
			q := &Queue{jobs: map[string]*Job{
				"job": {ID: "job", AK: "tenant", Status: "queued", CreatedAt: time.Now().Format(time.RFC3339Nano), DeadlineAt: oldDeadline},
			}}
			q.publish("job")
			barrier := &snapshotMarshalBarrier{entered: make(chan struct{}), release: make(chan struct{})}
			var releaseOnce sync.Once
			release := func() { releaseOnce.Do(func() { close(barrier.release) }) }
			defer release()
			q.mu.Lock()
			q.jobs["job"].Status = "running"
			q.jobs["job"].Results = []map[string]any{{"ok": true, "answer": "old captured answer", "marshal_barrier": barrier}}
			q.mu.Unlock()
			oldDone := make(chan struct{})
			go func() { q.publish("job"); close(oldDone) }()
			select {
			case <-barrier.entered:
			case <-time.After(time.Second):
				t.Fatal("old publication never reached its marshaling barrier")
			}
			// Check serialization while the old marshal is certainly suspended,
			// so the regression does not depend on goroutine scheduling speed.
			if q.snapPublishMu.TryLock() {
				q.snapPublishMu.Unlock()
				t.Error("old snapshot was allowed to publish after a newer capture")
			}
			updated := make(chan struct{})
			newDone := make(chan struct{})
			go func() {
				q.patch("job", func(j *Job) {
					j.Results = []map[string]any{{"ok": true, "answer": "new answer"}}
					j.DeadlineAt = newDeadline
					if change == "terminal" {
						j.Status = "succeeded"
						j.FinishedAt = time.Now().Format(time.RFC3339Nano)
					}
					close(updated)
				})
				close(newDone)
			}()
			select {
			case <-updated:
			case <-time.After(time.Second):
				t.Fatal("snapshot marshaling held the live job mutex")
			}
			// Polling continues to serve the last published immutable response
			// while newer publication work waits. It must not wait for either
			// the publication mutex or the live job mutex.
			polled := make(chan bool, 1)
			go func() {
				body, ok := q.VisibleBody("job", "tenant", false)
				var job Job
				polled <- ok && json.Unmarshal(body, &job) == nil && job.Status == "queued"
			}()
			select {
			case ok := <-polled:
				if !ok {
					t.Error("polling lost the last successfully published snapshot")
				}
			case <-time.After(time.Second):
				t.Error("polling blocked behind snapshot marshaling")
			}
			select {
			case <-newDone:
				t.Error("new publication overtook the blocked old publication")
			default:
			}
			release()
			for _, done := range []chan struct{}{oldDone, newDone} {
				select {
				case <-done:
				case <-time.After(time.Second):
					t.Fatal("serialized publication did not finish after releasing the marshal barrier")
				}
			}
			for _, admin := range []bool{false, true} {
				body, ok := q.VisibleBody("job", "tenant", admin)
				var job Job
				if !ok || json.Unmarshal(body, &job) != nil || job.DeadlineAt != newDeadline || len(job.Results) != 1 || job.Results[0]["answer"] != "new answer" {
					t.Fatalf("old publication overwrote the new deadline or answer: %s", body)
				}
				wantStatus := "running"
				if change == "terminal" {
					wantStatus = "succeeded"
				}
				if job.Status != wantStatus || change == "terminal" && (job.FinishedAt == "" || job.QueueLen != 0) {
					t.Fatalf("old publication overwrote terminal or queue state: %s", body)
				}
			}
		})
	}
}
