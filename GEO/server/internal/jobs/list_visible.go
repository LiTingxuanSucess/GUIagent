package jobs

import "sort"

// Filter while holding the queue lock, then copy only the requested records.
// A zero limit is an explicit unbounded snapshot, used for queued work only.
func (q *Queue) ListFilteredVisible(ak string, admin bool, status, requestID string, limit int) ([]*Job, int) {
	q.mu.Lock()
	defer q.mu.Unlock()
	matching := make([]*Job, 0)
	for _, job := range q.jobs {
		if job == nil || !admin && (ak == "" || job.AK != ak) ||
			status != "" && job.Status != status || requestID != "" && job.RequestID != requestID {
			continue
		}
		matching = append(matching, job)
	}
	sort.Slice(matching, func(i, j int) bool {
		if matching[i].CreatedAt == matching[j].CreatedAt {
			return matching[i].ID > matching[j].ID
		}
		return matching[i].CreatedAt > matching[j].CreatedAt
	})
	total := len(matching)
	if limit > 0 && len(matching) > limit {
		matching = matching[:limit]
	}
	rows := make([]*Job, 0, len(matching))
	for _, job := range matching {
		rows = append(rows, q.publicCopyLocked(job, admin))
	}
	return rows, total
}
