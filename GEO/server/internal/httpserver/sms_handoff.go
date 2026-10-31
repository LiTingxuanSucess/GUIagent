package httpserver

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"geo-server/internal/sessions"
	"geo-server/internal/sms"
)

type smsHandoff struct {
	AccountID  string    `json:"account_id"`
	Site       string    `json:"site"`
	MessageKey string    `json:"message_key"`
	TargetID   string    `json:"target_id"`
	SentAt     float64   `json:"sent_at"`
	ReceivedAt time.Time `json:"received_at"`
	UpdatedAt  time.Time `json:"updated_at"`
	Stage      string    `json:"stage"`
}

type smsHandoffStore struct {
	mu          sync.Mutex
	rows        map[string]smsHandoff
	running     map[string]bool
	unavailable bool
}

func smsMessageKey(msg sms.Message) string {
	sum := sha256.Sum256([]byte(msg.RequestID + "\x00" + msg.Receiver + "\x00" + msg.Received + "\x00" + msg.Result))
	return hex.EncodeToString(sum[:])
}

func handoffKey(account, site string, sent float64) string {
	return account + "/" + site + "/" + strconv.FormatFloat(sent, 'f', -1, 64)
}

func (s *Server) handoffPath() string {
	if s.cfg == nil || s.cfg.Root == "" {
		return ""
	}
	root := strings.TrimSpace(os.Getenv("GEO_DATA_DIR"))
	if root == "" {
		root = filepath.Join(s.cfg.Root, "data")
	}
	return filepath.Join(root, "sms-handoffs.json")
}

// Caller holds smsHandoffs.mu. No phone, SMS text or OTP is stored here.
func (s *Server) saveHandoffsLocked() bool {
	path := s.handoffPath()
	if path == "" {
		return true
	}
	for key, row := range s.smsHandoffs.rows {
		if time.Since(row.UpdatedAt) > 7*24*time.Hour && !s.smsHandoffs.running[key] {
			delete(s.smsHandoffs.rows, key)
		}
	}
	raw, err := json.Marshal(s.smsHandoffs.rows)
	if err != nil || os.MkdirAll(filepath.Dir(path), 0700) != nil {
		return false
	}
	if os.WriteFile(path+".tmp", raw, 0600) != nil {
		return false
	}
	return os.Rename(path+".tmp", path) == nil
}

func pendingHandoff(stage string) bool {
	return stage == "matched" || stage == "waiting_page" || stage == "waiting_owner"
}

func (s *Server) loadSMSHandoffs() {
	s.smsHandoffs.rows = map[string]smsHandoff{}
	s.smsHandoffs.running = map[string]bool{}
	if path := s.handoffPath(); path != "" {
		if raw, err := os.ReadFile(path); err == nil {
			if json.Unmarshal(raw, &s.smsHandoffs.rows) != nil || s.smsHandoffs.rows == nil {
				s.smsHandoffs.rows = map[string]smsHandoff{}
				s.smsHandoffs.unavailable = true
				log.Printf("sms handoff state unreadable; automatic OTP submission suspended")
				return
			}
		}
	}
	changed := false
	for key, row := range s.smsHandoffs.rows {
		if row.Stage == "submitting" {
			changed = true
			row.Stage = "submission_unknown"
			row.UpdatedAt = time.Now()
			s.smsHandoffs.rows[key] = row
		}
	}
	if changed {
		s.saveHandoffsLocked()
	}
	// Only pre-submission handoffs may resume. A crash during submission is
	// ambiguous: retain evidence for an operator, never replay the OTP.
	if s.sess == nil || s.sms == nil {
		return
	}
	type pending struct {
		row smsHandoff
		msg sms.Message
	}
	resume := []pending{}
	for _, msg := range s.sms.Recent(0) {
		key := smsMessageKey(msg)
		for _, row := range s.smsHandoffs.rows {
			if row.MessageKey == key && pendingHandoff(row.Stage) {
				resume = append(resume, pending{row, msg})
			}
		}
	}
	for _, item := range resume {
		current := s.sess.SMSContext(item.row.AccountID, item.row.Site)
		if current.SentAt != item.row.SentAt || current.SentTargetID != item.row.TargetID || current.TargetID != item.row.TargetID {
			s.updateHandoff(handoffKey(item.row.AccountID, item.row.Site, item.row.SentAt), "target_or_generation_changed")
			continue
		}
		s.startSMSHandoff(item.row.AccountID, item.row.Site, item.msg)
	}
}

