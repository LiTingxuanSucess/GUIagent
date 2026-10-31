package jobs

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
)

var cursorSessionIDPattern = regexp.MustCompile(`^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$`)

func cursorSessionPath(workspace string, call cursorCall) string {
	key := sha256.Sum256([]byte(call.AccountID + "\n" + call.Site + "\n" + call.TaskType + "\n" + os.Getenv("GEO_CURSOR_MODEL")))
	return filepath.Join(filepath.Dir(cursorCallDirectory(workspace)), "cursor-sessions", hex.EncodeToString(key[:])+".json")
}

func cursorArgsForCall(workspace, prompt string, call *cursorCall) []string {
	args := cursorCommandArgs(workspace, prompt)
	call.ResumedSessionID = ""
	raw, err := os.ReadFile(cursorSessionPath(workspace, *call))
	var previous struct {
		SessionID string `json:"session_id"`
	}
	if err == nil && json.Unmarshal(raw, &previous) == nil && cursorSessionIDPattern.MatchString(previous.SessionID) {
		call.ResumedSessionID = previous.SessionID
		args = append(args[:len(args)-1], "--resume", previous.SessionID,
			"这是新的执行轮次。历史 CDP、target、journal 和任务上下文均不得沿用；仅使用下面本轮提供的账号、任务和唯一 target，重新观察页面。\n"+prompt)
	}
	return args
}

// A model marker alone never makes a reusable session. Cache only a session
// whose page was independently verified, scoped to account, site and task type.
func rememberCursorSession(workspace string, call cursorCall) {
	path := cursorSessionPath(workspace, call)
	if call.Status == "success" && call.Verified != nil && *call.Verified && cursorSessionIDPattern.MatchString(call.SessionID) {
		if os.MkdirAll(filepath.Dir(path), 0o700) != nil {
			return
		}
		raw, _ := json.Marshal(map[string]string{"session_id": call.SessionID, "call_id": call.ID})
		file, err := os.CreateTemp(filepath.Dir(path), ".session-*")
		if err != nil {
			return
		}
		defer os.Remove(file.Name())
		_, err = file.Write(raw)
		closeErr := file.Close()
		if err == nil && closeErr == nil {
			_ = os.Rename(file.Name(), path)
		}
	} else if call.Status == "failed" && call.ErrorCode != "cli_canceled" && call.ResumedSessionID != "" {
		raw, _ := os.ReadFile(path)
		var previous struct {
			SessionID string `json:"session_id"`
		}
		if json.Unmarshal(raw, &previous) == nil && previous.SessionID == call.ResumedSessionID {
			_ = os.Remove(path)
		}
	}
}

func (out *cursorOutput) session() string {
	out.mu.Lock()
	defer out.mu.Unlock()
	// A process may exit after a final JSON event without a newline.
	out.captureSessionEvent(out.stream)
	return out.sessionID
}

// The caller holds out.mu; only CLI protocol metadata supplies a session ID.
func (out *cursorOutput) captureSessionEvent(raw []byte) {
	var event struct {
		Type      string `json:"type"`
		Subtype   string `json:"subtype"`
		SessionID string `json:"session_id"`
	}
	if json.Unmarshal(raw, &event) == nil && (event.Type == "result" || event.Type == "system" && event.Subtype == "init") && cursorSessionIDPattern.MatchString(event.SessionID) {
		out.sessionID = event.SessionID
	}
}
