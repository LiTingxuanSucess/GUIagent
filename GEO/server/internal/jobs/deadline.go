package jobs

import (
	"context"
	"strings"
	"time"
)

// Legacy request estimates remain available for older persisted records.
// Web-first jobs use independent phase budgets, beginning after admission.
func requestBudget(sites int) time.Duration {
	if sites <= 1 {
		return time.Duration(envInt("GEO_SINGLE_TOTAL_TIMEOUT_SEC", 120, 1, 300)) * time.Second
	}
	return time.Duration(envInt("GEO_MULTI_TOTAL_TIMEOUT_SEC", 300, 1, 900)) * time.Second
}

func requestBudgetForSites(sites []string) time.Duration {
	budget := requestBudget(len(sites))
	if len(sites) == 1 {
		id := internalSiteID(sites[0])
		if id == "doubao" || id == "deepseek" || id == "kimi" || id == "qianwen" || id == "yiyan" || id == "yuanbao" {
			budget = time.Duration(apiEnvInt("GEO_"+strings.ToUpper(id)+"_SINGLE_TOTAL_TIMEOUT_SEC", int(budget/time.Second), 1, 300)) * time.Second
		}
	}
	return budget
}

func jobDeadline(job *Job) time.Time {
	if deadline, err := time.Parse(time.RFC3339Nano, job.DeadlineAt); err == nil {
		return deadline
	}
	start, err := time.Parse(time.RFC3339Nano, job.CreatedAt)
	if err != nil {
		start = time.Now()
	}
	return start.Add(requestBudgetForSites(job.Sites))
}

// Resource waiting never spends the webpage's execution budget. An explicit
// caller cancellation/deadline still cancels admission normally.
func browserAdmissionContext(parent context.Context, site string) (context.Context, context.CancelFunc) {
	return context.WithCancel(parent)
}

// Account pools use the same cancellation-only resource wait.
func poolBrowserAdmissionContext(parent context.Context, site string) (context.Context, context.CancelFunc) {
	return context.WithCancel(parent)
}

func minimumWebWindow(site string) time.Duration {
	seconds := envInt("GEO_MIN_WEB_EXECUTION_SEC", 45, 1, 120)
	seconds = apiEnvInt("GEO_"+strings.ToUpper(internalSiteID(site))+"_MIN_WEB_EXECUTION_SEC", seconds, 1, 120)
	return time.Duration(seconds) * time.Second
}

func sessionWaitContext(parent context.Context, site string) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, time.Duration(envInt("GEO_SESSION_ACQUIRE_WAIT_SEC", 120, 1, 300))*time.Second)
}

func requestWebDeadline(ctx context.Context, site string, deadline, start time.Time, siteCount int) time.Time {
	if overall, ok := ctx.Deadline(); ok {
		if overall.Before(deadline) {
			deadline = overall
		}
	}
	limit := time.Duration(envInt("GEO_MULTI_WEB_BUDGET_SEC", 240, 1, 900)) * time.Second
	if siteCount <= 1 {
		limit = time.Duration(envInt("GEO_SINGLE_WEB_BUDGET_SEC", 240, 1, 900)) * time.Second
	}
	if id := internalSiteID(site); (id == "deepseek" || id == "doubao") && officialAPIConfigured(site) {
		// A site value may be shorter or, for a slow generator, longer than
		// the shared 240s window. Doubao answers were ending at that shared cap.
		limit = time.Duration(apiEnvInt("GEO_"+strings.ToUpper(id)+"_WEB_BUDGET_SEC", int(limit/time.Second), 45, 900)) * time.Second
	}
	if capped := start.Add(limit); capped.Before(deadline) {
		deadline = capped
	}
	return deadline
}

func apiReserve(site string) time.Duration {
	reserve := time.Duration(envInt("GEO_API_RESERVE_SEC", 35, 5, 90)) * time.Second
	id := internalSiteID(site)
	configuredThinkingBudget := (id == "kimi" || id == "qianwen" || id == "yiyan" || id == "yuanbao") && geoEnv("GEO_"+strings.ToUpper(id)+"_API_TIMEOUT_SEC") != ""
	if (id == "deepseek" || id == "doubao" || configuredThinkingBudget) && apiTimeout(site) > reserve {
		reserve = apiTimeout(site)
	}
	return reserve
}

type webExecutionBudgetKey struct{}

// Advertise a moving execution estimate. This is deliberately not a parent
// context deadline: another site's queue/setup must not cancel this site.
func (q *Queue) extendJobDeadline(id string, deadline time.Time) {
	q.patchIf(id, func(j *Job) bool {
		if j.Status != "running" || (j.DeadlineAt != "" && !deadline.After(jobDeadline(j))) {
			return false
		}
		j.DeadlinePolicy = "web_first"
		j.DeadlineAt = deadline.Format(time.RFC3339Nano)
		return true
	})
}

func (q *Queue) webpageDeadline(parent context.Context, site, jobID string, siteCount int) time.Time {
	now := time.Now()
	deadline := requestWebDeadline(parent, site, now.Add(siteAttemptTimeout()), now, siteCount)
	q.extendJobDeadline(jobID, deadline.Add(apiTimeout(site)))
	return deadline
}
