package httpserver

import (
	"encoding/json"
	"mime"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"geo-server/internal/config"
)

func TestManualSamplingReadsCurrentBatchAndOnlyExposesProgress(t *testing.T) {
	root := t.TempDir()
	configPath := filepath.Join(root, "private-config.json")
	if err := os.WriteFile(configPath, []byte(`{"batch_dir":`+quotedSampling(root)+`,"webhook":"PRIVATE_WEBHOOK","jobs_dir":"PRIVATE_PATH"}`), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GEO_SAMPLING_NOTIFY_CONFIG", configPath)
	t.Setenv("GEO_SAMPLING_PROGRESS_FILE", "")
	raw := `{"batch_id":"batch-live","status":"running","updated_at":"2026-10-22T16:00:00+08:00","total":3000,"counts":{"complete":68,"running":2},"current_round":1,"concurrency":6,"by_round":{"1":{"total":600,"counts":{"complete":68}}},"by_site":{"kimi":{"complete":2,"waiting_manual":1}},"active_samples":[{"question_id":"Q203","round":1,"site":"kimi","status":"waiting_manual","job_id":"job-live","phone":"PRIVATE_PHONE","prompt":"PRIVATE_PROMPT"}],"webhook":"PRIVATE_WEBHOOK","source":"PRIVATE_PATH"}`
	if err := os.WriteFile(filepath.Join(root, "progress.json"), []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	s := &Server{cfg: &config.Config{Root: root}}
	progress := s.manualSamplingProgress()
	if !progress.Available || progress.CurrentRound == nil || *progress.CurrentRound != 1 || progress.ByRound["1"].Counts["complete"] != 68 || progress.Counts["complete"] != 68 || progress.Total != 3000 {
		t.Fatalf("wrong progress: %+v", progress)
	}
	response, _ := json.Marshal(progress)
	for _, private := range []string{"PRIVATE_WEBHOOK", "PRIVATE_PATH", "PRIVATE_PHONE", "PRIVATE_PROMPT"} {
		if strings.Contains(string(response), private) {
			t.Fatal("private field exposed", private)
		}
	}
	if len(progress.ActiveSamples) != 1 || progress.ActiveSamples[0].QuestionID != "Q203" {
		t.Fatal("missing active question")
	}
}

func quotedSampling(s string) string { raw, _ := json.Marshal(s); return string(raw) }

func TestManualSamplingUnreadableOrMalformedDataNeverReportsZeroAsLive(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	t.Setenv("GEO_SAMPLING_PROGRESS_FILE", path)
	s := &Server{}
	if p := s.manualSamplingProgress(); p.Available || p.Message == "" {
		t.Fatal("missing progress reported as live")
	}
	for _, raw := range []string{`{"batch_id":`, `{}`, `{"batch_id":"x","counts":{"complete":"wrong"}}`} {
		if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if p := s.manualSamplingProgress(); p.Available || p.Message == "" {
			t.Fatalf("bad data reported as live: %s", raw)
		}
	}
}

func TestSamplingExportReturnsCurrentWorkbookAndAttachment(t *testing.T) {
	root := t.TempDir()
	t.Setenv("GEO_SAMPLING_PROGRESS_FILE", filepath.Join(root, "progress.json"))
	payload := "PK\x03\x04current workbook bytes"
	if err := os.WriteFile(filepath.Join(root, "采样结果.xlsx"), []byte(payload), 0600); err != nil {
		t.Fatal(err)
	}
	s := &Server{}
	for _, method := range []string{http.MethodGet, http.MethodHead} {
		response := httptest.NewRecorder()
		s.adminSamplingExport(response, httptest.NewRequest(method, "/api/admin/sampling/export?file=/etc/passwd", nil))
		if response.Code != 200 || !strings.Contains(response.Header().Get("Content-Type"), "spreadsheetml.sheet") || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("invalid download headers: %d %v", response.Code, response.Header())
		}
		media, params, err := mime.ParseMediaType(response.Header().Get("Content-Disposition"))
		if err != nil || media != "attachment" || !strings.HasPrefix(params["filename"], "GEO采样结果_") || !strings.HasSuffix(params["filename"], ".xlsx") {
			t.Fatal("missing downloadable Unicode filename")
		}
		if method == http.MethodGet && response.Body.String() != payload || method == http.MethodHead && response.Body.Len() != 0 {
			t.Fatal("incorrect workbook body")
		}
	}
	response := httptest.NewRecorder()
	s.adminSamplingExport(response, httptest.NewRequest(http.MethodPost, "/api/admin/sampling/export", nil))
	if response.Code != 405 {
		t.Fatal("export accepted write method")
	}
	if err := os.Remove(filepath.Join(root, "采样结果.xlsx")); err != nil {
		t.Fatal(err)
	}
	response = httptest.NewRecorder()
	s.adminSamplingExport(response, httptest.NewRequest(http.MethodGet, "/api/admin/sampling/export", nil))
	if response.Code != 404 || strings.Contains(response.Body.String(), root) {
		t.Fatal("missing workbook did not return safe error")
	}
}
