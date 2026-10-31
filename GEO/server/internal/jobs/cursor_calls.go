package jobs

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Cursor work restores browser sessions independently of the answer pipeline.
// Persist outcomes separately so a completed answer cannot hide a failed repair.
type cursorCall struct {
	ID               string           `json:"id"`
	JobID            string           `json:"job_id"`
	Site             string           `json:"site"`
	AccountID        string           `json:"account_id"`
	TaskType         string           `json:"task_type"`
	SessionID        string           `json:"session_id,omitempty"`
	ResumedSessionID string           `json:"resumed_session_id,omitempty"`
	StartedAt        time.Time        `json:"started_at"`
	FinishedAt       *time.Time       `json:"finished_at,omitempty"`
	ElapsedMS        int64            `json:"elapsed_ms"`
	OK               *bool            `json:"ok"`
	Status           string           `json:"status"`
	ErrorCode        string           `json:"error_code"`
	Verified         *bool            `json:"verified,omitempty"`
	ResumeOK         *bool            `json:"resume_ok,omitempty"`
	Phase            string           `json:"phase,omitempty"`
	ProbeReason      string           `json:"probe_reason,omitempty"`
	ProbeErrorCode   string           `json:"probe_error_code,omitempty"`
	ProbeDetail      string           `json:"probe_detail,omitempty"`
	OwnerPID         int              `json:"owner_pid,omitempty"`
	OwnerStart       string           `json:"owner_start,omitempty"`
	ProcessPID       int              `json:"process_pid,omitempty"`
	ProcessStart     string           `json:"process_start,omitempty"`
	ProcessGroup     int              `json:"process_group,omitempty"`
	BootID           string           `json:"boot_id,omitempty"`
	SchemaVersion    int              `json:"schema_version,omitempty"`
	BuildID          string           `json:"build_id,omitempty"`
	ConfigHash       string           `json:"config_hash,omitempty"`
	ProcessStatus    string           `json:"process_status,omitempty"`
	ProcessErrorCode string           `json:"process_error_code,omitempty"`
	RecoveryOrigin   string           `json:"recovery_origin,omitempty"`
	ResumeErrorCode  string           `json:"resume_error_code,omitempty"`
	StageTimings     map[string]int64 `json:"stage_timings,omitempty"`
	ToolCalls        int              `json:"tool_calls,omitempty"`
	Model            string           `json:"model,omitempty"`
	Assisted         *bool            `json:"cli_assisted,omitempty"`
	VerifiedAt       *time.Time       `json:"verified_at,omitempty"`
}

func newCursorCall(accountID, site, jobID string) cursorCall {
	var nonce [12]byte
	_, err := rand.Read(nonce[:])
	id := hex.EncodeToString(nonce[:])
	if err != nil {
		id = fmt.Sprintf("%d", time.Now().UnixNano())
	}
	call := cursorCall{ID: id, JobID: jobID, Site: site, AccountID: accountID, TaskType: "captcha_recovery", StartedAt: time.Now().UTC(), Status: "running"}
	call.SchemaVersion, call.BuildID, call.ConfigHash = 2, cursorBuildID, cursorConfigHash()
	owner, state := cursorProcess("/proc", os.Getpid())
	if boot := cursorBootID("/proc"); state == cursorProcessAlive && boot != "" {
		call.OwnerPID, call.OwnerStart, call.BootID = os.Getpid(), owner.start, boot
	}
	return call
}

type cursorProcessState uint8

const (
	cursorProcessUnknown cursorProcessState = iota
	cursorProcessAlive
	cursorProcessDead
)

type cursorProcessInfo struct {
	pid, parent, group int
	start              string
}

// A Linux start-time/boot tuple identifies an invocation without storing
// command arguments, browser context, credentials, or raw CLI output.
func cursorBootID(procRoot string) string {
	raw, err := os.ReadFile(filepath.Join(procRoot, "sys/kernel/random/boot_id"))
	if err != nil {
		return ""
	}
	boot := strings.ToLower(strings.TrimSpace(string(raw)))
	if !cursorValidBootID(boot) {
		return ""
	}
	return boot
}

