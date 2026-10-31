package jobs

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"time"
)

var cursorBuildID = "cursor-recovery-v2"

func cursorConfigHash() string {
	sum := sha256.Sum256([]byte(strings.Join([]string{os.Getenv("GEO_CURSOR_MODEL"), os.Getenv("GEO_CURSOR_RECOVERY_TIMEOUT_SEC"), os.Getenv("GEO_CURSOR_SILENCE_SEC"), os.Getenv("GEO_CURSOR_STARTUP_SEC"), os.Getenv("GEO_CURSOR_CONCURRENCY"), os.Getenv("GEO_CURSOR_MAX_TOOLS")}, "\n")))
	return hex.EncodeToString(sum[:8])
}

func cursorFalse() *bool { v := false; return &v }

// Called with the output mutex held. User/assistant/page text is never used
// as evidence of a provider outage, and no tool arguments are retained.
func (out *cursorOutput) captureTelemetry(raw []byte) {
	var event struct {
		Type    string          `json:"type"`
		Subtype string          `json:"subtype"`
		IsError bool            `json:"is_error"`
		Result  string          `json:"result"`
		Model   string          `json:"model"`
		Message json.RawMessage `json:"error"`
	}
	if json.Unmarshal(raw, &event) != nil {
		if !strings.HasPrefix(strings.TrimSpace(string(raw)), "{") {
			if code := cursorProviderError(string(raw)); code != "" {
				out.providerError = code
			}
		}
		return
	}
	if event.Type == "system" && event.Subtype == "init" && len(event.Model) < 100 {
		out.model = event.Model
	}
	if event.Type == "result" && event.IsError || event.Type == "error" {
		if code := cursorProviderError(event.Result + " " + string(event.Message)); code != "" {
			out.providerError = code
		}
	}
	if event.Type == "result" {
		out.terminalResult = true
		out.providerResponded = !event.IsError
	}
	if event.Type == "tool_call" {
		if event.Subtype == "started" {
			out.toolCalls++
			out.activeTools++
			if out.firstTool.IsZero() {
				out.firstTool = time.Now()
			}
		}
		if event.Subtype == "completed" && out.activeTools > 0 {
			out.activeTools--
		}
	}
}

func (out *cursorOutput) providerCode() string {
	out.mu.Lock()
	defer out.mu.Unlock()
	out.captureTelemetry(out.stream)
	return out.providerError
}

func (out *cursorOutput) hasProviderResponse() bool {
	if out == nil {
		return false
	}
	out.mu.Lock()
	defer out.mu.Unlock()
	return out.providerResponded && out.providerError == ""
}

func (out *cursorOutput) copyTelemetry(call *cursorCall) {
	out.mu.Lock()
	defer out.mu.Unlock()
	call.ToolCalls, call.Model = out.toolCalls, out.model
	if call.StageTimings == nil {
		call.StageTimings = map[string]int64{}
	}
	if !out.firstWrite.IsZero() {
		call.StageTimings["cli_first_output_ms"] = max(int64(0), out.firstWrite.Sub(call.StartedAt).Milliseconds())
	}
	if !out.firstTool.IsZero() {
		call.StageTimings["cli_first_tool_ms"] = max(int64(0), out.firstTool.Sub(call.StartedAt).Milliseconds())
	}
}

func (out *cursorOutput) toolBudgetExceeded() bool {
	out.mu.Lock()
	defer out.mu.Unlock()
	return !out.terminalResult && out.activeTools == 0 && out.toolCalls >= envInt("GEO_CURSOR_MAX_TOOLS", 24, 4, 100)
}

func (q *Queue) cursorAssisted(callID string) bool {
	if callID == "" || strings.ContainsAny(callID, `/\`) {
		return false
	}
	raw, err := os.ReadFile(cursorCallDirectory(q.cursorWorkspace()) + "/" + callID + ".json")
	if err != nil {
		return false
	}
	var call cursorCall
	return json.Unmarshal(raw, &call) == nil && call.Assisted != nil && *call.Assisted
}
