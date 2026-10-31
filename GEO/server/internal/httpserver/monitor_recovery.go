package httpserver

import (
	"encoding/json"
	"math"
	"regexp"
)

// Recovery DTOs accept public metadata only. Optional malformed evidence is
// ignored instead of preventing access to the complete historical index.
var monitorRecoveryID = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,160}$`)
var monitorRecoveryAccount = regexp.MustCompile(`^acct_[a-zA-Z0-9_-]{1,59}$`)
var monitorRecoveryError = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
var monitorRecoveryLabel = regexp.MustCompile(`^手机尾号 [0-9]{4}$`)

type monitorCLIPhase string

func (phase *monitorCLIPhase) UnmarshalJSON(raw []byte) error {
	*phase = ""
	var value string
	_ = json.Unmarshal(raw, &value)
	switch value {
	case "running", "candidate", "verify", "resume", "completed":
		*phase = monitorCLIPhase(value)
	}
	return nil
}

func recoveryText(fields map[string]json.RawMessage, key string, pattern *regexp.Regexp) string {
	var value string
	if json.Unmarshal(fields[key], &value) == nil && pattern.MatchString(value) {
		return value
	}
	return ""
}

func recoveryNumber(fields map[string]json.RawMessage, key string) float64 {
	var value float64
	if json.Unmarshal(fields[key], &value) != nil || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
		return 0
	}
	return value
}

func recoveryBool(fields map[string]json.RawMessage, key string) *bool {
	var value bool
	if string(fields[key]) == "null" || json.Unmarshal(fields[key], &value) != nil {
		return nil
	}
	return &value
}

type monitorWebRecovery struct {
	Status      string  `json:"status"`
	Attempts    float64 `json:"attempts"`
	CLICallID   string  `json:"cli_call_id,omitempty"`
	ErrorCode   string  `json:"error_code,omitempty"`
	Verified    *bool   `json:"verified"`
	Resumed     *bool   `json:"resumed"`
	CLIAssisted *bool   `json:"cli_assisted"`
	ElapsedMS   float64 `json:"elapsed_ms"`
}

func (recovery *monitorWebRecovery) UnmarshalJSON(raw []byte) error {
	*recovery = monitorWebRecovery{}
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil {
		return nil
	}
	var status string
	_ = json.Unmarshal(fields["status"], &status)
	switch status {
	case "succeeded", "failed", "cancelled", "skipped":
	default:
		return nil
	}
	recovery.Status = status
	recovery.Attempts = math.Floor(recoveryNumber(fields, "attempts"))
	recovery.CLICallID = recoveryText(fields, "cli_call_id", monitorRecoveryID)
	recovery.ErrorCode = recoveryText(fields, "error_code", monitorRecoveryError)
	recovery.Verified = recoveryBool(fields, "verified")
	recovery.Resumed = recoveryBool(fields, "resumed")
	recovery.CLIAssisted = recoveryBool(fields, "cli_assisted")
	recovery.ElapsedMS = recoveryNumber(fields, "elapsed_ms")
	return nil
}

type monitorRecoveryState struct {
	Phase        string  `json:"phase"`
	AccountID    string  `json:"account_id"`
	AccountLabel string  `json:"account_label"`
	CLICallID    string  `json:"cli_call_id,omitempty"`
	ErrorCode    string  `json:"error_code,omitempty"`
	UpdatedAt    float64 `json:"updated_at"`
}

type monitorRecoveryStates map[string]monitorRecoveryState

func (states *monitorRecoveryStates) UnmarshalJSON(raw []byte) error {
	*states = nil
	var sites map[string]json.RawMessage
	if json.Unmarshal(raw, &sites) != nil {
		return nil
	}
	clean := monitorRecoveryStates{}
	for site, encoded := range sites {
		if site == "yiyan" {
			site = "wenxinyiyan"
		}
		switch site {
		case "doubao", "yuanbao", "kimi", "qianwen", "deepseek", "wenxinyiyan":
		default:
			continue
		}
		var fields map[string]json.RawMessage
		if json.Unmarshal(encoded, &fields) != nil {
			continue
		}
		var phase string
		_ = json.Unmarshal(fields["phase"], &phase)
		switch phase {
		case "waiting_cli", "waiting_sms", "verifying", "resuming", "succeeded", "failed":
		default:
			continue
		}
		account := recoveryText(fields, "account_id", monitorRecoveryAccount)
		label := recoveryText(fields, "account_label", monitorRecoveryLabel)
		if label == "" {
			label = account
		}
		clean[site] = monitorRecoveryState{Phase: phase, AccountID: account, AccountLabel: label,
			CLICallID: recoveryText(fields, "cli_call_id", monitorRecoveryID), ErrorCode: recoveryText(fields, "error_code", monitorRecoveryError), UpdatedAt: recoveryNumber(fields, "updated_at")}
	}
	if len(clean) > 0 {
		*states = clean
	}
	return nil
}