func cursorValidBootID(boot string) bool {
	if len(boot) != 36 {
		return false
	}
	for i, char := range strings.ToLower(boot) {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if char != '-' {
				return false
			}
		} else if !((char >= '0' && char <= '9') || (char >= 'a' && char <= 'f')) {
			return false
		}
	}
	return true
}

func cursorProcess(procRoot string, pid int) (cursorProcessInfo, cursorProcessState) {
	info := cursorProcessInfo{pid: pid}
	if pid <= 0 {
		return info, cursorProcessUnknown
	}
	raw, err := os.ReadFile(filepath.Join(procRoot, strconv.Itoa(pid), "stat"))
	if errors.Is(err, os.ErrNotExist) {
		return info, cursorProcessDead
	}
	if err != nil {
		return info, cursorProcessUnknown
	}
	close := strings.LastIndexByte(string(raw), ')')
	if close < 0 {
		return info, cursorProcessUnknown
	}
	fields := strings.Fields(string(raw[close+1:]))
	if len(fields) < 20 {
		return info, cursorProcessUnknown
	}
	info.parent, err = strconv.Atoi(fields[1])
	if err != nil {
		return info, cursorProcessUnknown
	}
	info.group, err = strconv.Atoi(fields[2])
	if err != nil {
		return info, cursorProcessUnknown
	}
	if ticks, err := strconv.ParseUint(fields[19], 10, 64); err != nil || ticks == 0 {
		return info, cursorProcessUnknown
	}
	info.start = fields[19]
	if fields[0] == "Z" || fields[0] == "X" {
		return info, cursorProcessDead
	}
	switch fields[0] {
	case "R", "S", "D", "T", "t", "I", "W", "P":
		return info, cursorProcessAlive
	default:
		return info, cursorProcessUnknown
	}
}

func (call *cursorCall) bindProcess(pid int) {
	call.bindProcessAt("/proc", os.Getpid(), pid)
}

func (call *cursorCall) bindProcessAt(procRoot string, ownerPID, pid int) {
	// Even when /proc is unavailable, retain that a process actually started.
	// An incomplete binding must never fall back to legacy job-only evidence.
	call.ProcessPID = pid
	owner, ownerState := cursorProcess(procRoot, ownerPID)
	process, processState := cursorProcess(procRoot, pid)
	boot := cursorBootID(procRoot)
	if ownerState != cursorProcessAlive || processState == cursorProcessUnknown || process.start == "" ||
		boot == "" || call.OwnerPID != ownerPID || call.OwnerStart != owner.start || call.BootID != boot ||
		process.parent != ownerPID || process.group != pid {
		return
	}
	call.ProcessPID, call.ProcessStart, call.ProcessGroup = pid, process.start, process.group
}

func cursorOriginalProcessState(procRoot string, pid int, start string) cursorProcessState {
	current, state := cursorProcess(procRoot, pid)
	if state != cursorProcessUnknown && current.start != "" && current.start != start {
		return cursorProcessDead // PID reuse cannot keep the old invocation alive.
	}
	return state
}

