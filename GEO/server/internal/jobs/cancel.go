package jobs

import (
	"errors"
	"time"
)

var ErrJobNotFound = errors.New("job not found")

// CancelVisible commits an irreversible terminal state before a late worker can
// publish another result. Context cancellation stops local work; remote servers
// may already have received a question and cannot be recalled.
func (q *Queue) CancelVisible(id, ak string, admin bool) (*Job, error) {
	q.mu.Lock()
	j := q.jobs[id]
	if j == nil || !admin && (ak == "" || j.AK != ak) {
		q.mu.Unlock()
		return nil, ErrJobNotFound
	}
	if j.Status == "queued" || j.Status == "running" || j.Status == "waiting_manual" {
		j.Status, j.ErrorCode, j.Error = "cancelled", "request_cancelled", "任务已由调用方取消"
		j.FinishedAt = time.Now().Format(time.RFC3339Nano)
		j.UpdatedAt = j.FinishedAt
		j.Retryable = false
		j.RetryAt, j.WaitReason, j.DeadlineAt = "", "", ""
		j.RecoveryStates = nil
		if j.createdUnix > 0 {
			j.TotalMS = time.Now().UnixMilli() - j.createdUnix
		}
		if j.CallbackURL != "" {
			j.CallbackStatus, j.CallbackError = "skipped", ""
		}
		delete(q.enqueued, id)
	}
	cancel, active := q.runCancels[id], q.activeRuns[id]
	saved := copyJob(j)
	out := q.publicCopyLocked(j, admin)
	q.mu.Unlock()
	if saved.Status == "cancelled" {
		if cancel != nil {
			cancel()
		}
		err := q.saveChecked(saved)
		// Cancellation has already stopped local work. Publish it and release
		// scheduling intent even when disk persistence needs a caller retry.
		q.publish(id)
		q.finishFairRun(id)
		if !active {
			q.cleanupWebRecovery(id)
		}
		if err != nil {
			return nil, err
		}
	}
	return out, nil
}