func (s *Server) updateHandoff(key, stage string) bool {
	s.smsHandoffs.mu.Lock()
	if s.smsHandoffs.unavailable {
		s.smsHandoffs.mu.Unlock()
		return false
	}
	defer s.smsHandoffs.mu.Unlock()
	row, ok := s.smsHandoffs.rows[key]
	if !ok {
		return false
	}
	row.Stage, row.UpdatedAt = stage, time.Now()
	s.smsHandoffs.rows[key] = row
	return s.saveHandoffsLocked()
}

func (s *Server) startSMSHandoff(account, site string, msg sms.Message) {
	if signedSite := sms.SiteFromText(msg.Result); signedSite != "" && signedSite != site {
		return
	}
	matchedAccount := false
	for _, a := range s.sess.List() {
		if a != nil && a.Enabled && a.ID == account && sms.PhoneMatch(msg.Receiver, a.Phone) {
			matchedAccount = true
			break
		}
	}
	if !matchedAccount {
		return
	}
	received, err := time.ParseInLocation("2006-01-02T15:04:05", msg.Received, time.Local)
	if err != nil || msg.Code == "" {
		return
	}
	current := s.sess.SMSContext(account, site)
	key := handoffKey(account, site, current.SentAt)
	s.smsHandoffs.mu.Lock()
	if s.smsHandoffs.unavailable {
		s.smsHandoffs.mu.Unlock()
		return
	}
	if s.smsHandoffs.rows == nil {
		s.smsHandoffs.rows = map[string]smsHandoff{}
	}
	if s.smsHandoffs.running == nil {
		s.smsHandoffs.running = map[string]bool{}
	}
	row, exists := s.smsHandoffs.rows[key]
	if s.smsHandoffs.running[key] || exists && (!pendingHandoff(row.Stage) || row.MessageKey != smsMessageKey(msg)) {
		s.smsHandoffs.mu.Unlock()
		return
	}
	if !exists {
		row = smsHandoff{AccountID: account, Site: site, MessageKey: smsMessageKey(msg), TargetID: current.SentTargetID, SentAt: current.SentAt, ReceivedAt: received, UpdatedAt: time.Now(), Stage: "matched"}
	}
	switch {
	case current.SentAt <= 0:
		row.Stage = "send_unconfirmed"
	case !smsMatchesSend(current.SentAt, received):
		row.Stage = "stale_generation"
	case time.Since(received) > 3*time.Minute || received.After(time.Now().Add(time.Minute)):
		row.Stage = "expired"
	case row.TargetID == "":
		row.Stage = "target_missing"
	}
	s.smsHandoffs.rows[key] = row
	if !s.saveHandoffsLocked() || !pendingHandoff(row.Stage) {
		s.smsHandoffs.mu.Unlock()
		return
	}
	s.smsHandoffs.running[key] = true
	s.smsHandoffs.mu.Unlock()
	go s.runSMSHandoff(key, row, msg.Code)
}

