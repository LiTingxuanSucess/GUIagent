package jobs

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/procguard"
	"geo-server/internal/sessions"
)

type Job struct {
	ID                 string                    `json:"job_id"`
	RequestID          string                    `json:"request_id,omitempty"`
	Status             string                    `json:"status"`
	Prompt             string                    `json:"prompt"`
	Sites              []string                  `json:"sites"`
	CallbackURL        string                    `json:"callback_url,omitempty"`
	AK                 string                    `json:"ak,omitempty"`
	Results            []map[string]any          `json:"results,omitempty"`
	CompletedSites     int                       `json:"completed_sites"`
	Error              string                    `json:"error,omitempty"`
	ErrorCode          string                    `json:"error_code,omitempty"`
	Retryable          bool                      `json:"retryable,omitempty"`
	PollURL            string                    `json:"poll_url"`
	CreatedAt          string                    `json:"created_at"`
	UpdatedAt          string                    `json:"updated_at"`
	StartedAt          string                    `json:"started_at,omitempty"`
	FinishedAt         string                    `json:"finished_at,omitempty"`
	QueuePos           int                       `json:"queue_pos"`
	QueueLen           int                       `json:"queue_len"`
	QueuedMS           int64                     `json:"queued_ms"`
	ElapsedMS          int64                     `json:"elapsed_ms,omitempty"`
	TotalMS            int64                     `json:"total_ms,omitempty"`
	DeadlineAt         string                    `json:"deadline_at,omitempty"`
	DeadlinePolicy     string                    `json:"deadline_policy,omitempty"`
	ExecutionMode      string                    `json:"execution_mode,omitempty"`
	SiteExecutionModes map[string]string         `json:"site_execution_modes,omitempty"`
	DeliveryPolicy     string                    `json:"delivery_policy,omitempty"`
	RetryAt            string                    `json:"retry_at,omitempty"`
	RetryCount         int                       `json:"retry_count,omitempty"`
	WaitReason         string                    `json:"wait_reason,omitempty"`
	RecoveryStates     map[string]map[string]any `json:"recovery_states,omitempty"`
	ManualWorkflow     bool                      `json:"manual_workflow,omitempty"`
	ManualStates       map[string]ManualState    `json:"manual_states,omitempty"`
	CallbackStatus     string                    `json:"callback_status,omitempty"`
	CallbackError      string                    `json:"callback_error,omitempty"`
	CallbackMS         int64                     `json:"callback_ms,omitempty"`
	CallbackAttempts   int                       `json:"callback_attempts,omitempty"`
	// Sites listed here may have sent the question. Only jobs created with
	// DispatchTracked can treat an absent site as never sent.
	DispatchTracked bool     `json:"dispatch_tracked,omitempty"`
	WebDispatched   []string `json:"web_dispatched,omitempty"`

	createdUnix int64 `json:"-"`
}

type accountPool interface {
	AcquireContext(context.Context, string) (*sessions.Lease, error)
	AcquireForLoginContext(context.Context, string) (*sessions.Lease, error)
	AcquireContextExcept(context.Context, string, map[string]bool) (*sessions.Lease, error)
	LoggedInAccountIDs(string) []string
}

type Queue struct {
	GeoRoot string
	Python  string
	Script  string
	Sess    accountPool
	Keys    *auth.KeyStore

	DataDir               string
	routing               routingPolicyStore
	CallbackClient        *http.Client
	AllowPrivateCallbacks bool

	mu                sync.Mutex
	persistMu         sync.Mutex
	snapMu            sync.RWMutex
	snapPublishMu     sync.Mutex
	snaps             map[string]*jobSnap
	jobs              map[string]*Job
	requestIndex      map[string]string
	enqueued          map[string]bool
	activeRuns        map[string]bool
	runCancels        map[string]context.CancelFunc
	dispatchSem       chan struct{}
	activePlatforms   map[string]int
	recoverySem       chan struct{}
	ch                chan string
	siteSem           chan struct{}
	cloudSem          chan struct{}
	apiSem            chan struct{}
	diagnosticIDs     sync.Map
	siteGates         map[string]chan struct{}
	poolActive        map[string]int
	poolChanged       chan struct{}
	browserMu         sync.Mutex
	browserWaiters    []*browserWaiter
	browserPolling    bool
	browserLastSite   string
	browserLastJob    string
	fairTickets       map[fairAdmissionKey]*fairAdmissionTicket
	fairSequence      uint64
	fairWakePending   map[string]bool
	cooldownMu        sync.Mutex
	localBrowser      sync.RWMutex
	accountBlockMu    sync.Mutex
	accountBlocks     map[string]time.Time
	accountLimits     map[string]AccountLimit
	recoveryOwners    map[string]string
	recoveryFailureMu sync.Mutex
	recoveryFailures  map[string]recoveryFailureCache
	manualMu          sync.Mutex
}

func New(geoRoot string, sess *sessions.Pool, keys *auth.KeyStore, dataDir string) *Queue {
	workers := envInt("GEO_QUEUE_WORKERS", 3, 1, 8)
	conc := envInt("GEO_SITE_CONCURRENCY", 3, 1, 6)
	cloudConc := envInt("GEO_AGENTBAY_CONCURRENCY", 6, 1, 6)
	q := &Queue{
		GeoRoot:      geoRoot,
		Python:       "python3",
		Script:       filepath.Join(geoRoot, "server", "scripts", "run_scrape.py"),
		Keys:         keys,
		DataDir:      dataDir,
		jobs:         map[string]*Job{},
		requestIndex: map[string]string{},
		ch:           make(chan string, 128),
		siteSem:      make(chan struct{}, conc),
		cloudSem:     make(chan struct{}, cloudConc),
		apiSem:       make(chan struct{}, envInt("GEO_API_CONCURRENCY", 6, 1, 12)),
	}
	if sess != nil {
		q.Sess = sess
	}
	if envInt("GEO_PLATFORM_DISPATCH", 0, 0, 1) == 1 {
		q.dispatchSem = make(chan struct{}, envInt("GEO_DISPATCH_PENDING_MAX", 64, 3, 256))
		q.recoverySem = make(chan struct{}, 1)
	}
	n, queued, interrupted := q.load()
	q.restoreManualReservations()
	q.loadRoutingPolicy()
	q.loadAccountBlocks()
	q.restoreFairAdmissionTickets()
	q.reconcileCursorRecoveries()
	log.Printf("job queue workers=%d site_concurrency=%d cloud_concurrency=%d loaded=%d restored_queued=%d interrupted=%d", workers, conc, cloudConc, n, len(queued), len(interrupted))
	log.Printf("platform dispatch pending_max=%d recovery_concurrency=%d api_concurrency=%d", cap(q.dispatchSem), cap(q.recoverySem), cap(q.apiSem))
	for i := 0; i < workers; i++ {
		go q.loop()
	}
	go q.deliveryScheduler()
	go q.wakeFairResourceWaiters()
	go func() {
		for _, id := range queued {
			q.enqueueJob(id, time.Now())
		}
		for _, id := range interrupted {
			q.callback(id)
		}
	}()
	return q
}

func (q *Queue) Submit(prompt string, sites []string, callbackURL, ak, requestID string) (*Job, bool, error) {
	prompt = strings.TrimSpace(prompt)
	if prompt == "" {
		return nil, false, fmt.Errorf("prompt 不能为空")
	}
	if len([]rune(prompt)) > 20000 {
		return nil, false, fmt.Errorf("prompt 不能超过 20000 字符")
	}
	if len(sites) == 0 {
		return nil, false, fmt.Errorf("需要 site 或 sites")
	}
	for _, id := range sites {
		if _, ok := sessions.SiteByID(internalSiteID(id)); !ok {
			return nil, false, fmt.Errorf("未知站点: %s", id)
		}
	}
	requestID = strings.TrimSpace(requestID)
	if len(requestID) > 128 {
		return nil, false, fmt.Errorf("request_id 不能超过 128 字符")
	}
	if callbackURL != "" {
		if err := validateCallbackURL(callbackURL); err != nil {
			return nil, false, err
		}
	}
	q.mu.Lock()
	if q.requestIndex == nil {
		q.requestIndex = make(map[string]string)
		for _, existing := range q.jobs {
			if existing.RequestID != "" {
				q.requestIndex[requestKey(existing.AK, existing.RequestID)] = existing.ID
			}
		}
	}
	if requestID != "" {
		if existingID := q.requestIndex[requestKey(ak, requestID)]; existingID != "" {
			if existing := q.jobs[existingID]; existing != nil {
				if existing.Prompt != prompt || existing.CallbackURL != callbackURL || !sameStrings(existing.Sites, sites) {
					q.mu.Unlock()
					return nil, false, fmt.Errorf("request_id 已用于不同请求")
				}
				job := q.publicCopyLocked(existing, ak == "")
				q.mu.Unlock()
				q.publish(existing.ID)
				return job, false, nil
			}
		}
	}
	policy, err := q.RoutingPolicy()
	if err != nil {
		q.mu.Unlock()
		return nil, false, err
	}
	id, err := newID()
	if err != nil {
		q.mu.Unlock()
		return nil, false, err
	}
	now := time.Now()
	siteModes := policy.snapshotSites(sites)
	job := &Job{
		ID:                 id,
		RequestID:          requestID,
		Status:             "queued",
		Prompt:             prompt,
		Sites:              sites,
		CallbackURL:        callbackURL,
		AK:                 ak,
		PollURL:            "/api/v1/jobs/" + id,
		CreatedAt:          now.Format(time.RFC3339Nano),
		UpdatedAt:          now.Format(time.RFC3339Nano),
		DeadlinePolicy:     "web_first",
		ExecutionMode:      aggregateRoutingModes(siteModes),
		SiteExecutionModes: siteModes,
		DispatchTracked:    true,
		createdUnix:        now.UnixMilli(),
	}
	if requireDelivery() {
		job.DeliveryPolicy = "eventual"
	}
	if envInt("GEO_MANUAL_WORKFLOW", 0, 0, 1) == 1 {
		job.ManualWorkflow = true
		job.DeliveryPolicy = ""
		job.ExecutionMode = "manual_web"
		job.DeadlinePolicy = "manual_web"
		job.ManualStates = map[string]ManualState{}
		for _, site := range sites {
			job.ManualStates[site] = ManualState{Site: site, Status: "queued"}
		}
	}
	ahead := 0
	for _, j := range q.jobs {
		if j.Status == "queued" || j.Status == "running" {
			ahead++
		}
	}
	job.QueuePos = ahead
	job.QueueLen = ahead + 1
	q.jobs[id] = job
	if requestID != "" {
		q.requestIndex[requestKey(ak, requestID)] = id
	}
	saved := copyJob(job)
	out := q.publicCopyLocked(job, ak == "")
	q.mu.Unlock()
	q.publish(id)
	q.save(saved)
	// The worker buffer is finite. Block in the background instead of dropping
	// the job: the caller already has a queued id and can poll while it waits.
	go func() {
		q.enqueueJob(id, time.Now())
	}()
	return out, true, nil
}

func (q *Queue) Get(id string) (*Job, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	j, ok := q.jobs[id]
	if !ok {
		return nil, false
	}
	return q.copyWithQueueLocked(j), true
}

func (q *Queue) List() []*Job {
	return q.ListVisible("", true)
}

func (q *Queue) GetVisible(id, ak string, admin bool) (*Job, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	j := q.jobs[id]
	if j == nil {
		return nil, false
	}
	if !admin && (ak == "" || j.AK != ak) {
		return nil, false
	}
	return q.publicCopyLocked(j, admin), true
}