// An orphaned CLI may outlive its Go owner. Neither owner death alone nor
// the loss of a group leader proves that all work in this invocation stopped.
func (call cursorCall) invocationState(procRoot string) cursorProcessState {
	if call.OwnerPID <= 0 || call.OwnerStart == "" || call.ProcessPID <= 0 || call.ProcessStart == "" ||
		call.ProcessGroup != call.ProcessPID || !cursorValidBootID(call.BootID) {
		return cursorProcessUnknown
	}
	for _, start := range []string{call.OwnerStart, call.ProcessStart} {
		if ticks, err := strconv.ParseUint(start, 10, 64); err != nil || ticks == 0 {
			return cursorProcessUnknown
		}
	}
	boot := cursorBootID(procRoot)
	if boot == "" {
		return cursorProcessUnknown
	}
	if boot != strings.ToLower(call.BootID) {
		return cursorProcessDead
	}
	owner := cursorOriginalProcessState(procRoot, call.OwnerPID, call.OwnerStart)
	process := cursorOriginalProcessState(procRoot, call.ProcessPID, call.ProcessStart)
	if owner == cursorProcessAlive || process == cursorProcessAlive {
		return cursorProcessAlive
	}
	if owner == cursorProcessUnknown || process == cursorProcessUnknown {
		return cursorProcessUnknown
	}
	entries, err := os.ReadDir(procRoot)
	if err != nil {
		return cursorProcessUnknown
	}
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid <= 0 {
			continue
		}
		member, state := cursorProcess(procRoot, pid)
		if state == cursorProcessUnknown {
			return cursorProcessUnknown
		}
		if state == cursorProcessAlive && member.group == call.ProcessGroup {
			return cursorProcessAlive
		}
	}
	return cursorProcessDead
}

func (call *cursorCall) finish(status, code string, ok *bool) {
	now := time.Now().UTC()
	call.FinishedAt, call.ElapsedMS = &now, now.Sub(call.StartedAt).Milliseconds()
	call.Status, call.ErrorCode, call.OK = status, code, ok
	if call.StageTimings == nil {
		call.StageTimings = map[string]int64{}
	}
	call.StageTimings["cli_execution_ms"] = call.ElapsedMS
	if call.ProcessStatus == "" {
		call.ProcessStatus, call.ProcessErrorCode = status, code
		if call.Phase == "candidate" {
			call.ProcessStatus = "candidate"
			call.ProcessErrorCode = ""
		}
	}
}

func cursorCallDirectory(workspace string) string {
	dir := strings.TrimSpace(os.Getenv("GEO_DATA_DIR"))
	if dir == "" {
		if root := strings.TrimSpace(os.Getenv("GEO_SERVER_ROOT")); root != "" {
			dir = filepath.Join(root, "data")
		} else {
			dir = filepath.Join(workspace, "server", "data")
		}
	}
	return filepath.Join(dir, "cursor-calls")
}

func (call cursorCall) save(workspace string) error {
	dir := cursorCallDirectory(workspace)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	raw, err := json.Marshal(call)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".cursor-call-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err = f.Write(raw); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, filepath.Join(dir, call.ID+".json")); err != nil {
		return err
	}
	rememberCursorSession(workspace, call)
	return nil
}

// cursorProbeDetail keeps only boolean / enum fields from an independent probe.
// Prompts, URLs and account text stay out of the dashboard record.
func cursorProbeDetail(probe map[string]any) string {
	if probe == nil {
		return ""
	}
	parts := make([]string, 0, 12)
	for _, key := range []string{"ready", "context_valid", "challenge_visible", "logged_out", "waiting_sms",
		"editor_ready", "account_marker", "authenticated", "prompt_matched", "target_match"} {
		if value, ok := probe[key]; ok {
			parts = append(parts, fmt.Sprintf("%s=%v", key, value))
		}
	}
	if reason := stringField(probe, "reason"); reason != "" {
		parts = append(parts, "reason="+reason)
	}
	return strings.Join(parts, " ")
}

