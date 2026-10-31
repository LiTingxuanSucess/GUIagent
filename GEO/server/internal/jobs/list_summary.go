package jobs

import "sort"

// JobSummary contains only what the console table displays. Full answers and
// reference payloads are read from the existing per-job endpoint on demand.
type JobSummary struct {
	ID             string           `json:"job_id"`
	Status         string           `json:"status"`
	Prompt         string           `json:"prompt"`
	Sites          []string         `json:"sites"`
	AK             string           `json:"ak,omitempty"`
	Results        []map[string]any `json:"results,omitempty"`
	CreatedAt      string           `json:"created_at"`
	ElapsedMS      int64            `json:"elapsed_ms,omitempty"`
	CallbackStatus string           `json:"callback_status,omitempty"`
	HasCallback    bool             `json:"has_callback"`
}

func (q *Queue) ListSummariesVisible(ak string, admin bool, offset, limit int) ([]JobSummary, int) {
	if offset < 0 {
		offset = 0
	}
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	matching := make([]*Job, 0, len(q.jobs))
	for _, j := range q.jobs {
		if j == nil || (!admin && (ak == "" || j.AK != ak)) {
			continue
		}
		matching = append(matching, j)
	}
	sort.Slice(matching, func(i, k int) bool {
		if matching[i].CreatedAt == matching[k].CreatedAt {
			return matching[i].ID > matching[k].ID
		}
		return matching[i].CreatedAt > matching[k].CreatedAt
	})
	total := len(matching)
	if offset > total {
		offset = total
	}
	end := offset + limit
	if end > total {
		end = total
	}
	out := make([]JobSummary, 0, end-offset)
	for _, j := range matching[offset:end] {
		prompt := []rune(j.Prompt)
		if len(prompt) > 160 {
			prompt = prompt[:160]
		}
		row := JobSummary{ID: j.ID, Status: j.Status, Prompt: string(prompt),
			Sites: append([]string(nil), j.Sites...), CreatedAt: j.CreatedAt,
			ElapsedMS: j.ElapsedMS, CallbackStatus: j.CallbackStatus, HasCallback: j.CallbackURL != ""}
		if admin {
			row.AK = j.AK
		}
		for _, result := range j.Results {
			if result == nil {
				continue
			}
			status := map[string]any{"site_id": result["site_id"]}
			if ok, exists := result["ok"].(bool); exists {
				status["ok"] = ok
			}
			row.Results = append(row.Results, status)
		}
		out = append(out, row)
	}
	return out, total
}