func (q *Queue) ListVisible(ak string, admin bool) []*Job {
	q.mu.Lock()
	defer q.mu.Unlock()
	out := make([]*Job, 0, len(q.jobs))
	for _, j := range q.jobs {
		if !admin && j.AK != ak {
			continue
		}
		cp := q.publicCopyLocked(j, admin)
		out = append(out, cp)
	}
	sort.Slice(out, func(i, k int) bool { return out[i].CreatedAt > out[k].CreatedAt })
	return out
}

func (q *Queue) publicCopyLocked(j *Job, admin bool) *Job {
	cp := q.copyWithQueueLocked(j)
	if !admin {
		cp.AK = ""
		stripInternalResults(cp)
	}
	return cp
}

func (q *Queue) copyWithQueueLocked(j *Job) *Job {
	cp := copyJob(j)
	active := make([]*Job, 0, len(q.jobs))
	for _, candidate := range q.jobs {
		if candidate.Status == "queued" || candidate.Status == "running" {
			active = append(active, candidate)
		}
	}
	sort.Slice(active, func(i, k int) bool {
		return active[i].CreatedAt < active[k].CreatedAt
	})
	cp.QueueLen = len(active)
	cp.QueuePos = 0
	if cp.Status == "queued" || cp.Status == "running" {
		for i, candidate := range active {
			if candidate.ID == cp.ID {
				cp.QueuePos = i
				break
			}
		}
	}
	return cp
}

type UsageRow struct {
	AK             string         `json:"ak,omitempty"`
	Name           string         `json:"name"`
	Jobs           int            `json:"jobs"`
	Succeeded      int            `json:"succeeded"`
	Partial        int            `json:"partial"`
	Failed         int            `json:"failed"`
	Cancelled      int            `json:"cancelled"`
	Queued         int            `json:"queued"`
	Running        int            `json:"running"`
	SiteCalls      map[string]int `json:"site_calls"`
	SiteOK         map[string]int `json:"site_ok"`
	TotalElapsedMS int64          `json:"total_elapsed_ms"`
	AvgElapsedMS   int64          `json:"avg_elapsed_ms"`
	LastAt         string         `json:"last_at,omitempty"`
}

func (q *Queue) Usage(filterAK string, admin bool) []UsageRow {
	q.mu.Lock()
	defer q.mu.Unlock()
	by := map[string]*UsageRow{}
	for _, j := range q.jobs {
		if !admin && j.AK != filterAK {
			continue
		}
		ak := j.AK
		if ak == "" {
			ak = "unknown"
		}
		row := by[ak]
		if row == nil {
			name := ak
			if q.Keys != nil {
				if k, ok := q.Keys.Lookup(ak); ok && k.Name != "" {
					name = k.Name
				}
			}
			row = &UsageRow{AK: ak, Name: name, SiteCalls: map[string]int{}, SiteOK: map[string]int{}}
			by[ak] = row
		}
		row.Jobs++
		switch j.Status {
		case "succeeded":
			row.Succeeded++
		case "partial":
			row.Partial++
		case "failed":
			row.Failed++
		case "cancelled":
			row.Cancelled++
		case "queued":
			row.Queued++
		case "running":
			row.Running++
		}
		row.TotalElapsedMS += j.ElapsedMS
		if j.CreatedAt > row.LastAt {
			row.LastAt = j.CreatedAt
		}
		for _, site := range j.Sites {
			row.SiteCalls[site]++
		}
		for _, r := range j.Results {
			sid, _ := r["site_id"].(string)
			ok, _ := r["ok"].(bool)
			if sid != "" && ok {
				row.SiteOK[sid]++
			}
		}
	}
	out := make([]UsageRow, 0, len(by))
	for _, row := range by {
		if !admin {
			row.AK = ""
		}
		done := row.Succeeded + row.Partial + row.Failed
		if done > 0 {
			row.AvgElapsedMS = row.TotalElapsedMS / int64(done)
		}
		out = append(out, *row)
	}
	sort.Slice(out, func(i, k int) bool { return out[i].Jobs > out[k].Jobs })
	return out
}

func (q *Queue) loop() {
	for id := range q.ch {
		id = q.prioritizeFairQueuedJob(id)
		if q.dispatchSem == nil {
			q.run(id)
			continue
		}
		// Coordinators only aggregate platform results. Their waiting must
		// not occupy one of the three browser execution permits.
		q.dispatchSem <- struct{}{}
		go func(id string) {
			defer func() { <-q.dispatchSem }()
			q.run(id)
		}(id)
	}
}

func (q *Queue) run(id string) {
	if !q.waitForDeployment(id) {
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	started := q.patchIf(id, func(j *Job) bool {
		delete(q.enqueued, id)
		if j.Status != "queued" || !deliveryDue(j, time.Now()) || q.activeRuns[id] {
			return false
		}
		if q.activeRuns == nil {
			q.activeRuns = make(map[string]bool)
		}
		q.activeRuns[id] = true
		if q.runCancels == nil {
			q.runCancels = make(map[string]context.CancelFunc)
		}
		q.runCancels[id] = cancel
		if q.dispatchSem == nil || j.ManualWorkflow {
			j.Status = "running"
		}
		if !j.ManualWorkflow {
			j.DeadlinePolicy = "web_first"
		}
		j.DeadlineAt = ""
		if (q.dispatchSem == nil || j.ManualWorkflow) && (j.StartedAt == "" || !j.ManualWorkflow && j.DeliveryPolicy != "eventual") {
			j.StartedAt = time.Now().Format(time.RFC3339)
		}
		return true
	})
	if !started {
		return
	}
	defer func() {
		q.mu.Lock()
		delete(q.activeRuns, id)
		delete(q.runCancels, id)
		delete(q.activePlatforms, id)
		cancelled := q.jobs[id] != nil && q.jobs[id].Status == "cancelled"
		q.mu.Unlock()
		q.finishFairRun(id)
		if cancelled {
			q.cleanupWebRecovery(id)
		}
	}()
	job, ok := q.Get(id)
	if !ok {
		return
	}
	queuedMS := time.Now().UnixMilli() - job.createdUnix
	if queuedMS < 0 {
		queuedMS = 0
	}
	runStart := time.Now()
	ctx = context.WithValue(ctx, jobExecutionKey{}, id)
	ctx = context.WithValue(ctx, routingModeContextKey{}, canonicalRoutingMode(job.ExecutionMode))
	ctx = context.WithValue(ctx, routingSitesContextKey{}, job.SiteExecutionModes)
	if job.ManualWorkflow {
		q.runManualWorkflow(ctx, job, runStart, queuedMS)
		return
	}
	if job.DeliveryPolicy == "eventual" {
		q.runEventualDelivery(ctx, job, runStart, queuedMS)
		return
	}
	results := make([]map[string]any, len(job.Sites))
	var wg sync.WaitGroup
	var resMu sync.Mutex
	for i, site := range job.Sites {
		i, site := i, site
		wg.Add(1)
		go func() {
			defer wg.Done()
			one := q.runOneSiteContext(ctx, site, job.Prompt, id, len(job.Sites))
			resMu.Lock()
			results[i] = one
			q.patch(id, func(j *Job) {
				j.Results = append(j.Results, one)
				j.CompletedSites = len(j.Results)
				j.QueuedMS = queuedMS
				j.ElapsedMS = time.Since(runStart).Milliseconds()
				j.TotalMS = time.Now().UnixMilli() - job.createdUnix
			})
			resMu.Unlock()
		}()
	}
	wg.Wait()
	status, errorCode, errorMessage, retryable := summarizeResults(results)
	if status == "failed" && ctx.Err() != nil {
		errorCode, errorMessage, retryable = "job_timeout", "请求总时间预算耗尽，请稍后重试", true
	}
	q.patch(id, func(j *Job) {
		j.Status = status
		j.Results = results
		j.QueuedMS = queuedMS
		j.ElapsedMS = time.Since(runStart).Milliseconds()
		j.TotalMS = time.Now().UnixMilli() - job.createdUnix
		j.FinishedAt = time.Now().Format(time.RFC3339)
		if status == "failed" {
			j.ErrorCode = errorCode
			j.Error = errorMessage
			j.Retryable = retryable
		}
		if j.CallbackURL != "" {
			j.CallbackStatus = "pending"
			j.CallbackAttempts = 0
		}
	})
	// Callback retries must not hold a job worker after all answers finish.
	q.cleanupWebRecovery(id)
	go q.callback(id)
}

func summarizeResults(results []map[string]any) (string, string, string, bool) {
	okN := 0
	loginFailures := 0
	for _, result := range results {
		if ok, _ := result["ok"].(bool); ok {
			okN++
		}
		if code, _ := result["error_code"].(string); code == "platform_auth_unavailable" {
			loginFailures++
		}
	}
	if okN == len(results) && len(results) > 0 {
		return "succeeded", "", "", false
	}
	if okN > 0 {
		return "partial", "", "", false
	}
	if loginFailures == len(results) && len(results) > 0 {
		return "failed", "platform_auth_unavailable", "平台会话暂不可用，请稍后重试", true
	}
	return "failed", "all_sites_failed", "全部站点未拿到回答", false
}

func usesAgentBay() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("GEO_DOUBAO_BACKEND"))) {
	case "agentbay", "cloud":
		return true
	default:
		return false
	}
}

func agentBayPhone(site string) string {
	// CN_AI_PHONE is the empty default profile. Cloud browsers need the
	// account that actually has this site's saved login.
	if site == "yuanbao" {
		if phone := strings.TrimSpace(os.Getenv("GEO_YUANBAO_PHONE")); phone != "" {
			return phone
		}
	}
	if phone := strings.TrimSpace(os.Getenv("GEO_DOUBAO_PHONE")); phone != "" {
		return phone
	}
	return strings.TrimSpace(os.Getenv("CN_AI_PHONE"))
}

func (q *Queue) runAgentBay(ctx context.Context, site, internalSite, prompt, jobID string, deadline time.Time, waitStart time.Time) map[string]any {
	siteStart := time.Now()
	ctx = withAttemptAccount(ctx, "agentbay")
	one, err, retries := q.scrapeWithRetriesContext(ctx, "http://127.0.0.1:9", internalSite, prompt, jobID, agentBayPhone(internalSite), nil, deadline)
	wall := time.Since(siteStart).Milliseconds()
	if err != nil {
		log.Printf("agentbay failed job=%s site=%s elapsed_ms=%d: %v", jobID, site, wall, err)
		code, message, retry := publicScrapeError(err)
		failed := siteError(site, prompt, code, message, retry, wall)
		if retries > 0 {
			failed["platform_retries"] = retries
		}
		failed["browser_backend"] = "agentbay"
		failed["account_id"] = "agentbay"
		return failed
	}
	if _, ok := one["elapsed_ms"]; !ok || retries > 0 {
		one["elapsed_ms"] = wall
	}
	if retries > 0 {
		one["platform_retries"] = retries
	}
	one["browser_backend"] = "agentbay"
	one["site_id"] = site
	one["account_id"] = "agentbay"
	one["wait_ms"] = siteStart.Sub(waitStart).Milliseconds()
	one["total_ms"] = time.Since(waitStart).Milliseconds()
	return one
}

func doubaoUsesVolcengine() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("GEO_DOUBAO_BACKEND"))) {
	case "volcengine", "volc", "huoshan", "agentkit":
		return true
	default:
		return false
	}
}

func yuanbaoUsesAgentBay() bool {
	// Yuanbao stays on the persistent local Chrome profile. A picture check
	// has to remain open in the admin screenshot for a person to click.
	// AgentBay deletes its browser when the job exits, so it cannot do that.
	return false
}