// A CLI marker is only a candidate. The caller independently probes the exact
// browser target before confirming recovery, then records the resume outcome.
func (q *Queue) markCursorRecovery(callID string, verified, resumeOK bool, phase string, probe ...map[string]any) {
	if callID == "" || filepath.Base(callID) != callID || strings.ContainsAny(callID, `/\\`) {
		return
	}
	switch phase {
	case "verify", "resume", "completed":
	default:
		return
	}
	workspace := q.cursorWorkspace()
	raw, err := os.ReadFile(filepath.Join(cursorCallDirectory(workspace), callID+".json"))
	if err != nil {
		return
	}
	var call cursorCall
	if json.Unmarshal(raw, &call) != nil || call.ID != callID || call.TaskType != "web_recovery" {
		return
	}
	// No process was launched for skipped calls; a later browser probe must
	// not turn local admission/configuration skips into executed CLI failures.
	if call.Status == "skipped" {
		return
	}
	previousCode := call.ErrorCode
	eligible := call.ProcessStatus == "candidate" || call.ProcessStatus == "waiting_sms" || call.SchemaVersion == 0 && (call.Status == "unknown" && call.Phase == "candidate" || call.Status == "success")
	call.Verified, call.Phase = &verified, phase
	assisted := eligible && call.ToolCalls > 0 && verified
	call.Assisted = &assisted
	if phase == "verify" && verified {
		now := time.Now().UTC()
		call.VerifiedAt = &now
		if call.StageTimings == nil {
			call.StageTimings = map[string]int64{}
		}
		if call.FinishedAt != nil {
			call.StageTimings["cli_verify_ms"] = max(int64(0), now.Sub(*call.FinishedAt).Milliseconds())
		}
	}
	if phase == "completed" {
		call.ResumeOK = &resumeOK
		if call.VerifiedAt != nil {
			if call.StageTimings == nil {
				call.StageTimings = map[string]int64{}
			}
			call.StageTimings["cli_resume_ms"] = max(int64(0), time.Since(*call.VerifiedAt).Milliseconds())
		}
	}
	if !eligible {
		if verified {
			call.RecoveryOrigin = "external"
		}
		if len(probe) > 0 && probe[0] != nil {
			call.ProbeReason = stringField(probe[0], "reason")
			call.ProbeErrorCode = stringField(probe[0], "error_code")
			call.ProbeDetail = cursorProbeDetail(probe[0])
		}
		_ = call.save(workspace)
		return
	}
	call.OK = &verified
	call.Status, call.ErrorCode = "failed", "cli_verification_failed"
	if !verified && previousCode != "" && previousCode != "cli_result_unconfirmed" {
		call.ErrorCode = previousCode
	}
	if verified {
		call.Status, call.ErrorCode = "success", ""
		call.RecoveryOrigin = "cursor"
		if !assisted {
			call.RecoveryOrigin = "observed"
		}
	} else if call.ProcessStatus == "waiting_sms" {
		call.Status, call.ErrorCode, call.OK = "waiting_sms", "sms_required", nil
		if len(probe) > 0 && stringField(probe[0], "error_code") == "sms_timeout" {
			call.Status, call.ErrorCode = "action_required", "sms_timeout"
		}
	}
	if phase == "completed" {
		call.ResumeOK = &resumeOK
		if verified && !resumeOK {
			call.ErrorCode = "cli_resume_failed"
			call.ResumeErrorCode = "web_resume_failed"
			if len(probe) > 0 && stringField(probe[0], "error_code") != "" {
				call.ResumeErrorCode = stringField(probe[0], "error_code")
			}
		}
	}
	if len(probe) > 0 && probe[0] != nil {
		call.ProbeReason = stringField(probe[0], "reason")
		call.ProbeErrorCode = stringField(probe[0], "error_code")
		call.ProbeDetail = cursorProbeDetail(probe[0])
	}
	if err := call.save(workspace); err != nil {
		// Paths/output can carry private operating context; expose only a code.
		log.Printf("cursor recovery record update failed call=%s", callID)
	}
}

// Invocation fingerprints can identify killed CLI work even when its job was
// adopted by eventual delivery. No process is signaled and no job is changed.
// Unbound legacy records still require the prior explicit job interruption.
func (q *Queue) reconcileCursorRecoveries() {
	q.reconcileCursorRecoveriesAt("/proc")
}

