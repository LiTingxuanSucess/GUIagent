package jobs

import (
	"bufio"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// These are observations of the web account, independent of login and API quota.
// ResetAt is only populated from an explicit platform statement. NextProbeAt
// is a scheduling decision and never implies that platform quota has reset.
type AccountLimit struct {
	AccountID       string    `json:"account_id"`
	Site            string    `json:"site"`
	Kind            string    `json:"kind"`
	FirstDetectedAt time.Time `json:"first_detected_at"`
	LastDetectedAt  time.Time `json:"last_detected_at"`
	ResetAt         time.Time `json:"reset_at"`
	ResetPrecision  string    `json:"reset_precision"`
	NextProbeAt     time.Time `json:"next_probe_at"`
	RecoveredAt     time.Time `json:"recovered_at"`
	DetectionCount  int       `json:"detection_count"`
	Source          string    `json:"source"`
	Status          string    `json:"status"`
}

var platformBlockUntil = regexp.MustCompile(`\d{4}-\d{2}-\d{2} \d{2}:\d{2}`)
var platformQuotaReset = regexp.MustCompile(`(\d{1,2}-\d{1,2})\s*(?:重置|刷新|恢复)`)
var protectionLogLine = regexp.MustCompile(`^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}).*scrape failed job=\S+ site=(\S+) account=(\S+) elapsed_ms=\d+: (.*)$`)

func platformLimitDetails(message string, observed time.Time) (string, time.Time) {
	loc := time.FixedZone("CST", 8*3600)
	now := observed.In(loc)
	kind := "quota_exhausted"
	if strings.Contains(message, "禁言") {
		kind = "account_muted"
	}
	var reset time.Time
	if date := platformBlockUntil.FindString(message); date != "" {
		reset, _ = time.ParseInLocation("2006-01-02 15:04", date, loc)
	} else if strings.Contains(message, "次月1日零点刷新") {
		reset = time.Date(now.Year(), now.Month()+1, 1, 0, 0, 0, 0, loc)
	} else if date := platformQuotaReset.FindStringSubmatch(message); date != nil {
		parsed, err := time.ParseInLocation("2006-1-2", now.Format("2006")+"-"+date[1], loc)
		if err == nil {
			reset = parsed
			if !reset.After(now) {
				reset, _ = time.ParseInLocation("2006-1-2", now.AddDate(1, 0, 0).Format("2006")+"-"+date[1], loc)
			}
		}
	}
	if !reset.After(now) || reset.After(now.Add(366*24*time.Hour)) {
		reset = time.Time{}
	}
	return kind, reset
}

// Caller holds accountBlockMu, or is loading before workers start.
func (q *Queue) recordAccountLimitLocked(site, account, message string, at time.Time, source string) AccountLimit {
	if q.accountLimits == nil {
		q.accountLimits = make(map[string]AccountLimit)
	}
	key := internalSiteID(site) + "/" + account
	limit := q.accountLimits[key]
	if limit.AccountID == "" || !limit.RecoveredAt.IsZero() {
		limit = AccountLimit{AccountID: account, Site: internalSiteID(site), FirstDetectedAt: at, Source: source}
	}
	limit.LastDetectedAt, limit.RecoveredAt = at, time.Time{}
	limit.DetectionCount++
	kind, reset := platformLimitDetails(message, at)
	if message != "" {
		limit.Kind = kind
	}
	if limit.Kind == "" {
		limit.Kind = "quota_or_restriction"
	}
	if !reset.IsZero() {
		limit.ResetAt = reset
		limit.ResetPrecision = "minute"
		if platformQuotaReset.MatchString(message) {
			limit.ResetPrecision = "date"
		}
	}
	// Repeat refusals increase the cooldown, bounded at two hours by default.
	seconds := envInt("GEO_QUOTA_COOLDOWN_SEC", 1800, 60, 86400)
	maxSeconds := envInt("GEO_QUOTA_MAX_COOLDOWN_SEC", 7200, 60, 86400)
	if maxSeconds < seconds {
		maxSeconds = seconds
	}
	for n := 1; n < limit.DetectionCount && seconds < maxSeconds; n++ {
		seconds *= 2
		if seconds > maxSeconds {
			seconds = maxSeconds
		}
	}
	limit.NextProbeAt = at.Add(time.Duration(seconds) * time.Second)
	if limit.ResetAt.After(at) {
		limit.NextProbeAt = limit.ResetAt
	}
	q.accountLimits[key] = limit
	return limit
}

func (q *Queue) AccountLimits() []AccountLimit {
	q.accountBlockMu.Lock()
	defer q.accountBlockMu.Unlock()
	now := time.Now()
	rows := make([]AccountLimit, 0, len(q.accountLimits))
	for key, limit := range q.accountLimits {
		if until := q.accountBlocks[key]; until.After(limit.NextProbeAt) {
			limit.NextProbeAt = until
		}
		switch {
		case !limit.RecoveredAt.IsZero():
			limit.Status = "recovered"
		case limit.NextProbeAt.After(now):
			limit.Status = "blocked"
		default:
			limit.Status = "pending_recheck"
		}
		rows = append(rows, limit)
	}
	sort.Slice(rows, func(i, j int) bool {
		if (rows[i].Status == "recovered") != (rows[j].Status == "recovered") {
			return rows[j].Status == "recovered"
		}
		if !rows[i].LastDetectedAt.Equal(rows[j].LastDetectedAt) {
			return rows[i].LastDetectedAt.After(rows[j].LastDetectedAt)
		}
		return rows[i].Site+"/"+rows[i].AccountID < rows[j].Site+"/"+rows[j].AccountID
	})
	return rows
}

func (q *Queue) quotaBlocked(site, account string) bool {
	q.accountBlockMu.Lock()
	defer q.accountBlockMu.Unlock()
	limit, ok := q.accountLimits[internalSiteID(site)+"/"+account]
	return ok && limit.RecoveredAt.IsZero() && limit.NextProbeAt.After(time.Now())
}

// Migrate only real web attempts. A successful API fallback cannot close a web
// limit episode. Legacy blocks without dated evidence remain explicitly unknown.
func (q *Queue) backfillAccountLimits() {
	type event struct {
		site, account, message string
		at                     time.Time
		ok                     bool
	}
	events := []event{}
	for _, job := range q.jobs {
		for _, result := range job.Results {
			site := stringField(result, "site_id")
			if site == "" {
				site = stringField(result, "site")
			}
			for _, attempt := range cloneAttemptList(result["attempts"]) {
				if attempt["channel"] != "web" || attempt["skipped"] == true {
					continue
				}
				ok := attempt["ok"] == true
				if !ok && attempt["error_code"] != "platform_quota" {
					continue
				}
				account := stringField(attempt, "account_id")
				at, err := time.Parse(time.RFC3339Nano, stringField(attempt, "started_at"))
				if account == "" || site == "" || err != nil {
					continue
				}
				events = append(events, event{site: site, account: account, at: at, ok: ok})
			}
		}
	}
	sort.SliceStable(events, func(i, j int) bool { return events[i].at.Before(events[j].at) })
	for _, e := range events {
		key := internalSiteID(e.site) + "/" + e.account
		if e.ok {
			if limit, ok := q.accountLimits[key]; ok && limit.RecoveredAt.IsZero() {
				limit.RecoveredAt = e.at
				q.accountLimits[key] = limit
			}
		} else {
			q.recordAccountLimitLocked(e.site, e.account, "", e.at, "history")
		}
	}
	// Read only the fixed service log, never copy raw platform text to the API.
	// Old versions discarded reset hints from structured attempts.
	if f, err := os.Open(filepath.Join(q.GeoRoot, "server", "geo-server.out.log")); err == nil {
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 64*1024), 1024*1024)
		for scanner.Scan() {
			if !strings.Contains(scanner.Text(), "scrape failed") {
				continue
			}
			m := protectionLogLine.FindStringSubmatch(scanner.Text())
			if m == nil {
				continue
			}
			key := internalSiteID(m[2]) + "/" + m[3]
			limit, ok := q.accountLimits[key]
			if !ok || !limit.RecoveredAt.IsZero() {
				continue
			}
			at, err := time.ParseInLocation("2006/01/02 15:04:05", m[1], time.Local)
			if err != nil || at.Before(limit.FirstDetectedAt.Add(-time.Minute)) || at.After(limit.LastDetectedAt.Add(10*time.Minute)) {
				continue
			}
			kind, reset := platformLimitDetails(m[4], at)
			if kind == "account_muted" || !reset.IsZero() || strings.Contains(m[4], "额度已用") || strings.Contains(m[4], "用量已用完") {
				limit.Kind, limit.ResetAt = kind, reset
				limit.ResetPrecision = "minute"
				if platformQuotaReset.MatchString(m[4]) {
					limit.ResetPrecision = "date"
				}
				if reset.After(limit.NextProbeAt) {
					limit.NextProbeAt = reset
				}
				q.accountLimits[key] = limit
			}
		}
		f.Close()
	}
	if q.accountLimits == nil {
		q.accountLimits = make(map[string]AccountLimit)
	}
	for key, limit := range q.accountLimits {
		if !limit.RecoveredAt.IsZero() {
			continue
		}
		if until := q.accountBlocks[key]; until.After(limit.NextProbeAt) {
			limit.NextProbeAt = until
			q.accountLimits[key] = limit
		}
		if limit.NextProbeAt.After(time.Now()) && limit.NextProbeAt.After(q.accountBlocks[key]) {
			if q.accountBlocks == nil {
				q.accountBlocks = make(map[string]time.Time)
			}
			q.accountBlocks[key] = limit.NextProbeAt
		}
	}
}