func (q *Queue) runVolcengineDoubao(ctx context.Context, site, prompt, jobID string, deadline time.Time, waitStart time.Time) map[string]any {
	siteStart := time.Now()
	ctx = withAttemptAccount(ctx, "volcengine")
	phone := strings.TrimSpace(os.Getenv("GEO_DOUBAO_PHONE"))
	one, err, retries := q.scrapeWithRetriesContext(ctx, "http://127.0.0.1:9", "doubao", prompt, jobID, phone, nil, deadline)
	wall := time.Since(siteStart).Milliseconds()
	if err != nil {
		log.Printf("scrape failed job=%s site=%s elapsed_ms=%d: %v", jobID, site, wall, err)
		code, message, retry := publicScrapeError(err)
		return siteError(site, prompt, code, message, retry, wall)
	}
	if _, ok := one["elapsed_ms"]; !ok || retries > 0 {
		one["elapsed_ms"] = wall
	}
	if retries > 0 {
		one["platform_retries"] = retries
	}
	one["site_id"] = site
	one["account_id"] = "volcengine"
	one["wait_ms"] = siteStart.Sub(waitStart).Milliseconds()
	one["total_ms"] = time.Since(waitStart).Milliseconds()
	return one
}

func (q *Queue) runAgentBayYuanbao(ctx context.Context, site, prompt, jobID string) map[string]any {
	siteStart := time.Now()
	phone := strings.TrimSpace(os.Getenv("GEO_YUANBAO_PHONE"))
	if phone == "" {
		return siteError(site, prompt, "platform_auth_unavailable", "GEO_YUANBAO_PHONE is required", false, 0)
	}
	one, err := q.scrapeForContext(withAttemptAccount(ctx, "agentbay"), "http://127.0.0.1:9", "yuanbao", prompt, jobID, phone, siteAttemptTimeout())
	wall := time.Since(siteStart).Milliseconds()
	if err != nil {
		log.Printf("scrape failed job=%s site=%s elapsed_ms=%d: %v", jobID, site, wall, err)
		code, message, retry := publicScrapeError(err)
		return siteError(site, prompt, code, message, retry, wall)
	}
	if _, ok := one["elapsed_ms"]; !ok {
		one["elapsed_ms"] = wall
	}
	one["site_id"] = site
	one["account_id"] = "agentbay"
	return one
}

func (q *Queue) runOneSite(site, prompt, jobID string) (out map[string]any) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	return q.runOneSiteContext(ctx, site, prompt, jobID, 1)
}

