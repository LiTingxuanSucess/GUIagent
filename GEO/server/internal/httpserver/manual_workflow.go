package httpserver

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strings"
	"time"

	"geo-server/internal/jobs"
)

func (s *Server) adminManualTasks(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/admin/manual-tasks"), "/")
	if path == "" && r.Method == http.MethodGet {
		rows := []*jobs.Job{}
		for _, job := range s.jobs.List() {
			if job.ManualWorkflow {
				rows = append(rows, job)
				if len(rows) == 30 {
					break
				}
			}
		}
		writeJSON(w, 200, map[string]any{"enabled": os.Getenv("GEO_MANUAL_WORKFLOW") == "1", "auto_recovery_enabled": os.Getenv("GEO_MANUAL_AUTO_RECOVERY") == "1", "tasks": s.jobs.ManualTasks(), "jobs": rows, "sampling": s.manualSamplingProgress()})
		return
	}
	parts := strings.Split(path, "/")
	if len(parts) != 2 || r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"detail": "需要有效任务、平台和 POST 操作"})
		return
	}
	var body struct {
		Action   string  `json:"action"`
		Revision *uint64 `json:"revision"`
		ShareURL string  `json:"share_url"`
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&body); err != nil || body.Revision == nil {
		writeJSON(w, 400, map[string]any{"detail": "需要 action 和 revision"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	var shareURL []string
	if body.ShareURL != "" {
		shareURL = []string{body.ShareURL}
	}
	job, err := s.jobs.ManualAction(ctx, parts[0], parts[1], body.Action, *body.Revision, shareURL...)
	if err != nil {
		status := 409
		if errors.Is(err, jobs.ErrManualNotFound) {
			status = 404
		}
		writeJSON(w, status, map[string]any{"detail": err.Error()})
		return
	}
	writeJSON(w, 200, job)
}