func (q *Queue) reconcileCursorRecoveriesAt(procRoot string) {
	workspace := q.cursorWorkspace()
	paths, _ := filepath.Glob(filepath.Join(cursorCallDirectory(workspace), "*.json"))
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var call cursorCall
		if json.Unmarshal(raw, &call) != nil || (call.TaskType != "web_recovery" && call.TaskType != "captcha_recovery") || call.ID == "" || filepath.Base(path) != call.ID+".json" {
			continue
		}
		switch call.Phase {
		case "running", "candidate", "verify", "resume":
		case "":
			if call.TaskType != "captcha_recovery" || call.Status != "running" {
				continue
			}
		default:
			continue
		}
		if call.Status == "failed" || call.Status == "skipped" {
			continue // Preserve an already known terminal CLI outcome.
		}
		q.mu.Lock()
		job := q.jobs[call.JobID]
		interrupted := job != nil && job.Status == "failed" && job.ErrorCode == "service_interrupted"
		q.mu.Unlock()
		state := call.invocationState(procRoot)
		if state == cursorProcessAlive || state == cursorProcessUnknown &&
			(call.ProcessPID > 0 || call.TaskType != "web_recovery" || !interrupted) {
			continue
		}
		verified := call.Verified != nil && *call.Verified
		call.OK, call.Phase = &verified, "completed"
		call.Status, call.ErrorCode = "failed", "service_interrupted"
		if verified {
			resumed := false
			call.Status, call.ErrorCode, call.ResumeOK = "success", "cli_resume_interrupted", &resumed
		}
		if call.FinishedAt == nil {
			now := time.Now().UTC()
			call.FinishedAt, call.ElapsedMS = &now, 0
		}
		if err := call.save(workspace); err != nil {
			log.Printf("cursor recovery reconciliation failed call=%s", call.ID)
		}
	}
}

// Keep only a bounded tail for the final task marker. Raw CLI output stays in
// the private operation log and never enters the dashboard record.
type cursorOutput struct {
	mu                sync.Mutex
	tail              []byte
	lastWrite         time.Time
	stream            []byte
	sessionID         string
	providerError     string
	firstWrite        time.Time
	firstTool         time.Time
	toolCalls         int
	activeTools       int
	model             string
	terminalResult    bool
	providerResponded bool
}

func (out *cursorOutput) Write(p []byte) (int, error) {
	out.mu.Lock()
	defer out.mu.Unlock()
	if len(p) > 0 {
		out.lastWrite = time.Now()
		if out.firstWrite.IsZero() {
			out.firstWrite = out.lastWrite
		}
	}
	// Capture init metadata before the bounded result tail evicts it. Writes
	// can split JSON lines; discard oversized events without growing memory.
	for _, b := range p {
		if b == '\n' {
			out.captureTelemetry(out.stream)
			out.captureSessionEvent(out.stream)
			out.stream = out.stream[:0]
		} else if len(out.stream) < 64<<10 {
			out.stream = append(out.stream, b)
		}
	}
	const limit = 64 << 10
	if len(p) >= limit {
		out.tail = append(out.tail[:0], p[len(p)-limit:]...)
	} else {
		if n := len(out.tail) + len(p) - limit; n > 0 {
			out.tail = out.tail[n:]
		}
		out.tail = append(out.tail, p...)
	}
	return len(p), nil
}

func (out *cursorOutput) silenceRemaining(start, now time.Time) time.Duration {
	out.mu.Lock()
	last := out.lastWrite
	firstTool, terminal := out.firstTool, out.terminalResult
	out.mu.Unlock()
	if firstTool.IsZero() && !terminal && os.Getenv("GEO_CURSOR_STARTUP_SEC") != "" {
		return time.Duration(envInt("GEO_CURSOR_STARTUP_SEC", 75, 1, 180))*time.Second - now.Sub(start)
	}
	if last.IsZero() {
		last = start
	}
	return cursorSilenceTimeout() - now.Sub(last)
}