func (q *Queue) runOneSiteContext(parent context.Context, site, prompt, jobID string, siteCount int) (out map[string]any) {
	if routingModeForContext(parent, site) == RoutingAPIOnly {
		return q.runDirectAPIContext(parent, site, prompt)
	}
	parent, attempts := newAttemptContext(parent)
	waitStart := time.Now()
	executionStart := waitStart
	timings := &stageTimings{}
	parent = context.WithValue(parent, browserPermitKey{}, &browserPermit{q: q, timings: timings})
	internalSite := internalSiteID(site)
	var recoverySummary map[string]any
	defer func() {
		if out != nil {
			if recoverySummary != nil {
				out["web_recovery"] = recoverySummary
			}
			if code := stringField(out, "error_code"); localWebFailure(code) && !attempts.hasExecutedChannel("web") {
				out["web_skipped"] = true
				if stringField(out, "web_skip_reason") == "" {
					out["web_skip_reason"] = code
				}
				out["elapsed_ms"], out["web_elapsed_ms"] = int64(0), int64(0)
				delete(out, "account_id")
			}
			if !attempts.hasChannel("web") {
				reason := stringField(out, "web_skip_reason")
				if reason == "" {
					switch {
					case stringField(out, "error_code") == "platform_auth_unavailable":
						reason = "no_logged_in_account"
					case parent.Err() != nil:
						reason = "request_deadline_exceeded"
					default:
						reason = "session_setup"
					}
				}
				attempts.skip("web", stringField(out, "account_id"), stringField(out, "error_code"), reason, waitStart)
				out["web_skipped"] = true
				out["web_skip_reason"] = reason
			}
			waitMS := executionStart.Sub(waitStart).Milliseconds()
			if executionStart.Equal(waitStart) {
				waitMS = time.Since(waitStart).Milliseconds()
			}
			timings.attach(out)
			q.attachDiagnostic(out, internalSite, jobID)
			if ok, _ := out["ok"].(bool); !ok && webFailureAllowsAPI(out) && parent.Err() == nil && deliveryWebFallbackEligible(parent, out) {
				q.extendJobDeadline(jobID, time.Now().Add(apiTimeout(internalSite)+5*time.Second))
				out = q.applyAPIFallbackContext(parent, site, prompt, out)
			} else if ok {
				out = q.recoverWebSearchContext(parent, site, prompt, out)
			}
			timings.attach(out)
			out["wait_ms"] = waitMS
			out["total_ms"] = time.Since(waitStart).Milliseconds()
			attempts.attach(out)
		}
	}()
	// Queue and resource waiting do not spend the actual webpage budget.
	admission, cancelAdmission := browserAdmissionContext(parent, internalSite)
	if !usesAgentBay() && !doubaoUsesVolcengine() && len(q.loggedInAccounts(internalSite)) > 0 {
		cancelAdmission()
		admission, cancelAdmission = poolBrowserAdmissionContext(parent, internalSite)
	}
	if isEventualDelivery(parent) {
		cancelAdmission()
		admission, cancelAdmission = q.platformAdmissionContext(parent)
	}
	defer cancelAdmission()
	admissionFailure := func(message string) map[string]any {
		code, reason := "site_timeout", "request_deadline_exceeded"
		if isEventualDelivery(parent) && parent.Err() == nil {
			code, reason = "browser_admission_timeout", "browser_admission"
		}
		log.Printf("web admission canceled job=%s site=%s wait_ms=%d", jobID, site, time.Since(waitStart).Milliseconds())
		return webSkip(site, prompt, code, reason, message)
	}
	if failure := q.siteCooldown(internalSite, site, prompt); failure != nil {
		if isEventualDelivery(parent) {
			return failure // Persist the backoff instead of holding a worker.
		}
		if stringField(failure, "error_code") == "platform_rate_limited" {
			seconds, _ := failure["retry_after_sec"].(int)
			timer := time.NewTimer(time.Duration(seconds) * time.Second)
			select {
			case <-parent.Done():
				timer.Stop()
				return admissionFailure("等待平台限流退避时任务已取消")
			case <-timer.C:
			}
		} else {
			return failure
		}
	}
	if admission.Err() != nil {
		return admissionFailure("网页等待已取消")
	}
	release, admitted := q.acquireDeliveryBrowserResources(parent, admission, internalSite, jobID, timings)
	if !admitted {
		return admissionFailure("网页等待已取消")
	}
	defer release()
	defer func() { q.recordSiteCooldown(internalSite, out) }()
	executionStart = time.Now()
	ctx, cancelWebRound := q.webRoundContext(parent, internalSite, siteCount)
	defer cancelWebRound()
	deadline := q.webpageDeadline(ctx, internalSite, jobID, siteCount)

	if usesAgentBay() {
		webStarted := time.Now()
		defer func() { timings.add("web_execution_ms", time.Since(webStarted)) }()
		return q.runAgentBay(ctx, site, internalSite, prompt, jobID, deadline, waitStart)
	}
	if internalSite == "doubao" && doubaoUsesVolcengine() {
		webStarted := time.Now()
		defer func() { timings.add("web_execution_ms", time.Since(webStarted)) }()
		return q.runVolcengineDoubao(ctx, site, prompt, jobID, deadline, waitStart)
	}
	if internalSite == "yuanbao" && yuanbaoUsesAgentBay() {
		cloudCtx, cancelCloud := context.WithDeadline(ctx, deadline)
		defer cancelCloud()
		webStarted := time.Now()
		defer func() { timings.add("web_execution_ms", time.Since(webStarted)) }()
		return q.runAgentBayYuanbao(cloudCtx, site, prompt, jobID)
	}
	// Local sites share one Chrome. Qianwen used to take an exclusive lock and
	// only started after the other five finished, which pushed a six-site
	// question past five minutes. Cloud doubao does not use this browser.
	if !doubaoUsesVolcengine() {
		q.localBrowser.RLock()
		defer q.localBrowser.RUnlock()
	}
	siteStart := time.Now()
	accountRetries := 0
	tried := map[string]bool{}
	for _, id := range q.blockedAccounts(internalSite) {
		tried[id] = true
	}
	prepareFailures := map[string]bool{}
	authFailures := map[string]bool{}
	prepareCandidates := append(q.loggedInAccounts(internalSite), q.recoveryAccountIDs(internalSite)...)
	var lastAccountFailure map[string]any
	defer func() {
		currentCandidates := append(append(append([]string(nil), prepareCandidates...), q.loggedInAccounts(internalSite)...), q.recoveryAccountIDs(internalSite)...)
		if out != nil && parent.Err() == nil && exhaustedWebAuthentication(currentCandidates, prepareFailures, authFailures, out) {
			out["web_recovery_exhausted"] = true
		}
		if out != nil && accountRetries > 0 {
			out["account_retries"] = accountRetries
		}
	}()
	for {
		if ctx.Err() != nil {
			if lastAccountFailure != nil {
				lastAccountFailure["web_round_exhausted"] = true
				return lastAccountFailure
			}
			return siteError(site, prompt, "site_timeout", "网页调用已取消", true, time.Since(siteStart).Milliseconds())
		}
		var lease *sessions.Lease
		var err error
		needsRecovery := false
		acquireStarted := time.Now()
		sessionCtx, cancelSession := sessionWaitContext(ctx, internalSite)
		if sessionDeadline, ok := sessionCtx.Deadline(); ok {
			q.extendJobDeadline(jobID, sessionDeadline.Add(siteAttemptTimeout()+apiTimeout(internalSite)))
		}
		if len(q.loggedInAccounts(internalSite)) == 0 && anotherAccountReady(q.recoveryAccountIDs(internalSite), tried, "") {
			lease, err = q.acquireRecoveryAccount(sessionCtx, internalSite, tried)
			needsRecovery = err == nil
		} else if len(tried) == 0 && len(q.loggedInAccounts(internalSite)) == 0 && (internalSite == "doubao" || internalSite == "yuanbao") {
			// Reuse existing logins across the whole pool first. Only a pool
			// without saved logins needs the SMS callback handset.
			lease, err = q.Sess.AcquireForLoginContext(sessionCtx, internalSite)
		} else if len(tried) == 0 {
			lease, err = q.Sess.AcquireContext(sessionCtx, internalSite)
		} else {
			lease, err = q.Sess.AcquireContextExcept(sessionCtx, internalSite, tried)
		}
		sessionExpired := sessionCtx.Err() != nil
		cancelSession()
		var preparation *sessions.PreparationError
		newFailures := 0
		if isEventualDelivery(parent) && ctx.Err() == nil && errors.As(err, &preparation) {
			prepareCandidates = append(prepareCandidates, preparation.CandidateIDs...)
			for _, failedID := range preparation.FailedIDs {
				code := preparation.FailureCodes[failedID]
				if failedID == "" || tried[failedID] || (code != "browser_start_failed" && code != "session_setup_failed") {
					continue
				}
				prepareFailures[failedID], tried[failedID] = true, true
				q.coolAccount(internalSite, failedID, code, time.Duration(envInt("GEO_ACCOUNT_TRANSIENT_COOLDOWN_SEC", 120, 0, 600))*time.Second)
				newFailures++
				attempts.skip("web", failedID, code, "session_setup", acquireStarted)
			}
		}
		// An authenticated acquisition can discover an expired token or a live
		// challenge. Lease that same pending page before considering API.
		if err != nil && ctx.Err() == nil && anotherAccountReady(q.recoveryAccountIDs(internalSite), tried, "") {
			recoveryCtx, stopRecoveryAcquire := sessionWaitContext(ctx, internalSite)
			if recoveredLease, recoverErr := q.acquireRecoveryAccount(recoveryCtx, internalSite, tried); recoverErr == nil {
				lease, err, needsRecovery, sessionExpired = recoveredLease, nil, true, false
			}
			stopRecoveryAcquire()
		}
		timings.add("session_acquire_ms", time.Since(acquireStarted))
		if err != nil {
			if isEventualDelivery(parent) && ctx.Err() == nil && preparation != nil {
				// A spent acquisition window does not discard completed failures.
				// Freshly lease the remaining account candidates once, rather than
				// repeatedly starting the first failed browser after every requeue.
				if newFailures > 0 && anotherAccountReady(q.loggedInAccounts(internalSite), tried, "") {
					accountRetries += newFailures
					continue
				}
				if !anotherAccountReady(q.recoveryAccountIDs(internalSite), tried, "") && allPrepareCandidatesFailed(prepareCandidates, prepareFailures) {
					if lastAccountFailure == nil {
						lastAccountFailure = webSkip(site, prompt, "browser_start_failed", "session_setup", "所有候选账号的网页准备均实际失败，等待恢复或使用最终兜底")
					}
					lastAccountFailure["web_prepare_exhausted"] = true
				} else {
					// Unfinished preparation is resource waiting, not proof that
					// an existing recovery page is unavailable or exhausted.
					return webSkip(site, prompt, "session_setup_timeout", "session_setup", "候选账号尚未完成网页准备，保留任务继续等待")
				}
			}
			if lastAccountFailure != nil {
				return lastAccountFailure
			}
			if ctx.Err() != nil || sessionExpired {
				return webSkip(site, prompt, "session_setup_timeout", "session_setup_timeout", "等待网页会话超过执行预算，未启动提问")
			}
			return siteError(site, prompt, "platform_auth_unavailable", "平台会话暂不可用，请稍后重试", true, time.Since(siteStart).Milliseconds())
		}
		if tried[lease.AccountID] {
			lease.Release()
			if lastAccountFailure != nil {
				return lastAccountFailure
			}
			return siteError(site, prompt, "platform_auth_unavailable", "平台会话暂不可用，请稍后重试", true, time.Since(siteStart).Milliseconds())
		}
		sessionStarted := time.Now()
		cdp, err := lease.CDPEndpoint()
		timings.add("session_setup_ms", time.Since(sessionStarted))
		if err != nil {
			failedID := lease.AccountID
			lease.Release()
			tried[failedID] = true
			prepareFailures[failedID] = true
			lastAccountFailure = siteError(site, prompt, "platform_session_unavailable", "平台会话暂不可用，请稍后重试", true, time.Since(siteStart).Milliseconds())
			lastAccountFailure["account_id"] = failedID
			attempts.skip("web", failedID, "platform_session_unavailable", "session_unavailable", sessionStarted)
			if q.switchToNextAccount(deadline, internalSite, tried, failedID) {
				accountRetries++
				log.Printf("account failover job=%s site=%s account=%s code=platform_session_unavailable", jobID, site, failedID)
				continue
			}
			if isEventualDelivery(parent) && allPrepareCandidatesFailed(prepareCandidates, prepareFailures) {
				lastAccountFailure["web_prepare_exhausted"] = true
			}
			return lastAccountFailure
		}
		if ctx.Err() != nil {
			lease.Release()
			return siteError(site, prompt, "site_timeout", "网页调用已取消", true, time.Since(siteStart).Milliseconds())
		}
		deadline = q.webpageDeadline(ctx, internalSite, jobID, siteCount)
		log.Printf("lease job=%s site=%s account=%s", jobID, site, lease.AccountID)

		alternates := anotherAccountReady(q.loggedInAccounts(internalSite), tried, lease.AccountID)
		var one map[string]any
		var accountRecovery map[string]any
		var platformRetries int
		priorLoggedIn := false
		for _, id := range q.loggedInAccounts(internalSite) {
			if id == lease.AccountID {
				priorLoggedIn = true
				break
			}
		}
		accountContext := withWebRecovery(withAttemptAccount(ctx, lease.AccountID), webRecoveryExecution{
			StatePath: q.webRecoveryPath(jobID, internalSite, lease.AccountID), AccountID: lease.AccountID, PriorLoggedIn: priorLoggedIn && !needsRecovery,
		})
		webStarted := time.Now()
		resumeOnly := isEventualDelivery(parent) && pendingWebSend(accountContext)
		if resumeOnly {
			var recovery map[string]any
			one, err, recovery = q.recoverWebAttempt(accountContext, lease, cdp, site, prompt, jobID, siteCount, false, recoveryFailure("recovery_unconfirmed"))
			accountRecovery = recovery
			recoverySummary = mergeWebRecovery(recoverySummary, recovery)
		} else if needsRecovery {
			var recovery map[string]any
			one, err, recovery = q.recoverWebAttempt(accountContext, lease, cdp, site, prompt, jobID, siteCount, true, recoveryFailure("platform_auth_unavailable"))
			accountRecovery = recovery
			recoverySummary = mergeWebRecovery(recoverySummary, recovery)
		} else if alternates {
			// Give another logged-in account a chance before API fallback,
			// including when that API is configured but cannot answer.
			attempt := time.Until(deadline)
			one, err = q.scrapeForContext(accountContext, cdp, internalSite, prompt, jobID, lease.Phone, attempt)
		} else {
			one, err, platformRetries = q.scrapeWithRetriesContext(accountContext, cdp, internalSite, prompt, jobID, lease.Phone, lease.Manager, deadline)
		}
		if err != nil && !needsRecovery && !resumeOnly && ctx.Err() == nil {
			if code, _, _ := publicScrapeError(err); webRecoveryMayResume(code) {
				var recovery map[string]any
				one, err, recovery = q.recoverWebAttempt(accountContext, lease, cdp, site, prompt, jobID, siteCount, false, err)
				accountRecovery = recovery
				if stringField(recovery, "error_code") != "recovery_state_missing" {
					recoverySummary = mergeWebRecovery(recoverySummary, recovery)
				}
			}
		}
		timings.add("web_execution_ms", time.Since(webStarted))
		wall := time.Since(siteStart).Milliseconds()
		if err != nil {
			if ctx.Err() != nil {
				lease.Release()
				return siteError(site, prompt, "site_timeout", "网页调用已取消", true, wall)
			}
			log.Printf("scrape failed job=%s site=%s account=%s elapsed_ms=%d: %v", jobID, site, lease.AccountID, wall, err)
			code, message, retry := publicScrapeError(err)
			if accountRecovery["auth_unavailable_proven"] == true && !pendingWebSend(accountContext) {
				authFailures[lease.AccountID] = true
			}
			if localWebFailure(code) {
				failedID := lease.AccountID
				q.coolAccount(internalSite, failedID, code, time.Duration(envInt("GEO_ACCOUNT_TRANSIENT_COOLDOWN_SEC", 120, 0, 600))*time.Second)
				lease.Release()
				tried[failedID] = true
				if code == "browser_start_failed" {
					prepareFailures[failedID] = true
				}
				if q.switchToNextAccount(deadline, internalSite, tried, failedID) {
					accountRetries++
					continue
				}
				if lastAccountFailure != nil {
					if isEventualDelivery(parent) && allPrepareCandidatesFailed(prepareCandidates, prepareFailures) {
						lastAccountFailure["web_prepare_exhausted"] = true
					}
					return lastAccountFailure
				}
				failed := webSkip(site, prompt, code, code, message)
				if isEventualDelivery(parent) && allPrepareCandidatesFailed(prepareCandidates, prepareFailures) {
					failed["web_prepare_exhausted"] = true
				}
				return failed
			}
			switch code {
			case "platform_auth_unavailable":
				if internalSite == "yuanbao" {
					message = fmt.Sprintf("账号 %s 的元宝登录已失效，下次提问会自动重新登录；若出现图片验证，请在后台截图中点击", lease.AccountID)
				}
				lease.MarkUnavailable(message)
				q.coolAccount(internalSite, lease.AccountID, code, time.Duration(envInt("GEO_AUTH_COOLDOWN_SEC", 120, 0, 3600))*time.Second)
			case "platform_risk_control":
				log.Printf("web verification remains unresolved job=%s site=%s account=%s", jobID, site, lease.AccountID)
				lease.MarkNeedsCaptcha(message)
				q.coolAccount(internalSite, lease.AccountID, "platform_risk_control", time.Duration(envInt("GEO_RISK_COOLDOWN_SEC", 600, 0, 3600))*time.Second)
			case "platform_quota":
				q.blockAccount(internalSite, lease.AccountID, message)
			case "browser_start_failed", "browser_unresponsive", "send_timeout", "generation_timeout", "answer_extract_timeout", "site_timeout":
				q.coolAccount(internalSite, lease.AccountID, code, time.Duration(envInt("GEO_ACCOUNT_TRANSIENT_COOLDOWN_SEC", 120, 0, 600))*time.Second)
			}
			failedID := lease.AccountID
			lastAccountFailure = siteError(site, prompt, code, message, retry, wall)
			lastAccountFailure["account_id"] = failedID
			if seconds := evidenceCount(accountRecovery["retry_after_sec"]); seconds > 0 {
				lastAccountFailure["retry_after_sec"] = seconds
			}
			if failure, ok := err.(*scrapeFailure); ok && code == "platform_rate_limited" && failure.retryAfter > 0 {
				lastAccountFailure["retry_after_sec"] = failure.retryAfter
			}
			if platformRetries > 0 {
				lastAccountFailure["platform_retries"] = platformRetries
			}
			lease.Release()
			tried[failedID] = true
			if isEventualDelivery(parent) && pendingWebSend(accountContext) && code != "platform_quota" {
				lastAccountFailure["delivery_resume_required"] = true
				if unconfirmedDeliverySend(accountContext) {
					lastAccountFailure["retry_state"] = "awaiting_confirmation"
				}
				return lastAccountFailure
			}
			// The API is the last resort after available pool accounts.
			if q.switchToNextAccount(deadline, internalSite, tried, failedID) {
				accountRetries++
				log.Printf("account failover job=%s site=%s account=%s code=%s", jobID, site, failedID, code)
				continue
			}
			return lastAccountFailure
		}
		if _, ok := one["elapsed_ms"]; !ok {
			one["elapsed_ms"] = wall
		}
		if platformRetries > 0 {
			one["elapsed_ms"] = wall
			one["platform_retries"] = platformRetries
		}
		if accountRetries > 0 {
			one["account_retries"] = accountRetries
		}
		one["site_id"] = site
		if _, exists := one["browser_backend"]; !exists {
			one["browser_backend"] = "local"
		}
		one["account_id"] = lease.AccountID
		one["wait_ms"] = siteStart.Sub(waitStart).Milliseconds()
		one["total_ms"] = time.Since(waitStart).Milliseconds()
		if internalSite == "doubao" {
			switch doubaoSessionEffect(one) {
			case "logged_in":
				lease.MarkAvailable("登录有效")
			case "need_captcha":
				lease.MarkNeedsCaptcha("豆包触发语义安全验证，请人工完成验证后重试")
			}
		}
		if internalSite == "yuanbao" {
			lease.MarkAvailable("登录有效")
		}
		q.clearAccountBlock(internalSite, lease.AccountID)
		lease.Release()
		return one
	}
}

