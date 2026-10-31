package jobs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"geo-server/internal/sessions"
)

type workflowPageOpener interface {
	OpenWorkflowPage(context.Context, string, string, string, string, string) (sessions.WorkflowPage, error)
}

type workflowPageCreator interface {
	NewWorkflowPage(context.Context, string, string, string) (sessions.WorkflowPage, error)
}

func (q *Queue) boundManualJournal(job *Job, site string, state ManualState) ([]byte, map[string]any, error) {
	raw, err := os.ReadFile(q.webRecoveryPath(job.ID, site, state.AccountID))
	if err != nil {
		return nil, nil, err
	}
	var journal map[string]any
	sum := sha256.Sum256([]byte(job.Prompt))
	if json.Unmarshal(raw, &journal) != nil || journal["site_id"] != internalSiteID(site) ||
		journal["account_id"] != state.AccountID || journal["question_id"] != "job_"+job.ID ||
		journal["prompt_sha256"] != hex.EncodeToString(sum[:]) || journal["prompt"] != job.Prompt ||
		journal["target_id"] != state.TargetID || journal["page_url"] != state.ConversationURL {
		return nil, nil, fmt.Errorf("原对话记录与本任务不一致，不能自动重开")
	}
	return raw, journal, nil
}

func writeManualOpenJournal(path string, raw []byte) error {
	tmp := path + ".open.tmp"
	if err := os.WriteFile(tmp, raw, 0600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

// manualMu is held by the caller: reopening and updating the original binding
// must finish before an operator or auto-recovery can queue continuation.
// Merely viewing a page never queues, sends, or changes the question/send state.
func (q *Queue) openManualPage(ctx context.Context, opener workflowPageOpener, job *Job, site string, state ManualState) (*Job, error) {
	raw, journal, journalErr := q.boundManualJournal(job, site, state)
	savedURL := state.ConversationURL
	if journalErr != nil {
		savedURL = "" // Existing target can still be focused, but no unsafe rebinding.
	}
	page, openErr := opener.OpenWorkflowPage(ctx, manualOwner(job.ID, site), state.AccountID, internalSiteID(site), state.TargetID, savedURL)
	if page.Reopened && page.TargetID != "" && page.TargetID != state.TargetID {
		if journalErr != nil || page.URL != savedURL || savedURL == "" {
			return nil, fmt.Errorf("已定位页面，但原对话记录不能安全绑定，请核对本题")
		}
		if err := q.bindManualPage(job, site, state, page, raw, journal, false); err != nil {
			return nil, err
		}
	}
	if openErr != nil {
		return nil, openErr
	}
	out, _ := q.Get(job.ID)
	return out, nil
}

func (q *Queue) newManualPage(ctx context.Context, creator workflowPageCreator, job *Job, site string, state ManualState) (*Job, error) {
	raw, journal, err := q.boundManualJournal(job, site, state)
	// Acquisition can fail before the first page/journal is ever created.
	// Initialize only this provably unstarted case; never reset a known send.
	if os.IsNotExist(err) && unstartedManualSite(job, site, state) {
		if err = q.createManualJournal(job, site, state); err == nil {
			raw, journal, err = q.boundManualJournal(job, site, state)
		}
	}
	if err != nil {
		return nil, fmt.Errorf("本题原记录无法核对，未替换原标签页: %w", err)
	}
	page, openErr := creator.NewWorkflowPage(ctx, manualOwner(job.ID, site), state.AccountID, internalSiteID(site))
	if page.TargetID != "" {
		if err := q.bindManualPage(job, site, state, page, raw, journal, true); err != nil {
			return nil, err
		}
	}
	if openErr != nil {
		return nil, openErr
	}
	out, _ := q.Get(job.ID)
	return out, nil
}

func unstartedManualSite(job *Job, site string, state ManualState) bool {
	if state.TargetID != "" || state.SendState != "" || state.ConversationURL != "" || len(state.TurnHistory) != 0 {
		return false
	}
	for _, result := range job.Results {
		resultSite, _ := result["site_id"].(string)
		if resultSite == "" || internalSiteID(resultSite) == internalSiteID(site) {
			return false
		}
	}
	for _, dispatched := range job.WebDispatched {
		if internalSiteID(dispatched) == internalSiteID(site) {
			return false
		}
	}
	return true
}

func (q *Queue) bindManualPage(job *Job, site string, state ManualState, page sessions.WorkflowPage, raw []byte, journal map[string]any, newTab bool) error {
	q.mu.Lock()
	current := q.jobs[job.ID]
	if current == nil || current.Status == "cancelled" || q.activeRuns[job.ID] || current.ManualStates[site].Revision != state.Revision {
		q.mu.Unlock()
		return ErrManualConflict
	}
	next := copyJob(current)
	q.mu.Unlock()
	history, _ := journal["manual_turn_history"].([]any)
	audit := map[string]any{"reason": "reopened_original_conversation", "previous_target_id": state.TargetID, "target_id": page.TargetID, "page_url": page.URL, "at": time.Now().Format(time.RFC3339Nano)}
	if newTab {
		audit["reason"] = "operator_created_platform_tab"
		audit["previous_url"] = journal["page_url"]
		audit["previous_send_state"] = journal["send_state"]
		audit["previous_turn"] = journal["manual_turn"]
		audit["previous_baseline"] = journal["manual_baseline"]
		journal["manual_new_tab"] = true
		state.Status = "operator_active"
		state.Action = ""
		if state.SendState == "" {
			state.SendState, _ = journal["send_state"].(string)
		}
		state.Reason = "已新建平台标签页。请从历史记录找回本题，再点击“采集当前已提交的本题”"
		if state.SendState == "before_send" {
			state.Reason = "已新建平台标签页。完成原账号登录后，点击“已完成验证，继续执行”"
		}
	}
	journal["manual_turn_history"] = append(history, audit)
	journal["target_id"] = page.TargetID
	updated, err := json.MarshalIndent(journal, "", "  ")
	if err != nil {
		return err
	}
	path := q.webRecoveryPath(job.ID, site, state.AccountID)
	if err := writeManualOpenJournal(path, updated); err != nil {
		return fmt.Errorf("页面已打开，但保存新标签页失败: %w", err)
	}
	state.TargetID = page.TargetID
	state.Revision++
	state.UpdatedAt = time.Now().Format(time.RFC3339Nano)
	state.TurnHistory = append(state.TurnHistory, audit)
	next.ManualStates[site] = state
	next.UpdatedAt = state.UpdatedAt
	q.mu.Lock()
	if q.jobs[job.ID] != current || current.Status == "cancelled" || q.activeRuns[job.ID] || current.ManualStates[site].Revision != job.ManualStates[site].Revision {
		q.mu.Unlock()
		_ = writeManualOpenJournal(path, raw)
		return ErrManualConflict
	}
	q.jobs[job.ID] = next
	q.mu.Unlock()
	if err := q.saveChecked(next); err != nil {
		q.mu.Lock()
		rollback := q.jobs[job.ID] == next && next.Status != "cancelled"
		if rollback {
			q.jobs[job.ID] = current
		}
		q.mu.Unlock()
		var rollbackErr error
		if rollback {
			rollbackErr = writeManualOpenJournal(path, raw)
		}
		return fmt.Errorf("页面已打开，但保存任务绑定失败: %v（恢复原记录: %v）", err, rollbackErr)
	}
	q.publish(job.ID)
	return nil
}