func (out *cursorOutput) outcome(processErr error) (status, code string, ok *bool) {
	if code = out.providerCode(); code != "" {
		return "failed", code, cursorFalse()
	}
	if processErr != nil {
		value := false
		return "failed", "cli_process_failed", &value
	}
	out.mu.Lock()
	defer out.mu.Unlock()
	lines := strings.Split(string(out.tail), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line == "captcha-ok" {
			value := true
			return "success", "", &value
		}
		if strings.HasPrefix(line, "captcha-stopped:") {
			value := false
			return "failed", "cli_task_stopped", &value
		}
	}
	return "unknown", "cli_result_unconfirmed", nil
}

func (out *cursorOutput) recoveryOutcome(processErr error) (status, code string, ok *bool) {
	if code = out.providerCode(); code != "" {
		return "failed", code, cursorFalse()
	}
	if processErr != nil {
		value := false
		return "failed", "cli_process_failed", &value
	}
	out.mu.Lock()
	defer out.mu.Unlock()
	text, failed, fromResult := cursorFinalText(out.tail)
	if failed {
		value := false
		return "failed", "cli_process_failed", &value
	}
	// The model often ends the result paragraph with the marker instead of
	// giving it a line of its own. Only the result event counts; an earlier
	// assistant quote of the same words is not a verdict.
	if fromResult {
		if marker := lastRecoveryMarker(text); marker != "" {
			if marker == "recovery-candidate" || marker == "captcha-ok" {
				return "candidate", "", nil
			}
			value := false
			code = recoveryStopCode(marker)
			if code == "sms_required" {
				return "waiting_sms", code, nil
			}
			if code == "operator_required" {
				return "action_required", code, nil
			}
			return "failed", code, &value
		}
	}
	lines := strings.Split(text, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line == "recovery-candidate" || line == "captcha-ok" {
			return "candidate", "", nil
		}
		if strings.HasPrefix(line, "recovery-stopped:") || strings.HasPrefix(line, "captcha-stopped:") {
			value := false
			code = recoveryStopCode(line)
			if code == "sms_required" {
				return "waiting_sms", code, nil
			}
			if code == "operator_required" {
				return "action_required", code, nil
			}
			return "failed", code, &value
		}
	}
	return "unknown", "cli_result_unconfirmed", nil
}

// stream-json prints progress events during the run, so the silence watchdog
// sees a live CLI. Only the final result event carries the task marker;
// assistant or thinking text may quote markers before the work is done.
// Plain text output (no result event) is read as-is.
var recoveryMarkerPattern = regexp.MustCompile(`recovery-candidate|captcha-ok|recovery-stopped:\s*[a-z_]+|captcha-stopped:\s*[a-z_]+`)

func lastRecoveryMarker(text string) string {
	found := recoveryMarkerPattern.FindAllString(text, -1)
	if len(found) == 0 {
		return ""
	}
	return strings.TrimSpace(found[len(found)-1])
}

func cursorFinalText(tail []byte) (text string, failed, fromResult bool) {
	lines := strings.Split(string(tail), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if !strings.HasPrefix(line, "{") {
			continue
		}
		var event struct {
			Type    string `json:"type"`
			Result  string `json:"result"`
			IsError bool   `json:"is_error"`
		}
		if json.Unmarshal([]byte(line), &event) != nil || event.Type != "result" {
			continue
		}
		return event.Result, event.IsError, true
	}
	return string(tail), false, false
}

// Keep the stable reason the SOP requires. A glued Chinese explanation must
// not hide sms_required or no_progress from the server handoff.
func recoveryStopCode(line string) string {
	reason := strings.TrimSpace(line)
	for _, prefix := range []string{"recovery-stopped:", "captcha-stopped:"} {
		if strings.HasPrefix(reason, prefix) {
			reason = strings.TrimSpace(strings.TrimPrefix(reason, prefix))
			break
		}
	}
	reason = strings.ToLower(reason)
	if strings.HasPrefix(strings.TrimSpace(line), "captcha-stopped:") {
		return "cli_task_stopped"
	}
	for _, code := range []string{"sms_required", "operator_required", "platform_quota", "target_missing", "no_progress"} {
		if strings.HasPrefix(reason, code) {
			return code
		}
	}
	return "cli_task_stopped"
}