func (q *Queue) siteGate(site string) chan struct{} {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.siteGates == nil {
		q.siteGates = make(map[string]chan struct{})
	}
	if q.siteGates[site] == nil {
		limit := 1
		if q.Sess != nil && !usesAgentBay() && !doubaoUsesVolcengine() && cap(q.siteSem) > 0 {
			// Local account leases already serialize each profile/site. A
			// platform-wide mutex otherwise leaves the rest of the logged-in
			// pool idle while queued questions exhaust their web budgets.
			// Keep this gate stable as accounts are added; the global browser
			// semaphore still bounds actual execution across all platforms.
			limit = cap(q.siteSem)
		}
		q.siteGates[site] = make(chan struct{}, limit)
	}
	return q.siteGates[site]
}

func acquireSlot(ctx context.Context, slot chan struct{}) bool {
	if ctx.Err() != nil {
		return false
	}
	select {
	case slot <- struct{}{}:
		if ctx.Err() != nil {
			<-slot
			return false
		}
		return true
	case <-ctx.Done():
		return false
	}
}

// doubaoSessionEffect decides what a successful Doubao result says about the
// leased local profile. A local answer means that profile is logged in.
// AgentBay can still return an answer after the local page hits a captcha;
// that cloud answer must not clear the local account's expired flag into
// logged_in, or the next selection treats a blocked profile as ready.
func doubaoSessionEffect(result map[string]any) string {
	if result == nil {
		return ""
	}
	if ok, _ := result["ok"].(bool); !ok {
		return ""
	}
	backend, _ := result["browser_backend"].(string)
	switch backend {
	case "local":
		return "logged_in"
	case "agentbay":
		if strings.Contains(stringField(result, "local_error"), "验证") {
			return "need_captcha"
		}
	}
	return ""
}

// scrapeWithRetries repeats a retryable scrape until deadline. Login, captcha,
// and quota are not repeated on the same session. The deadline is the whole
// site budget, including later account failover in the caller.
func (q *Queue) scrapeWithRetries(cdp, site, prompt, jobID, phone string, manager *sessions.Manager, deadline time.Time) (map[string]any, error, int) {
	return q.scrapeWithRetriesContext(context.Background(), cdp, site, prompt, jobID, phone, manager, deadline)
}

func (q *Queue) scrapeWithRetriesContext(parent context.Context, cdp, site, prompt, jobID, phone string, manager *sessions.Manager, deadline time.Time) (map[string]any, error, int) {
	var one map[string]any
	var err error
	retries := 0
	for {
		if parent.Err() != nil {
			return one, &scrapeFailure{code: "site_timeout", raw: "请求总预算耗尽", retryable: true}, retries
		}
		count, freshBudget := parent.Value(webExecutionBudgetKey{}).(int)
		if freshBudget {
			deadline = q.webpageDeadline(parent, site, jobID, count)
		}
		remain := time.Until(deadline)
		if remain <= 0 {
			if err != nil {
				return one, err, retries
			}
			return nil, &scrapeFailure{code: "site_timeout", raw: "站点执行预算耗尽", retryable: true}, retries
		}
		if !freshBudget && retries > 0 && remain < minimumWebWindow(site) {
			log.Printf("site budget reached job=%s site=%s remain=%s", jobID, site, remain.Round(time.Second))
			return one, err, retries
		}
		attempt := attemptTimeoutFor(site)
		if freshBudget {
			attempt = siteAttemptTimeout()
		}
		if remain < attempt {
			attempt = remain
		}
		one, err = q.scrapeForContext(parent, cdp, site, prompt, jobID, phone, attempt)
		canRetry := shouldRetryScrape(site, err, retries, deadline)
		if freshBudget {
			canRetry = parent.Err() == nil && err != nil && retries < scrapeRetryLimit(site, err)
		}
		if pendingWebSend(parent) {
			canRetry = false
		}
		if !canRetry {
			if err != nil && scrapeRetryLimit(site, err) > retries {
				log.Printf("site budget reached job=%s site=%s remain=%s", jobID, site, time.Until(deadline).Round(time.Second))
			}
			return one, err, retries
		}
		retries++
		delay := scrapeRetryDelay(site, err, retries)
		log.Printf(
			"platform transient job=%s site=%s retry=%d/%d delay=%s error=%v",
			jobID, site, retries, scrapeRetryLimit(site, err), delay, err,
		)
		if manager != nil && !doubaoUsesVolcengine() && (site == "yuanbao" || site == "yiyan" || site == "qianwen") {
			setupCtx, cancelSetup := sessionWaitContext(parent, site)
			_ = manager.EnsurePageContext(setupCtx, site)
			cancelSetup()
		}
		if delay > 0 {
			timer := time.NewTimer(delay)
			select {
			case <-parent.Done():
				timer.Stop()
				return one, err, retries
			case <-timer.C:
			}
		}
	}
}

// siteBudget caps one site from session acquire through retries. It stays
// above one GEO_SITE_TIMEOUT_SEC attempt so a slow healthy answer can finish,
// and below a stack of full-length retries.
func siteBudget() time.Duration {
	return time.Duration(envInt("GEO_SITE_BUDGET_SEC", 420, 180, 900)) * time.Second
}

// siteExecutionDeadline is the webpage deadline for one site. With an official
// API key configured, that phase stops at GEO_API_FALLBACK_WEB_BUDGET_SEC
// (default 240s) so a hung page and its retries cannot run for the full
// multi-account budget. The official API call happens after this deadline.
func siteExecutionDeadline(site string, start time.Time, accounts int) time.Time {
	deadline := start.Add(siteBudgetForAccounts(accounts))
	if !officialAPIConfigured(site) {
		return deadline
	}
	capped := start.Add(fallbackWebBudget())
	if capped.Before(deadline) {
		return capped
	}
	return deadline
}

func fallbackWebBudget() time.Duration {
	return time.Duration(apiEnvInt("GEO_API_FALLBACK_WEB_BUDGET_SEC", 240, 45, 240)) * time.Second
}

// attemptTimeoutFor keeps a single AgentBay try short enough that a second
// failure still fits before the official API is allowed.
func attemptTimeoutFor(site string) time.Duration {
	attempt := siteAttemptTimeout()
	if !officialAPIConfigured(site) {
		return attempt
	}
	capped := time.Duration(apiEnvInt("GEO_API_FALLBACK_ATTEMPT_SEC", 140, 30, 180)) * time.Second
	if capped < attempt {
		return capped
	}
	return attempt
}

// siteBudgetForAccounts leaves one full attempt per logged-in phone. A single
// shared budget used to expire before the second or third handset could run.
func siteBudgetForAccounts(accounts int) time.Duration {
	base := siteBudget()
	if accounts <= 1 {
		return base
	}
	need := time.Duration(accounts) * siteAttemptTimeout()
	if need < base {
		return base
	}
	const cap = 15 * time.Minute
	if need > cap {
		return cap
	}
	return need
}

func siteAttemptTimeout() time.Duration {
	return time.Duration(envInt("GEO_SITE_TIMEOUT_SEC", 120, 30, 300)) * time.Second
}

const minAttemptWindow = 45 * time.Second

func budgetAllowsAttempt(deadline time.Time) bool {
	return time.Until(deadline) >= minAttemptWindow
}

func shouldRetryScrape(site string, err error, retries int, deadline time.Time) bool {
	if err == nil || retries >= scrapeRetryLimit(site, err) {
		return false
	}
	delay := scrapeRetryDelay(site, err, retries+1)
	if failure, ok := err.(*scrapeFailure); ok && failure.code == "site_timeout" {
		return time.Until(deadline) >= delay+attemptTimeoutFor(site)
	}
	return time.Until(deadline) >= delay+minimumWebWindow(site)
}

// anotherAccountReady is true when a logged-in account has not yet failed
// this site. Failed phones stay out for the rest of the job so the next
// attempt starts on a different handset immediately.
func anotherAccountReady(loggedIn []string, tried map[string]bool, current string) bool {
	for _, id := range loggedIn {
		if id == "" || id == current || tried[id] {
			continue
		}
		return true
	}
	return false
}

func (q *Queue) switchToNextAccount(deadline time.Time, site string, tried map[string]bool, current string) bool {
	return anotherAccountReady(q.loggedInAccounts(site), tried, current) || anotherAccountReady(q.recoveryAccountIDs(site), tried, current)
}

func poolAttemptTimeout(site string, deadline time.Time) time.Duration {
	attempt := attemptTimeoutFor(site)
	remaining := time.Until(deadline)
	// A submitted question may still be searching or generating. Keep its
	// full attempt window; switch accounts on an actual failure rather than
	// halving a healthy generation merely because other logins exist.
	if remaining < attempt {
		attempt = remaining
	}
	return attempt
}

func (q *Queue) loggedInAccounts(site string) []string {
	if q == nil || q.Sess == nil {
		return nil
	}
	return q.withoutBlockedAccounts(site, q.Sess.LoggedInAccountIDs(site))
}

func scrapeRetryLimit(site string, err error) int {
	if err == nil {
		return 0
	}
	code, _, retryable := publicScrapeError(err)
	if !retryable {
		return 0
	}
	switch code {
	case "platform_quota", "platform_risk_control", "platform_auth_unavailable", "sms_timeout":
		return 0
	}
	if officialAPIConfigured(site) {
		// Retry transient failures only. Auth, quota and verification cannot
		// recover by reopening a browser, even when an API key is configured.
		return apiEnvInt("GEO_API_FALLBACK_WEB_RETRIES", 1, 0, 8)
	}
	failure, ok := err.(*scrapeFailure)
	if !ok || !failure.retryable {
		return 0
	}
	switch failure.code {
	case "platform_quota", "platform_risk_control", "platform_auth_unavailable", "sms_timeout":
		return 0
	}
	switch failure.code {
	case "site_timeout":
		if site == "kimi" {
			return envInt("GEO_KIMI_TIMEOUT_RETRIES", 2, 0, 4)
		}
		return 1
	}
	if site == "kimi" {
		// One short retry on the same account, then runOneSite leases the
		// next logged-in Kimi account. Sitting on one busy send button for
		// the shared five-retry backoff never produced a reply.
		return envInt("GEO_KIMI_BUSY_RETRIES", 1, 0, 3)
	}
	return envInt("GEO_SITE_FAILURE_RETRIES", 5, 0, 8)
}

func scrapeRetryDelay(site string, _ error, retry int) time.Duration {
	if site == "kimi" {
		base := envInt("GEO_PLATFORM_BUSY_RETRY_BASE_SEC", 15, 5, 60)
		shift := retry - 1
		if shift > 2 {
			shift = 2
		}
		return time.Duration(base*(1<<shift)) * time.Second
	}
	return time.Duration(envInt("GEO_SITE_FAILURE_RETRY_SEC", 3, 0, 30)) * time.Second
}

func isKimiTransient(err error) bool {
	failure, ok := err.(*scrapeFailure)
	if !ok || !failure.retryable {
		return false
	}
	switch failure.code {
	case "platform_busy", "site_timeout", "empty_answer", "invalid_answer":
		return true
	default:
		return false
	}
}

func isYuanbaoTransient(err error) bool {
	failure, ok := err.(*scrapeFailure)
	if !ok || !failure.retryable {
		return false
	}
	switch failure.code {
	case "empty_answer", "invalid_answer", "platform_busy":
		return true
	default:
		return false
	}
}

func isPlatformRisk(err error) bool {
	failure, ok := err.(*scrapeFailure)
	return ok && failure.code == "platform_risk_control"
}

func internalSiteID(site string) string {
	if site == "weixinyiyan" || site == "wenxinyiyan" {
		return "yiyan"
	}
	return site
}

func siteError(site, prompt, code, message string, retryable bool, elapsedMS int64) map[string]any {
	return map[string]any{
		"site_id": site, "ok": false, "prompt": prompt,
		"error_code": code, "error": message, "retryable": retryable,
		"elapsed_ms": elapsedMS,
	}
}

