package httpserver

import (
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

type samplingRound struct {
	Total  int            `json:"total"`
	Counts map[string]int `json:"counts"`
}

type samplingSample struct {
	QuestionID      string   `json:"question_id"`
	Round           int      `json:"round"`
	Site            string   `json:"site"`
	Status          string   `json:"status"`
	JobID           string   `json:"job_id,omitempty"`
	QualityIssues   []string `json:"quality_issues,omitempty"`
	ErrorCode       string   `json:"error_code,omitempty"`
	Reason          string   `json:"reason,omitempty"`
	SendState       string   `json:"send_state,omitempty"`
	AssistStatus    string   `json:"assist_status,omitempty"`
	AssistErrorCode string   `json:"assist_error_code,omitempty"`
	AutoRetryAt     string   `json:"auto_retry_at,omitempty"`
}

type samplingProgress struct {
	Available          bool                      `json:"available"`
	Message            string                    `json:"message,omitempty"`
	BatchID            string                    `json:"batch_id,omitempty"`
	Status             string                    `json:"status,omitempty"`
	UpdatedAt          string                    `json:"updated_at,omitempty"`
	Total              int                       `json:"total"`
	Counts             map[string]int            `json:"counts,omitempty"`
	BySite             map[string]map[string]int `json:"by_site,omitempty"`
	ByRound            map[string]samplingRound  `json:"by_round,omitempty"`
	CurrentRound       *int                      `json:"current_round"`
	CurrentRoundBySite map[string]map[string]int `json:"current_round_by_site,omitempty"`
	Concurrency        int                       `json:"concurrency"`
	SamplingStrategy   string                    `json:"sampling_strategy,omitempty"`
	RoundPolicy        string                    `json:"round_policy,omitempty"`
	MaxRound           *int                      `json:"max_round,omitempty"`
	SiteRounds         map[string]*int           `json:"site_rounds,omitempty"`
	AllowClarification bool                      `json:"allow_clarification_advance"`
	Processed          int                       `json:"processed"`
	ActiveSamples      []samplingSample          `json:"active_samples,omitempty"`
	QualityGaps        []samplingSample          `json:"quality_gaps,omitempty"`
	QualityGapCount    int                       `json:"quality_gap_count"`
}

func readSamplingJSON(path string, limit int64, out any) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	if info, err := f.Stat(); err != nil {
		return err
	} else if !info.Mode().IsRegular() || info.Size() > limit {
		return io.ErrUnexpectedEOF
	}
	return json.NewDecoder(io.LimitReader(f, limit)).Decode(out)
}

// Only administrator-configured files are read. Typed decoding prevents
// private config, prompts, paths or unknown fields from entering this response.
func samplingProgressPath() (string, error) {
	path := os.Getenv("GEO_SAMPLING_PROGRESS_FILE")
	if path == "" {
		config := os.Getenv("GEO_SAMPLING_NOTIFY_CONFIG")
		if config == "" {
			return "", errors.New("暂无运行中的 Excel 采样批次")
		}
		var settings struct {
			BatchDir string `json:"batch_dir"`
		}
		if readSamplingJSON(config, 64<<10, &settings) != nil || settings.BatchDir == "" {
			return "", errors.New("采样批次配置暂不可读")
		}
		path = filepath.Join(settings.BatchDir, "progress.json")
	}
	return path, nil
}

func (s *Server) manualSamplingProgress() samplingProgress {
	path, err := samplingProgressPath()
	if err != nil {
		return samplingProgress{Message: err.Error()}
	}
	var progress samplingProgress
	if readSamplingJSON(path, 1<<20, &progress) != nil || progress.BatchID == "" {
		return samplingProgress{Message: "采样进度暂不可读，请稍后刷新"}
	}
	progress.Available = true
	if len(progress.ActiveSamples) > 24 {
		progress.ActiveSamples = progress.ActiveSamples[:24]
	}
	if len(progress.QualityGaps) > 12 {
		progress.QualityGaps = progress.QualityGaps[:12]
	}
	if s.jobs == nil {
		return progress
	}
	for i := range progress.ActiveSamples {
		row := &progress.ActiveSamples[i]
		job, ok := s.jobs.Get(row.JobID)
		if !ok {
			continue
		}
		state, ok := job.ManualStates[row.Site]
		if !ok {
			continue
		}
		row.Status = job.Status
		row.Reason = state.Reason
		row.ErrorCode = state.ErrorCode
		row.SendState = state.SendState
		row.AssistStatus = state.AssistStatus
		row.AutoRetryAt = state.AutoRetryAt
		if state.AssistCallID != "" && !strings.ContainsAny(state.AssistCallID, "/\\.") {
			var call struct {
				ErrorCode        string `json:"error_code"`
				ProcessErrorCode string `json:"process_error_code"`
			}
			if readSamplingJSON(filepath.Join(filepath.Dir(s.jobs.DataDir), "cursor-calls", state.AssistCallID+".json"), 64<<10, &call) == nil {
				row.AssistErrorCode = call.ProcessErrorCode
				if row.AssistErrorCode == "" {
					row.AssistErrorCode = call.ErrorCode
				}
			}
		}
	}
	return progress
}

// The sampler replaces this workbook atomically. Serve the opened inode so
// a concurrent update cannot produce a download containing two file versions.
func (s *Server) adminSamplingExport(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "请使用 GET 下载采样结果"})
		return
	}
	path, err := samplingProgressPath()
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"detail": err.Error()})
		return
	}
	f, err := os.Open(filepath.Join(filepath.Dir(path), "采样结果.xlsx"))
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"detail": "当前批次尚无可导出的结果，请稍后重试"})
		return
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > 128<<20 {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"detail": "采样结果暂不可读，请稍后重试"})
		return
	}
	name := "GEO采样结果_" + info.ModTime().Format("20060102_150405") + ".xlsx"
	w.Header().Set("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name}))
	http.ServeContent(w, r, name, info.ModTime(), f)
}
