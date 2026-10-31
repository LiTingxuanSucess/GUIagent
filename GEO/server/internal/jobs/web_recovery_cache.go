package jobs

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

type recoveryFailureCache struct {
	fingerprint string
	expires     time.Time
	callID      string
	attempts    int
	windowUntil time.Time
}

func recoveryPageFingerprint(probe map[string]any) string {
	state := map[string]any{}
	for _, key := range []string{"challenge_visible", "logged_out", "waiting_sms", "account_marker", "editor_present", "editor_ready"} {
		state[key] = probe[key] == true
	}
	if signature, ok := probe["challenge_signature"].(string); ok {
		state["challenge_signature"] = signature
	}
	raw, _ := json.Marshal(state)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func recoveryFailureKey(job, site, account, target string) string {
	return internalSiteID(site) + "\x00" + account
}

type recoveryFailureDisk struct {
	Fingerprint string    `json:"fingerprint"`
	Expires     time.Time `json:"expires"`
	CallID      string    `json:"call_id"`
	Attempts    int       `json:"attempts"`
	WindowUntil time.Time `json:"window_until"`
}

func (q *Queue) recoveryFailurePath(key string) string {
	if q.GeoRoot == "" && q.DataDir == "" && os.Getenv("GEO_DATA_DIR") == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(key))
	return filepath.Join(filepath.Dir(cursorCallDirectory(q.cursorWorkspace())), "cursor-retry-state", hex.EncodeToString(sum[:])+".json")
}

func (q *Queue) loadRecoveryFailure(key string) (recoveryFailureCache, bool) {
	if cached, ok := q.recoveryFailures[key]; ok {
		return cached, true
	}
	path := q.recoveryFailurePath(key)
	if path == "" {
		return recoveryFailureCache{}, false
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return recoveryFailureCache{}, false
	}
	var d recoveryFailureDisk
	if json.Unmarshal(raw, &d) != nil {
		return recoveryFailureCache{}, false
	}
	return recoveryFailureCache{fingerprint: d.Fingerprint, expires: d.Expires, callID: d.CallID, attempts: d.Attempts, windowUntil: d.WindowUntil}, true
}

func (q *Queue) persistRecoveryFailure(key string, v recoveryFailureCache) {
	path := q.recoveryFailurePath(key)
	if path == "" {
		return
	}
	if os.MkdirAll(filepath.Dir(path), 0700) != nil {
		return
	}
	raw, _ := json.Marshal(recoveryFailureDisk{v.fingerprint, v.expires, v.callID, v.attempts, v.windowUntil})
	f, err := os.CreateTemp(filepath.Dir(path), ".retry-")
	if err != nil {
		return
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(raw); err != nil {
		f.Close()
		return
	}
	if f.Close() != nil {
		return
	}
	_ = os.Rename(f.Name(), path)
}

func (q *Queue) recentRecoveryFailure(key string, probe map[string]any, now time.Time) (recoveryFailureCache, bool) {
	q.recoveryFailureMu.Lock()
	defer q.recoveryFailureMu.Unlock()
	previous, found := q.loadRecoveryFailure(key)
	if !found {
		return recoveryFailureCache{}, false
	}
	if !now.Before(previous.windowUntil) || previous.fingerprint != recoveryPageFingerprint(probe) {
		delete(q.recoveryFailures, key)
		if path := q.recoveryFailurePath(key); path != "" {
			_ = os.Remove(path)
		}
		return recoveryFailureCache{}, false
	}
	if !now.Before(previous.expires) {
		return recoveryFailureCache{}, false
	}
	return previous, true
}

func (q *Queue) rememberRecoveryFailure(key string, probe map[string]any, result cursorRecoveryResult, now time.Time) {
	if probe["context_valid"] != true || probe["ready"] == true || result.Status == "skipped" || result.CallID == "" {
		return
	}
	switch result.ErrorCode {
	case "no_progress", "cli_timeout", "cli_no_output", "cli_verification_failed", "sms_required", "operator_required", "target_missing":
	default:
		return
	}
	q.recoveryFailureMu.Lock()
	defer q.recoveryFailureMu.Unlock()
	if q.recoveryFailures == nil {
		q.recoveryFailures = map[string]recoveryFailureCache{}
	}
	for key, previous := range q.recoveryFailures {
		if !now.Before(previous.windowUntil) {
			delete(q.recoveryFailures, key)
		}
	}
	previous, found := q.loadRecoveryFailure(key)
	fingerprint := recoveryPageFingerprint(probe)
	if !found || previous.fingerprint != fingerprint || !now.Before(previous.windowUntil) {
		previous = recoveryFailureCache{windowUntil: now.Add(10 * time.Minute)}
	}
	previous.fingerprint, previous.callID = fingerprint, result.CallID
	previous.attempts++
	previous.expires = now.Add(time.Duration(envInt("GEO_CURSOR_RETRY_COOLDOWN_SEC", 120, 1, 600)) * time.Second)
	if previous.attempts >= 2 {
		previous.expires = previous.windowUntil
	}
	q.recoveryFailures[key] = previous
	q.persistRecoveryFailure(key, previous)
}