func publicScrapeError(err error) (string, string, bool) {
	if failure, ok := err.(*scrapeFailure); ok {
		switch failure.code {
		case "platform_busy":
			return failure.code, "平台网页繁忙或当前回答尚未完成", failure.retryable
		case "platform_risk_control":
			return failure.code, "平台触发安全验证，请完成验证后重试", failure.retryable
		case "platform_auth_unavailable":
			return failure.code, "平台会话暂不可用，请重新登录", failure.retryable
		case "platform_quota":
			return failure.code, failure.raw, false
		case "sms_timeout":
			return failure.code, failure.raw, failure.retryable
		case "insufficient_web_budget":
			return failure.code, "剩余时间不足以完成网页调用", true
		case "browser_admission_timeout":
			return failure.code, "网页资源等待超时，未启动提问", true
		case "session_setup_timeout", "browser_start_failed":
			return failure.code, "本地浏览器会话启动失败或超时", true
		case "browser_unresponsive":
			return failure.code, "浏览器页面持续无进展，请稍后重试", true
		case "generation_timeout":
			return failure.code, "平台回答未在生成预算内完成", failure.retryable
		case "send_timeout":
			return failure.code, "发送阶段超时，保留原页面核实，未重复提交问题", failure.retryable
		case "answer_extract_timeout":
			return failure.code, "网页正文提取超时", failure.retryable
		case "recovery_unconfirmed":
			return failure.code, "无法确认原问题的网页恢复状态，已停止重复发送", false
		case "manual_question_mismatch", "manual_send_unconfirmed":
			return failure.code, manualReason(failure.code), false
		case "no_progress":
			return failure.code, "网页恢复没有进展，已停止重复操作", false
		case "target_missing":
			return failure.code, "原网页标签已不存在，无法继续读取本次对话", false
		case "recovery_page_missing":
			return failure.code, "原网页标签已不存在，无法继续读取本次对话", false
		case "platform_setting_unavailable":
			return failure.code, "网页对话设置未能确认，已停止提交问题", false
		case "platform_rate_limited":
			return failure.code, "平台明确限制当前请求频率", false
		case "share_timeout":
			return failure.code, "网页分享阶段超时", failure.retryable
		}
	}
	text := strings.ToLower(err.Error())
	authMarkers := []string{
		"登录", "未登录", "logged_out", "not logged", "sign in",
		"login required", "session expired", "unauthorized",
	}
	for _, marker := range authMarkers {
		if strings.Contains(text, marker) {
			return "platform_auth_unavailable", "平台会话暂不可用，请稍后重试", true
		}
	}
	if strings.Contains(text, "deadline exceeded") || strings.Contains(text, "timeout") || strings.Contains(text, "超时") {
		return "site_timeout", "平台响应超时，请稍后重试", true
	}
	return "site_request_failed", "平台请求失败，请稍后重试", true
}

type scrapeFailure struct {
	code       string
	raw        string
	retryable  bool
	retryAfter int64
}

func (e *scrapeFailure) Error() string { return e.raw }

func (q *Queue) scrape(cdp, site, prompt, jobID, phone string) (map[string]any, error) {
	return q.scrapeFor(cdp, site, prompt, jobID, phone, siteAttemptTimeout())
}

func (q *Queue) scrapeFor(cdp, site, prompt, jobID, phone string, timeout time.Duration) (map[string]any, error) {
	return q.scrapeForContext(context.Background(), cdp, site, prompt, jobID, phone, timeout)
}

func (q *Queue) scrapeForContext(parent context.Context, cdp, site, prompt, jobID, phone string, timeout time.Duration) (result map[string]any, scrapeErr error) {
	scrapeStarted := time.Now()
	attempted := false
	var diagnostic *scrapeDiagnostic
	// Register first so quota/checkpoint classification runs before recording
	// the final outcome of this exact account attempt.
	defer func() {
		if attempts := attemptsFromContext(parent); attempts != nil {
			code := ""
			if scrapeErr != nil {
				code, _, _ = publicScrapeError(scrapeErr)
			}
			if !attempted {
				reason := "request_deadline_exceeded"
				if localWebFailure(code) {
					reason = code
				}
				attempts.skip("web", "", code, reason, scrapeStarted)
				return
			}
			if localWebFailure(code) {
				attempts.skip("web", "", code, code, scrapeStarted)
				return
			}
			details := map[string]any{}
			if scrapeErr != nil {
				details["failure_reason"] = scrapeFailureReason(scrapeErr)
				if diagnostic != nil {
					details["failure_stage"], details["diagnostic_id"] = diagnostic.phase(), diagnostic.ID
				}
			}
			attempts.record("web", attemptAccount(parent), scrapeErr == nil && result["ok"] == true, code, scrapeStarted, details)
		}
	}()
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	diagnostic = q.beginDiagnostic(site, jobID)
	if diagnostic != nil {
		q.diagnosticIDs.Store(jobID+"/"+site, diagnostic.ID)
		defer func() { diagnostic.finish(ctx, result, scrapeErr) }()
	}
	// Some Yuanbao detail responses report a quota refusal as a completed
	// assistant message. Reject it on every success path, including recovered
	// checkpoints, so the caller releases this lease and tries another account.
	defer func() {
		if scrapeErr == nil && yuanbaoQuotaReply(site, stringField(result, "answer")) {
			result = nil
			scrapeErr = &scrapeFailure{code: "platform_quota", raw: "元宝此模型的本月用量已用完，次月1日零点刷新", retryable: false}
		}
	}()
	if ctx.Err() != nil {
		return nil, &scrapeFailure{code: "site_timeout", raw: "请求总预算耗尽", retryable: true}
	}
	if err := q.markWebDispatch(jobID, site); err != nil {
		return nil, &scrapeFailure{code: "browser_start_failed", raw: "无法保存网页派发状态，未发送问题", retryable: true}
	}
	cmd := exec.CommandContext(ctx, q.Python, q.Script,
		"--cdp-url", cdp,
		"--site", site,
		"--prompt", prompt,
		"--question-id", "job_"+jobID,
		"--phone", phone,
	)
	if execution, ok := parent.Value(webRecoveryContextKey{}).(webRecoveryExecution); ok && execution.StatePath != "" {
		if err := os.MkdirAll(filepath.Dir(execution.StatePath), 0700); err != nil {
			return nil, &scrapeFailure{code: "browser_start_failed", raw: "无法保存网页任务恢复状态", retryable: false}
		}
		if execution.Action != "" {
			cmd.Args = append(cmd.Args, "--recovery-action", execution.Action, "--recovery-file", execution.StatePath)
		}
		if execution.SharedURL != "" {
			cmd.Args = append(cmd.Args, "--shared-url", execution.SharedURL)
		}
	}
	deadline, _ := ctx.Deadline()
	cmd.Env = recoveryEnv(parent, append(os.Environ(), fmt.Sprintf("GEO_DIAGNOSTIC_DEADLINE_MS=%d", deadline.UnixMilli())))
	if diagnostic != nil {
		cmd.Env = append(cmd.Env, "GEO_DIAGNOSTIC_RUN_DIR="+diagnostic.Dir, "GEO_DIAGNOSTIC_DIR="+q.diagnosticRoot())
	}
	// Playwright starts a Node/browser descendant. Killing only the Python
	// parent leaves that descendant holding stdout/stderr and makes Wait block
	// until the scraper's own 300s timeout. Terminate the whole process group.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.WaitDelay = 2 * time.Second
	cmdDone := make(chan struct{})
	cmd.Dir = q.GeoRoot
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Start()
	if err != nil {
		return nil, &scrapeFailure{code: "browser_start_failed", raw: "本地网页执行进程无法启动", retryable: true}
	}
	attempted = true
	// Start before reading Process in the cancellation goroutine: Start
	// writes this pointer, so reading it concurrently caused a data race.
	pid := cmd.Process.Pid
	if diagnostic != nil {
		diagnostic.PID = pid
	}
	var pageStalled atomic.Bool
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				_ = syscall.Kill(-pid, syscall.SIGKILL)
				return
			case <-cmdDone:
				return
			case <-ticker.C:
				if diagnostic != nil && diagnostic.pageStalled(time.Now()) {
					pageStalled.Store(true)
					log.Printf("unresponsive page job=%s site=%s diagnostic_id=%s", jobID, site, diagnostic.ID)
					_ = syscall.Kill(-pid, syscall.SIGKILL)
					return
				}
			}
		}
	}()
	if cmd.Process != nil {
		procguard.Adopt(cmd.Process.Pid)
	}
	err = cmd.Wait()
	if diagnostic != nil && cmd.ProcessState != nil {
		value := cmd.ProcessState.ExitCode()
		diagnostic.ExitCode = &value
	}
	close(cmdDone)
	if pageStalled.Load() && ctx.Err() == nil {
		if completed := completedWebCheckpoint(stdout.Bytes(), site, prompt, jobID); completed != nil {
			completed["elapsed_ms"] = time.Since(scrapeStarted).Milliseconds()
			completed["optional_stage_error_code"] = "browser_unresponsive"
			return completed, nil
		}
		return nil, &scrapeFailure{code: "browser_unresponsive", raw: "浏览器页面持续无响应，正在切换号池账号", retryable: true}
	}
	if ctx.Err() != nil {
		code := diagnosticTimeoutCode(diagnostic)
		if completed := completedWebCheckpoint(stdout.Bytes(), site, prompt, jobID); completed != nil {
			completed["elapsed_ms"] = time.Since(scrapeStarted).Milliseconds()
			completed["optional_stage_error_code"] = code
			return completed, nil
		}
		return nil, &scrapeFailure{code: code, raw: fmt.Sprintf("网页阶段超时（超过 %s）", timeout), retryable: true}
	}
	line := lastJSON(stdout.Bytes())
	if len(line) == 0 {
		msg := strings.TrimSpace(stderr.String())
		if err != nil && msg == "" {
			msg = err.Error()
		}
		if msg == "" {
			msg = "抓取脚本无输出"
		}
		if strings.Contains(msg, "connect_over_cdp") {
			return nil, &scrapeFailure{code: "browser_start_failed", raw: "本地浏览器连接失败", retryable: true}
		}
		return nil, fmt.Errorf("%s", msg)
	}
	var out map[string]any
	if err := json.Unmarshal(line, &out); err != nil {
		return nil, fmt.Errorf("解析抓取结果失败: %w", err)
	}
	if ok, _ := out["ok"].(bool); !ok {
		if completed := completedWebCheckpoint(stdout.Bytes(), site, prompt, jobID); completed != nil {
			completed["elapsed_ms"] = time.Since(scrapeStarted).Milliseconds()
			code := stringField(out, "error_code")
			switch code {
			case "answer_extract_timeout", "share_timeout", "platform_auth_unavailable", "platform_risk_control", "generation_timeout":
			default:
				code = "postprocess_failed"
			}
			completed["optional_stage_error_code"] = code
			return completed, nil
		}
		msg, _ := out["error"].(string)
		msg = strings.TrimSpace(msg)
		if strings.EqualFold(msg, "none") || strings.EqualFold(msg, "null") {
			msg = ""
		}
		if msg == "" {
			msg = strings.TrimSpace(stderr.String())
		}
		if strings.TrimSpace(msg) == "" && err != nil {
			msg = err.Error()
		}
		if strings.TrimSpace(msg) == "" {
			msg = "平台未返回有效回答"
		}
		if strings.Contains(msg, "connect_over_cdp") {
			return nil, &scrapeFailure{code: "browser_start_failed", raw: "本地浏览器连接失败", retryable: true}
		}
		if code, _ := out["error_code"].(string); strings.TrimSpace(code) != "" {
			retryable := true
			if value, exists := out["retryable"].(bool); exists {
				retryable = value
			}
			failure := &scrapeFailure{code: code, raw: msg, retryable: retryable}
			if after, ok := out["retry_after_sec"].(float64); ok && after >= 1 && after <= 600 {
				failure.retryAfter = int64(after)
			}
			return nil, failure
		}
		return nil, fmt.Errorf("%s", msg)
	}
	if err != nil {
		if completed := completedWebCheckpoint(stdout.Bytes(), site, prompt, jobID); completed != nil {
			completed["optional_stage_error_code"] = "postprocess_failed"
			return completed, nil
		}
		return nil, fmt.Errorf("%s", err.Error())
	}
	return out, nil
}

