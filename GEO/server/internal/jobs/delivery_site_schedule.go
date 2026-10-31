package jobs

import "time"

func deliveryPermanentAPIRejection(row map[string]any) bool {
	switch stringField(row, "api_error_code") {
	case "api_auth_failed", "api_quota_exhausted", "api_risk_control":
		return true
	}
	return false
}

func deliverySiteRetryTime(row map[string]any) (time.Time, bool) {
	at, err := time.Parse(time.RFC3339Nano, stringField(row, "delivery_retry_at"))
	return at, err == nil
}

func deliverySiteDue(row map[string]any, now time.Time) bool {
	if validDelivery(row) {
		return false
	}
	at, known := deliverySiteRetryTime(row)
	return !known || !now.Before(at)
}

func deliveryResourceSiteDue(job *Job, site string, now time.Time) bool {
	for _, row := range job.Results {
		if internalSiteID(stringField(row, "site_id")) == internalSiteID(site) {
			return resourceAdmissionResult(row) && deliverySiteDue(row, now)
		}
	}
	return false
}

func hasDueDeliveryResources(job *Job, now time.Time) bool {
	for _, site := range job.Sites {
		if deliveryResourceSiteDue(job, site, now) {
			return true
		}
	}
	return false
}

func hasPendingDeliveryResources(job *Job) bool {
	for _, row := range job.Results {
		if resourceAdmissionResult(row) {
			for _, site := range job.Sites {
				if internalSiteID(site) == internalSiteID(stringField(row, "site_id")) {
					return true
				}
			}
		}
	}
	return false
}

func copyDeliveryScheduleRow(row map[string]any) map[string]any {
	copy := make(map[string]any, len(row)+3)
	for key, value := range row {
		copy[key] = value
	}
	return copy
}

func legacyDeliverySiteRetryCount(row map[string]any, fallback int) int {
	if _, known := row["delivery_retry_count"]; known {
		return evidenceCount(row["delivery_retry_count"])
	}
	if row == nil || resourceAdmissionResult(row) && !hasActualWebEvidence(row) && row["attempts_truncated"] != true && evidenceCount(row["attempts_dropped"]) == 0 {
		return 0
	}
	return fallback
}

// Old jobs only persisted a job-wide retry time. Preserve that time for an
// operator/provider wait before advancing its resource-waiting siblings.
// Resource waits without a platform time are immediately eligible, so an old
// mixed five-minute backoff cannot keep a ready browser ticket offline.
func ensureDeliverySiteSchedule(job *Job, now time.Time) bool {
	changed := false
	rows := append([]map[string]any(nil), job.Results...)
	parentAt, parentErr := time.Parse(time.RFC3339Nano, job.RetryAt)
	for i, previous := range rows {
		if validDelivery(previous) || previous == nil {
			continue
		}
		if _, known := deliverySiteRetryTime(previous); known {
			continue
		}
		row := copyDeliveryScheduleRow(previous)
		at := now
		state, reason := deliveryWaitState(stringField(row, "site_id"), row)
		if !resourceAdmissionResult(row) {
			if parentErr == nil && parentAt.After(now) {
				at = parentAt
			} else if parentErr != nil && (state == "operator_wait" || job.RetryCount > 0) {
				at = now.Add(deliveryDelay(max(1, job.RetryCount), state))
			}
		}
		row["delivery_retry_at"] = at.Format(time.RFC3339Nano)
		row["delivery_retry_count"] = legacyDeliverySiteRetryCount(previous, job.RetryCount)
		row["delivery_retry_reason"] = reason
		rows[i], changed = row, true
	}
	if changed {
		job.Results = rows
	}
	return changed
}

func scheduleDeliverySiteRetry(site string, previous, row map[string]any, fallbackCount int, now time.Time) {
	if validDelivery(row) {
		delete(row, "delivery_retry_at")
		delete(row, "delivery_retry_count")
		delete(row, "delivery_retry_reason")
		return
	}
	state, reason := deliveryWaitState(site, row)
	count := legacyDeliverySiteRetryCount(previous, fallbackCount)
	delay := resourceAdmissionRetryDelay()
	if stringField(row, "error_code") == "recovery_queue_busy" {
		delay = time.Duration(envInt("GEO_RECOVERY_RETRY_SEC", 10, 2, 60)) * time.Second
	}
	if !resourceAdmissionResult(row) && stringField(row, "error_code") != "recovery_queue_busy" {
		count++
		delay = deliveryDelay(count, state)
	}
	if seconds := evidenceCount(row["retry_after_sec"]); time.Duration(seconds)*time.Second > delay {
		delay = time.Duration(seconds) * time.Second
	}
	row["delivery_pending"], row["retry_state"] = true, state
	row["delivery_retry_at"] = now.Add(delay).Format(time.RFC3339Nano)
	row["delivery_retry_count"], row["delivery_retry_reason"] = count, reason
}

func nextDeliverySiteRetry(rows []map[string]any, now time.Time) (time.Time, string) {
	var next time.Time
	reason := "empty_answer"
	for _, row := range rows {
		if validDelivery(row) {
			continue
		}
		at, known := deliverySiteRetryTime(row)
		if !known {
			at = now
		}
		if next.IsZero() || at.Before(next) {
			next = at
			reason = stringField(row, "delivery_retry_reason")
			if reason == "" {
				_, reason = deliveryWaitState(stringField(row, "site_id"), row)
			}
		}
	}
	if next.IsZero() {
		next = now.Add(resourceAdmissionRetryDelay())
	}
	return next, reason
}

// Caller holds q.mu. Copy each changed row so published snapshots keep their
// original operator/provider times. Only browser capacity waits are advanced.
func wakeDeliveryResourceSites(job *Job, now time.Time) bool {
	changed := ensureDeliverySiteSchedule(job, now)
	rows := append([]map[string]any(nil), job.Results...)
	for i, previous := range rows {
		if !resourceAdmissionResult(previous) || deliverySiteDue(previous, now) {
			continue
		}
		row := copyDeliveryScheduleRow(previous)
		row["delivery_retry_at"] = now.Format(time.RFC3339Nano)
		rows[i], changed = row, true
	}
	if changed {
		job.Results = rows
	}
	at, reason := nextDeliverySiteRetry(job.Results, now)
	if job.RetryAt != at.Format(time.RFC3339Nano) || job.WaitReason != reason {
		job.RetryAt, job.WaitReason, changed = at.Format(time.RFC3339Nano), reason, true
	}
	return changed
}
