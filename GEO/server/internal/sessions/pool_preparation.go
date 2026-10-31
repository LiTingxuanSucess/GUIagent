package sessions

import (
	"context"
	"errors"
	"fmt"
)

// PreparationError contains observations from this acquisition only. Waiting
// for a busy account and caller cancellation are never preparation failures.
type PreparationError struct {
	Site         string
	CandidateIDs []string
	FailedIDs    []string
	FailureCodes map[string]string
	cause        error
}

func (e *PreparationError) Error() string {
	return fmt.Sprintf("%s 网页账号准备未完成", e.Site)
}

func (e *PreparationError) Unwrap() error { return e.cause }

func (e *PreparationError) AllCandidatesFailed() bool {
	if e == nil || len(e.CandidateIDs) == 0 || errors.Is(e.cause, context.Canceled) || errors.Is(e.cause, context.DeadlineExceeded) {
		return false
	}
	for _, id := range e.CandidateIDs {
		if e.FailureCodes[id] == "" {
			return false
		}
	}
	return true
}

func (e *PreparationError) candidates(ids []string) {
	for _, id := range ids {
		found := false
		for _, known := range e.CandidateIDs {
			if known == id {
				found = true
				break
			}
		}
		if !found {
			e.CandidateIDs = append(e.CandidateIDs, id)
		}
	}
}

func (e *PreparationError) failed(id, code string) {
	if e.FailureCodes == nil {
		e.FailureCodes = make(map[string]string)
	}
	if e.FailureCodes[id] == "" {
		e.FailedIDs = append(e.FailedIDs, id)
	}
	e.FailureCodes[id] = code
}

func (e *PreparationError) withCause(cause error) error {
	e.cause = cause
	return e
}

// The internal marker distinguishes a completed browser-start error from a
// busy account, disabled account, and an interrupted StartContext operation.
type preparationStageFailure struct {
	code  string
	cause error
}

func (e *preparationStageFailure) Error() string { return e.cause.Error() }
func (e *preparationStageFailure) Unwrap() error { return e.cause }

func preparationStartError(ctx context.Context, err error) error {
	if err != nil && ctx.Err() == nil {
		// A browser's internal startup timeout is a completed operation
		// failure when the caller is still live; it is not lock waiting.
		return &preparationStageFailure{code: "browser_start_failed", cause: err}
	}
	return err
}