func completedWebCheckpoint(stdout []byte, site, prompt, jobID string) map[string]any {
	lines := bytes.Split(stdout, []byte{'\n'})
	for i := len(lines) - 1; i >= 0; i-- {
		var result map[string]any
		if json.Unmarshal(lines[i], &result) != nil || result["web_answer_completed"] != true || result["ok"] != true {
			continue
		}
		if stringField(result, "site_id") != site || stringField(result, "prompt") != prompt || stringField(result, "question_id") != "job_"+jobID || strings.TrimSpace(stringField(result, "answer")) == "" || stringField(result, "answer_origin") != "web" || stringField(result, "source") == "official_api" {
			continue
		}
		// Never publish an unverified share from an unfinished optional stage.
		result["url"] = ""
		result["share_status"] = "generation_failed"
		result["share_error_code"] = "share_timeout"
		result["web_answer_recovered"] = true
		delete(result, "web_answer_completed")
		return result
	}
	return nil
}

func (q *Queue) fail(id, msg string) {
	q.patch(id, func(j *Job) {
		j.Status = "failed"
		j.Error = msg
		j.FinishedAt = time.Now().Format(time.RFC3339)
	})
}

func (q *Queue) patch(id string, fn func(*Job)) {
	q.patchIf(id, func(j *Job) bool { fn(j); return true })
}

func (q *Queue) patchIf(id string, fn func(*Job) bool) bool {
	q.mu.Lock()
	j := q.jobs[id]
	if j == nil || j.Status == "cancelled" || !fn(j) {
		q.mu.Unlock()
		return false
	}
	j.UpdatedAt = time.Now().Format(time.RFC3339)
	saved := copyJob(j)
	q.mu.Unlock()
	q.publish(id)
	q.save(saved)
	return true
}

func (q *Queue) load() (int, []string, []string) {
	if q.DataDir == "" {
		return 0, nil, nil
	}
	if err := os.MkdirAll(q.DataDir, 0o755); err != nil {
		log.Printf("jobs data dir: %v", err)
		return 0, nil, nil
	}
	matches, _ := filepath.Glob(filepath.Join(q.DataDir, "*.json"))
	n := 0
	if q.requestIndex == nil {
		q.requestIndex = make(map[string]string)
	}
	queued := make([]string, 0)
	interrupted := make([]string, 0)
	for _, p := range matches {
		raw, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		var j Job
		if err := json.Unmarshal(raw, &j); err != nil || j.ID == "" {
			continue
		}
		if created, err := time.Parse(time.RFC3339, j.CreatedAt); err == nil {
			j.createdUnix = created.UnixMilli()
		}
		if j.ManualWorkflow {
			recoverManualJob(&j)
			q.jobs[j.ID] = &j
			if j.RequestID != "" {
				q.requestIndex[requestKey(j.AK, j.RequestID)] = j.ID
			}
			if j.Status == "queued" {
				queued = append(queued, j.ID)
			}
			q.saveLocked(&j)
			n++
			continue
		}
		// Keep terminal history unchanged; pending jobs adopt web-first waiting.
		recovered := false
		if requireDelivery() && j.DeadlinePolicy == "web_first" && (j.Status == "queued" || j.Status == "running") && j.DeliveryPolicy != "eventual" {
			j.DeliveryPolicy = "eventual"
			recovered = true
		}
		if j.DeliveryPolicy == "eventual" && (j.Status == "queued" || j.Status == "running") {
			if j.Status == "running" {
				recoverDeliveryJob(&j, q.heldBeforeSend(&j))
				recovered = true
			}
			queued = append(queued, j.ID)
			q.jobs[j.ID] = &j
			if j.RequestID != "" {
				q.requestIndex[requestKey(j.AK, j.RequestID)] = j.ID
			}
			if recovered {
				q.saveLocked(&j)
			}
			n++
			continue
		}
		switch j.Status {
		case "queued":
			if j.DeadlinePolicy != "web_first" {
				j.DeadlinePolicy = "web_first"
				recovered = true
			}
			queued = append(queued, j.ID)
		case "running":
			recovered = true
			if q.heldBeforeSend(&j) {
				j.Status = "queued"
				j.Results = nil
				j.CompletedSites = 0
				j.StartedAt = ""
				j.DeadlinePolicy = "web_first"
				queued = append(queued, j.ID)
				break
			}
			j.Status = "failed"
			j.Error = "服务重启，任务中断"
			j.ErrorCode = "service_interrupted"
			j.Retryable = true
			j.UpdatedAt = time.Now().Format(time.RFC3339)
			j.FinishedAt = time.Now().Format(time.RFC3339)
			if j.CallbackURL != "" {
				j.CallbackStatus = "pending"
				j.CallbackAttempts = 0
				interrupted = append(interrupted, j.ID)
			}
		}
		q.jobs[j.ID] = &j
		if j.RequestID != "" {
			q.requestIndex[requestKey(j.AK, j.RequestID)] = j.ID
		}
		if recovered {
			q.saveLocked(&j)
		}
		n++
	}
	sort.Slice(queued, func(i, k int) bool {
		return q.jobs[queued[i]].CreatedAt < q.jobs[queued[k]].CreatedAt
	})
	q.publishAll()
	return n, queued, interrupted
}

// The Python deployment barrier runs before any browser operation. Only jobs
// explicitly held there may return to the queue; dispatched jobs never replay.
// Preserve the original request ID without replaying a dispatched question.
func (q *Queue) heldBeforeSend(job *Job) bool {
	for _, result := range job.Results {
		if result["web_skipped"] != true {
			return false
		}
	}
	if !q.deploymentHolds(job.ID) {
		return false
	}
	root := filepath.Dir(q.DataDir)
	questionID := "job_" + job.ID
	raw, err := os.ReadFile(filepath.Join(root, ".deployment-held-"+questionID+".json"))
	if err != nil {
		return false
	}
	var marker struct {
		QuestionID string `json:"question_id"`
	}
	return json.Unmarshal(raw, &marker) == nil && marker.QuestionID == questionID
}

func (q *Queue) saveLocked(j *Job) {
	q.save(j)
}

func (q *Queue) save(j *Job) {
	_ = q.saveChecked(j)
}

// markWebDispatch must reach disk before the scraper starts; otherwise a
// restart could mistake a sent question for one that never left.
func (q *Queue) markWebDispatch(jobID, site string) error {
	site = internalSiteID(site)
	q.mu.Lock()
	j := q.jobs[jobID]
	if j != nil && j.Status == "cancelled" {
		q.mu.Unlock()
		return context.Canceled
	}
	if j == nil || !j.DispatchTracked {
		q.mu.Unlock()
		return nil
	}
	for _, sent := range j.WebDispatched {
		if sent == site {
			q.mu.Unlock()
			return nil
		}
	}
	j.WebDispatched = append(j.WebDispatched, site)
	saved := copyJob(j)
	q.mu.Unlock()
	return q.saveChecked(saved)
}

