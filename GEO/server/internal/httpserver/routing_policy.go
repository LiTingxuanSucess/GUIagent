package httpserver

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"geo-server/internal/jobs"
)

func (s *Server) adminRoutingPolicy(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != http.MethodGet && r.Method != http.MethodPut {
		writeJSON(w, 405, map[string]any{"detail": "method not allowed"})
		return
	}
	if s.jobs == nil {
		writeJSON(w, 503, map[string]any{"detail": "请求渠道设置暂不可用"})
		return
	}
	if r.Method == http.MethodGet {
		state, err := s.jobs.RoutingPolicy()
		if err != nil {
			writeJSON(w, 503, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, 200, state)
		return
	}
	var body struct {
		WebFirst *bool           `json:"web_first"`
		Revision *uint64         `json:"revision"`
		SiteID   json.RawMessage `json:"site_id"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"detail": "请求格式无效"})
		return
	}
	var extra any
	if body.WebFirst == nil || body.Revision == nil || decoder.Decode(&extra) != io.EOF {
		writeJSON(w, 400, map[string]any{"detail": "必须提供 web_first 布尔值和当前 revision"})
		return
	}
	var state jobs.RoutingPolicy
	var err error
	if len(body.SiteID) == 0 {
		state, err = s.jobs.SetRoutingPolicy(*body.WebFirst, *body.Revision)
	} else {
		var site string
		if json.Unmarshal(body.SiteID, &site) != nil || site == "" {
			writeJSON(w, 400, map[string]any{"detail": "site_id 必须为有效的平台字符串"})
			return
		}
		state, err = s.jobs.SetSiteRoutingPolicy(site, *body.WebFirst, *body.Revision)
	}
	if errors.Is(err, jobs.ErrRoutingSite) {
		writeJSON(w, 400, map[string]any{"detail": err.Error()})
		return
	}
	if errors.Is(err, jobs.ErrRoutingConflict) {
		writeJSON(w, 409, map[string]any{"detail": "设置已被其他管理员修改，请刷新后重试", "current": state})
		return
	}
	if err != nil {
		writeJSON(w, 503, map[string]any{"detail": err.Error()})
		return
	}
	writeJSON(w, 200, state)
}