func (s *Server) runSMSHandoff(key string, row smsHandoff, code string) {
	defer func() { s.smsHandoffs.mu.Lock(); delete(s.smsHandoffs.running, key); s.smsHandoffs.mu.Unlock() }()
	deadline := row.ReceivedAt.Add(3 * time.Minute)
	for time.Now().Before(deadline) {
		current := s.sess.SMSContext(row.AccountID, row.Site)
		if current.SentAt != row.SentAt {
			s.updateHandoff(key, "stale_generation")
			return
		}
		if current.TargetID != "" && current.TargetID != row.TargetID {
			s.updateHandoff(key, "target_changed")
			return
		}
		if current.Status == "logged_in" {
			s.updateHandoff(key, "verified")
			return
		}
		if current.TargetID == "" || current.Status != "waiting_sms" {
			s.updateHandoff(key, "waiting_page")
		} else {
			// Persist the intent before any browser action. If this process
			// crashes now the next process reports uncertainty instead of replay.
			if !s.updateHandoff(key, "submitting") {
				return
			}
			state, err := s.sess.SubmitCallbackCode(row.AccountID, row.Site, code, sessions.SMSCallbackContext{SentAt: row.SentAt, TargetID: row.TargetID})
			switch {
			case errors.Is(err, sessions.ErrRecoverySMSNotReady), errors.Is(err, sessions.ErrExistingRecoveryBusy):
				s.updateHandoff(key, "waiting_owner")
			case errors.Is(err, sessions.ErrSMSGenerationChanged):
				s.updateHandoff(key, "target_or_generation_changed")
				return
			case errors.Is(err, sessions.ErrExistingRecoveryMissing):
				s.updateHandoff(key, "target_missing")
				return
			case err != nil:
				s.updateHandoff(key, "submit_failed")
				return
			case state != nil && state.Status == "logged_in":
				s.updateHandoff(key, "verified")
				return
			default:
				s.updateHandoff(key, "submitted_unconfirmed")
				return
			}
		}
		time.Sleep(5 * time.Second)
	}
	s.updateHandoff(key, "expired")
}

func (s *Server) monitorHandoff(account, site string, sent float64) (string, string) {
	s.smsHandoffs.mu.Lock()
	defer s.smsHandoffs.mu.Unlock()
	row, ok := s.smsHandoffs.rows[handoffKey(account, site, sent)]
	if !ok {
		return "", ""
	}
	return row.Stage, row.UpdatedAt.Format(time.RFC3339)
}

// Interrupted login state may be reset to unknown on pool startup. Keep the
// latest unresolved handoff visible even when that reset cleared LastSendAt.
func (s *Server) appendInterruptedHandoffActions(rows []monitorAction, accounts []*sessions.Account) []monitorAction {
	s.smsHandoffs.mu.Lock()
	latest := map[string]smsHandoff{}
	for _, row := range s.smsHandoffs.rows {
		key := row.AccountID + "/" + row.Site
		if old, ok := latest[key]; !ok || row.ReceivedAt.After(old.ReceivedAt) {
			latest[key] = row
		}
	}
	s.smsHandoffs.mu.Unlock()
	for _, a := range accounts {
		if a == nil || !a.Enabled {
			continue
		}
		for site, state := range a.Sites {
			row, ok := latest[a.ID+"/"+site]
			if !ok || row.Stage == "verified" || state.Status == "logged_in" || time.Since(row.UpdatedAt) > 7*24*time.Hour || state.LastSendAt > row.SentAt {
				continue
			}
			if loggedIn, err := time.Parse(time.RFC3339Nano, state.LastLoginAt); err == nil && loggedIn.After(row.ReceivedAt) {
				continue
			}
			found := false
			for i := range rows {
				if rows[i].AccountID == a.ID && rows[i].Site == site {
					if rows[i].HandoffStage == "" {
						rows[i].HandoffStage = row.Stage
						rows[i].HandoffUpdatedAt = row.UpdatedAt.Format(time.RFC3339)
					}
					found = true
					break
				}
			}
			if found {
				continue
			}
			label := "账号"
			if len(a.Phone) >= 4 {
				label = "手机尾号 " + a.Phone[len(a.Phone)-4:]
			}
			rows = append(rows, monitorAction{AccountID: a.ID, AccountLabel: label, Site: site, Status: state.Status, Kind: "manual",
				Reason: "短信接续记录尚未确认完成", NextAction: "检查账号与原登录页面；确认提交结果后再操作",
				SMSState: "received_unconfirmed", SentAt: row.SentAt, ReceivedAt: float64(row.ReceivedAt.Unix()), UpdatedAt: state.UpdatedAt,
				HandoffStage: row.Stage, HandoffUpdatedAt: row.UpdatedAt.Format(time.RFC3339)})
		}
	}
	return rows
}