func (q *Queue) saveChecked(j *Job) error {
	if q.DataDir == "" || j == nil {
		return nil
	}
	q.persistMu.Lock()
	defer q.persistMu.Unlock()
	// Submission and worker progress can race. Persist the current state so
	// an older queued snapshot cannot overwrite running or finished progress.
	q.mu.Lock()
	if latest := q.jobs[j.ID]; latest != nil {
		j = copyJob(latest)
	}
	q.mu.Unlock()
	if err := os.MkdirAll(q.DataDir, 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(j, "", "  ")
	if err != nil {
		return err
	}
	path := q.jobPath(j.ID)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (q *Queue) jobPath(id string) string {
	if q.DataDir == "" {
		return ""
	}
	return filepath.Join(q.DataDir, id+".json")
}

func (q *Queue) callback(id string) {
	job, ok := q.Get(id)
	if !ok || job.CallbackURL == "" || job.Status == "cancelled" {
		return
	}
	if job.DeliveryPolicy == "eventual" {
		_, completed := compactDeliveryResults(deliveryResults(job))
		if job.Status != "succeeded" || completed != len(job.Sites) {
			return
		}
	}
	publicJob := copyJob(job)
	publicJob.AK = ""
	publicJob.CallbackStatus = ""
	publicJob.CallbackError = ""
	publicJob.CallbackMS = 0
	publicJob.CallbackAttempts = 0
	stripInternalResults(publicJob)
	body, err := json.Marshal(publicJob)
	if err != nil {
		q.patch(id, func(j *Job) {
			j.CallbackStatus = "failed"
			j.CallbackError = "marshal: " + err.Error()
		})
		return
	}
	t0 := time.Now()
	var last error
	for i := 0; i < 3; i++ {
		attempt := i + 1
		q.patch(id, func(j *Job) {
			j.CallbackStatus = "delivering"
			j.CallbackAttempts = attempt
		})
		if err := q.postCallback(job, body); err == nil {
			q.patch(id, func(j *Job) {
				j.CallbackStatus = "ok"
				j.CallbackError = ""
				j.CallbackMS = time.Since(t0).Milliseconds()
			})
			log.Printf("callback %s ok in %dms", id, time.Since(t0).Milliseconds())
			return
		} else {
			last = err
			log.Printf("callback %s try %d: %v", id, attempt, err)
			if i < 2 {
				time.Sleep(time.Duration(1<<i) * time.Second)
			}
		}
	}
	msg := "callback failed"
	if last != nil {
		msg = last.Error()
	}
	q.patch(id, func(j *Job) {
		j.CallbackStatus = "failed"
		j.CallbackError = msg
		j.CallbackMS = time.Since(t0).Milliseconds()
	})
}

func (q *Queue) postCallback(job *Job, body []byte) error {
	if !q.AllowPrivateCallbacks {
		if err := validateCallbackURL(job.CallbackURL); err != nil {
			return err
		}
	}
	req, err := http.NewRequest(http.MethodPost, job.CallbackURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("X-Geo-Job-Id", job.ID)
	if q.Keys != nil && job.AK != "" {
		if k, ok := q.Keys.Lookup(job.AK); ok {
			ts := fmt.Sprintf("%d", time.Now().Unix())
			nonce := hex.EncodeToString(randBytes(8))
			path := callbackPath(job.CallbackURL)
			canon := auth.Canonical("POST", path, ts, nonce, auth.BodyHash(body))
			req.Header.Set(auth.HeaderAK, k.AK)
			req.Header.Set(auth.HeaderTimestamp, ts)
			req.Header.Set(auth.HeaderNonce, nonce)
			req.Header.Set(auth.HeaderSign, auth.Sign(k.SK, canon))
		}
	}
	cli := q.CallbackClient
	if cli == nil {
		cli = &http.Client{
			Timeout: 8 * time.Second,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		}
	}
	resp, err := cli.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("http %d", resp.StatusCode)
	}
	return nil
}

func validateCallbackURL(raw string) error {
	if len(raw) > 2048 {
		return fmt.Errorf("callback_url 过长")
	}
	u, err := url.ParseRequestURI(raw)
	if err != nil || u.Host == "" {
		return fmt.Errorf("callback_url 无效")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("callback_url 仅支持 http/https")
	}
	if u.User != nil {
		return fmt.Errorf("callback_url 不允许包含用户信息")
	}
	if u.Fragment != "" {
		return fmt.Errorf("callback_url 不允许包含 fragment")
	}
	host := u.Hostname()
	if strings.EqualFold(host, "localhost") {
		return fmt.Errorf("callback_url 不允许指向本机或内网")
	}
	ips, err := net.LookupIP(host)
	if err != nil || len(ips) == 0 {
		return fmt.Errorf("callback_url 域名无法解析")
	}
	for _, ip := range ips {
		if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
			ip.IsLinkLocalMulticast() || ip.IsUnspecified() || ip.IsMulticast() {
			return fmt.Errorf("callback_url 不允许指向本机或内网")
		}
	}
	return nil
}

func callbackPath(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Path == "" {
		return "/callback"
	}
	return u.Path
}

func lastJSON(b []byte) []byte {
	lines := bytes.Split(bytes.TrimSpace(b), []byte("\n"))
	for i := len(lines) - 1; i >= 0; i-- {
		line := bytes.TrimSpace(lines[i])
		if len(line) > 0 && line[0] == '{' {
			return line
		}
	}
	return nil
}

func copyJob(j *Job) *Job {
	cp := *j
	if j.ManualStates != nil {
		cp.ManualStates = make(map[string]ManualState, len(j.ManualStates))
		for site, state := range j.ManualStates {
			cp.ManualStates[site] = state
		}
	}
	if j.SiteExecutionModes != nil {
		cp.SiteExecutionModes = make(map[string]string, len(j.SiteExecutionModes))
		for site, mode := range j.SiteExecutionModes {
			cp.SiteExecutionModes[site] = mode
		}
	}
	if j.RecoveryStates != nil {
		cp.RecoveryStates = make(map[string]map[string]any, len(j.RecoveryStates))
		for site, state := range j.RecoveryStates {
			cloned := make(map[string]any, len(state))
			for key, value := range state {
				cloned[key] = value
			}
			cp.RecoveryStates[site] = cloned
		}
	}
	if j.Results != nil {
		cp.Results = make([]map[string]any, len(j.Results))
		for i, result := range j.Results {
			if result == nil {
				continue
			}
			cp.Results[i] = make(map[string]any, len(result))
			for key, value := range result {
				cp.Results[i][key] = value
			}
		}
	}
	cp.Sites = append([]string(nil), j.Sites...)
	if j.WebDispatched != nil {
		cp.WebDispatched = append([]string(nil), j.WebDispatched...)
	}
	return &cp
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func requestKey(ak, requestID string) string {
	return ak + "\x00" + requestID
}

func stripInternalResults(j *Job) {
	j.RecoveryStates = nil
	j.ManualStates = nil
	for _, result := range j.Results {
		delete(result, "account_id")
		delete(result, "attempts")
		delete(result, "attempts_truncated")
		delete(result, "attempts_dropped")
		if recovery, ok := result["web_recovery"].(map[string]any); ok {
			publicRecovery := make(map[string]any, len(recovery))
			for key, value := range recovery {
				if key != "cli_call_id" {
					publicRecovery[key] = value
				}
			}
			result["web_recovery"] = publicRecovery
		}
		answer := stringField(result, "answer")
		if plain := stringField(result, "answer_plain"); plain != "" {
			answer = plain
		} else {
			answer = cleanAnswerCitationMarkers(answer)
		}
		result["answer"] = answer
		delete(result, "answer_raw")
		delete(result, "answer_plain")
		// Older stored jobs may contain private chat URLs. Keep only public
		// share patterns that can open without an account.
		if !verifiedPublicShareURL(stringField(result, "site_id"), stringField(result, "url")) {
			result["url"] = ""
		}
		// Metadata may be absent, empty or incomplete in older results. Merge
		// actual source URLs instead of letting an empty detail array erase
		// them. Neither conversion nor repeated reads mutate stored history.
		details := normalizeCitationDetails(result["citation_details"])
		details = normalizeCitationDetails(append(details, normalizeCitationDetails(result["citations"])...))
		result["citations"] = details
		result["citation_details"] = details
		if len(details) > 0 {
			result["citations_status"] = "available"
		} else if _, exists := result["citations_status"]; exists {
			result["citations_status"] = "missing"
		}
		delete(result, "citation_domains")
	}
}

var (
	answerCitationMarkerRE = regexp.MustCompile(`(?i)[\[【][[:space:]]*(citation|reference)[[:space:]]*:[[:space:]]*[0-9]+([[:space:]]*[,，-][[:space:]]*[0-9]+)*[[:space:]]*[\]】]`)
	answerTrailingSpaceRE  = regexp.MustCompile(`[ \t]+\n`)
)

func cleanAnswerCitationMarkers(answer string) string {
	answer = answerCitationMarkerRE.ReplaceAllString(answer, "")
	answer = answerTrailingSpaceRE.ReplaceAllString(answer, "\n")
	return strings.TrimSpace(answer)
}

func verifiedPublicShareURL(siteID, raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Fragment != "" {
		return false
	}
	host := strings.ToLower(u.Host)
	path := u.Path
	switch siteID {
	case "deepseek":
		return host == "chat.deepseek.com" && u.RawQuery == "" && sharePathID(path, "/share/")
	case "qianwen":
		if host == "qianwen.my.cn" {
			return path != "" &&
				sharePathID(path, "/share/chat/") &&
				len(u.Query()) == 1 &&
				u.Query().Get("lang") == "zh-CN"
		}
		if host != "www.qianwen.com" {
			return false
		}
		if path == "/share" && shareIDOK(u.Query().Get("shareId")) && len(u.Query()) == 1 {
			return true
		}
		return u.RawQuery == "" && (sharePathID(path, "/share/chat/") || sharePathID(path, "/share/"))
	case "yuanbao":
		// Observed short codes such as YztGTQLWVktv are letters only.
		// The shared digit check would drop them from the public job.
		if host == "yb.tencent.com" {
			return u.RawQuery == "" && yuanbaoSharePathID(path, "/s/")
		}
		return host == "yuanbao.tencent.com" && u.RawQuery == "" && yuanbaoSharePathID(path, "/bot/app/share/chat/")
	case "kimi":
		if host != "www.kimi.com" && host != "kimi.com" && host != "kimi.moonshot.cn" && host != "www.kimi.moonshot.cn" {
			return false
		}
		return u.RawQuery == "" && kimiSharePathOK(path)
	case "doubao":
		if host != "www.doubao.com" && host != "doubao.com" || u.RawQuery != "" {
			return false
		}
		return doubaoSharePathID(path, "/thread/") || doubaoSharePathID(path, "/s/")
	case "yiyan", "weixinyiyan", "wenxinyiyan":
		if host == "mr.baidu.com" {
			return sharePathID(path, "/r/") &&
				len(u.Query()) == 2 &&
				u.Query().Get("f") == "ot" &&
				shareIDOK(u.Query().Get("u"))
		}
		if host != "chat.baidu.com" && host != "wenxin.baidu.com" {
			return false
		}
		if path != "/csaitab/history/share" {
			return false
		}
		version := u.Query().Get("v")
		if version != "" && version != "2" {
			return false
		}
		return yiyanShareIDOK(u.Query().Get("share_id"))
	default:
		return false
	}
}

func kimiSharePathOK(path string) bool {
	rest := strings.TrimPrefix(path, "/share/")
	if rest == path {
		return false
	}
	if !strings.Contains(rest, "/") {
		return shareIDOK(rest)
	}
	lang, id, ok := strings.Cut(rest, "/")
	if !ok || id == "" || strings.Contains(id, "/") {
		return false
	}
	if len(lang) < 2 || len(lang) > 8 {
		return false
	}
	for _, ch := range lang {
		if ch < 'A' || ch > 'z' || (ch > 'Z' && ch < 'a') {
			return false
		}
	}
	return shareIDOK(id)
}

func yuanbaoSharePathID(path, prefix string) bool {
	if !strings.HasPrefix(path, prefix) {
		return false
	}
	id := strings.TrimPrefix(path, prefix)
	if len(id) < 8 || strings.Contains(id, "/") {
		return false
	}
	for _, ch := range id {
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '_' || ch == '-') {
			return false
		}
	}
	return true
}

func sharePathID(path, prefix string) bool {
	if !strings.HasPrefix(path, prefix) {
		return false
	}
	id := strings.TrimPrefix(path, prefix)
	return shareIDOK(id)
}

func shareIDOK(id string) bool {
	switch strings.ToLower(id) {
	case "create", "share", "chat", "api", "index", "thread", "search", "talk", "app", "bot":
		return false
	}
	if len(id) < 8 {
		return false
	}
	hasDigit := false
	for _, ch := range id {
		if ch >= '0' && ch <= '9' {
			hasDigit = true
		}
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '_' || ch == '-') {
			return false
		}
	}
	return hasDigit
}

func yiyanShareIDOK(id string) bool {
	if len(id) < 8 {
		return false
	}
	for _, ch := range id {
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '_' || ch == '-' || ch == '.' || ch == '@') {
			return false
		}
	}
	return true
}

func doubaoSharePathID(path, prefix string) bool {
	if !strings.HasPrefix(path, prefix) {
		return false
	}
	id := strings.TrimPrefix(path, prefix)
	if len(id) < 8 {
		return false
	}
	for _, ch := range id {
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9') {
			return false
		}
	}
	return true
}

func normalizeCitationDetails(raw any) []map[string]any {
	details := make([]map[string]any, 0)
	byURL := map[string]map[string]any{}
	appendDetail := func(item map[string]any) {
		rawURL := strings.TrimSpace(stringField(item, "url"))
		u, err := url.Parse(rawURL)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil {
			return
		}
		if previous := byURL[rawURL]; previous != nil {
			for _, field := range []string{"icon", "site_name", "title"} {
				if stringField(previous, field) == "" {
					previous[field] = stringField(item, field)
				}
			}
			return
		}
		index := len(details) + 1
		detail := map[string]any{
			"icon":      stringField(item, "icon"),
			"index":     index,
			"site_name": stringField(item, "site_name"),
			"title":     stringField(item, "title"),
			"url":       rawURL,
		}
		if detail["site_name"] == "" {
			detail["site_name"] = strings.TrimPrefix(u.Hostname(), "www.")
		}
		byURL[rawURL] = detail
		details = append(details, detail)
	}
	switch items := raw.(type) {
	case []any:
		for _, item := range items {
			if detail, ok := item.(map[string]any); ok {
				appendDetail(detail)
			} else if rawURL, ok := item.(string); ok {
				appendDetail(map[string]any{"url": rawURL})
			}
		}
	case []string:
		for _, rawURL := range items {
			appendDetail(map[string]any{"url": rawURL})
		}
	case []map[string]any:
		for _, item := range items {
			appendDetail(item)
		}
	case []map[string]string:
		for _, item := range items {
			appendDetail(map[string]any{
				"icon": item["icon"], "site_name": item["site_name"],
				"title": item["title"], "url": item["url"],
			})
		}
	}
	return details
}

func stringField(item map[string]any, key string) string {
	if value, ok := item[key].(string); ok {
		return value
	}
	return ""
}

func basicCitationDetail(raw string, index int) map[string]any {
	siteName := ""
	if parsed, err := url.Parse(raw); err == nil {
		siteName = strings.TrimPrefix(strings.ToLower(parsed.Hostname()), "www.")
	}
	return map[string]any{
		"icon": "", "index": index, "site_name": siteName, "title": "", "url": raw,
	}
}

func newID() (string, error) {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func randBytes(n int) []byte {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return b
}

func envInt(name string, def, min, max int) int {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < min || n > max {
		return def
	}
	return n
}
