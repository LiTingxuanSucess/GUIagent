package httpserver

import (
	"context"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

func (s *Server) pageMonitor(w http.ResponseWriter, r *http.Request) {
	if !s.auth.IsAdmin(r) {
		http.Redirect(w, r, "/admin/login?next=%2Fadmin%2Fmonitor", http.StatusFound)
		return
	}
	s.servePage(w, "monitor.html")
}

func (s *Server) apiMonitor(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != http.MethodGet {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	window := r.URL.Query().Get("window")
	date := r.URL.Query().Get("date")
	if _, present := r.URL.Query()["date"]; present && window != "day" {
		writeJSON(w, 400, map[string]any{"detail": "日期筛选必须使用按天统计"})
		return
	}
	if window != "" {
		switch window {
		case "day":
			if _, _, err := monitorDayBounds(date); err != nil {
				writeJSON(w, 400, map[string]any{"detail": "日期无效，请使用 YYYY-MM-DD"})
				return
			}
		case "15m", "1h", "6h", "24h", "today", "afternoon":
		default:
			writeJSON(w, 400, map[string]any{"detail": "统计范围无效"})
			return
		}
	}
	root := filepath.Join(s.cfg.Root, "data")
	if configured := os.Getenv("GEO_DATA_DIR"); configured != "" {
		root = configured
	}
	f, err := os.Open(filepath.Join(root, "monitor-dashboard.json"))
	if err != nil {
		writeJSON(w, 503, map[string]any{"detail": "监控进程尚未就绪"})
		return
	}
	defer f.Close()
	var snapshot map[string]any
	if json.NewDecoder(io.LimitReader(f, 4<<20)).Decode(&snapshot) != nil {
		writeJSON(w, 503, map[string]any{"detail": "监控数据暂不可用"})
		return
	}
	if window == "day" {
		dailyFile, err := os.Open(filepath.Join(root, "monitor-daily.json"))
		if err != nil {
			writeJSON(w, 503, map[string]any{"detail": "按天统计尚未就绪，请稍后刷新"})
			return
		}
		defer dailyFile.Close()
		var daily struct {
			UpdatedAt float64                   `json:"updated_at"`
			Days      map[string]map[string]any `json:"days"`
		}
		if json.NewDecoder(io.LimitReader(dailyFile, 16<<20)).Decode(&daily) != nil {
			writeJSON(w, 503, map[string]any{"detail": "按天统计暂不可用"})
			return
		}
		view := daily.Days[date]
		if view == nil {
			view = emptyMonitorDay()
		}
		delete(view, "recent")
		snapshot["windows"] = map[string]any{"day": view}
		snapshot["selected_date"] = date
		snapshot["timezone"] = "Asia/Shanghai"
		// The day file and summary are atomically published separately. Report
		// the older timestamp so freshness never conceals a failed publication.
		if updated, _ := snapshot["updated_at"].(float64); daily.UpdatedAt < updated {
			snapshot["updated_at"] = daily.UpdatedAt
		}
	}
	if window != "" {
		windows, _ := snapshot["windows"].(map[string]any)
		view, ok := windows[window].(map[string]any)
		if !ok {
			writeJSON(w, 503, map[string]any{"detail": "当前统计范围尚未生成，请稍后刷新"})
			return
		}
		// Requests have their own paginated endpoint. Sending the recent-job
		// arrays in every window duplicates almost the entire response body.
		delete(view, "recent")
		snapshot["windows"] = map[string]any{window: view}
	}
	// Use the server clock for freshness. A browser clock ahead by a minute
	// must not turn a healthy monitor into a false service-stalled warning.
	now := float64(time.Now().UnixNano()) / 1e9
	updated, _ := snapshot["updated_at"].(float64)
	snapshot["server_time"] = now
	snapshot["snapshot_age_seconds"] = math.Max(0, now-updated)
	snapshot["poll_interval_seconds"] = 10
	snapshot["stale_after_seconds"] = 40
	if s.jobs != nil {
		snapshot["cursor_health"] = s.jobs.CursorHealth()
	}
	writeJSON(w, 200, snapshot)
}

func (s *Server) apiCursorHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	if s.jobs == nil {
		writeJSON(w, 503, map[string]any{"detail": "CLI 状态暂不可用"})
		return
	}
	if r.Method == http.MethodPost {
		ctx, cancel := context.WithTimeout(r.Context(), 50*time.Second)
		defer cancel()
		writeJSON(w, 200, s.jobs.RecheckCursorProvider(ctx))
		return
	}
	writeJSON(w, 200, s.jobs.CursorHealth())
}

func emptyMonitorDay() map[string]any {
	channels := map[string]any{}
	for _, channel := range []string{"web", "api", "cursor"} {
		channels[channel] = map[string]any{"total": 0, "success": 0, "failed": 0, "running": 0, "skipped": 0, "unknown": 0, "success_rate": nil, "p50_ms": 0, "p95_ms": 0, "avg_ms": 0, "latency_samples": 0}
	}
	return map[string]any{"total": 0, "counts": map[string]int{"queued": 0, "running": 0, "succeeded": 0, "partial": 0, "failed": 0}, "success_rate": nil, "p50_ms": 0, "p95_ms": 0, "channels": channels, "sites": []any{}, "accounts": []any{}, "trend": []any{}}
}
