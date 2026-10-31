package sessions

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func cancelledCheckFixture(t *testing.T) (*Pool, State) {
	t.Helper()
	p := NewPool("/nonexistent/chrome", t.TempDir(), "13800138000", filepath.Join(t.TempDir(), "accounts.json"))
	prior := State{SiteID: "doubao", Name: "豆包", Status: "unknown", URL: "https://www.doubao.com/chat/prior", Message: "prior message", Evidence: "prior evidence", Phone: "13800138000", LastSendAt: 1234.5, LastCheckedAt: "prior check", LastLoginAt: "prior login", UpdatedAt: "prior update"}
	p.accounts["default"].Sites["doubao"] = prior
	return p, prior
}

func TestCancelledCheckRestoresCompletePersistedState(t *testing.T) {
	p, prior := cancelledCheckFixture(t)
	snapshot, ok := p.beginCheckState("default", "doubao", nil)
	if !ok || snapshot.checking.Status != "checking" || snapshot.prior != prior {
		t.Fatal("probe did not preserve its original complete state")
	}
	if !p.restoreCancelledCheck("default", "doubao", snapshot, nil, nil) {
		t.Fatal("cancelled probe was not restored")
	}
	if got := p.accounts["default"].Sites["doubao"]; got != prior {
		t.Fatalf("cancelled probe changed state metadata: %#v", got)
	}
	reloaded := NewPool("/nonexistent/chrome", t.TempDir(), "", p.DataPath)
	if got := reloaded.accounts["default"].Sites["doubao"]; got != prior {
		t.Fatalf("restored state was not persisted: %#v", got)
	}
}

func TestCancelledCheckDoesNotOverwriteNewerState(t *testing.T) {
	for _, status := range []string{"need_captcha", "expired", "not_logged_in", "logged_in", "checking"} {
		t.Run(status, func(t *testing.T) {
			p, _ := cancelledCheckFixture(t)
			snapshot, _ := p.beginCheckState("default", "doubao", nil)
			newer := snapshot.checking
			newer.Status, newer.Evidence, newer.UpdatedAt = status, "new positive decision", "new update"
			p.accounts["default"].Sites["doubao"] = newer
			if p.restoreCancelledCheck("default", "doubao", snapshot, nil, nil) {
				t.Fatal("cancelled probe replaced another operation's decision")
			}
			if got := p.accounts["default"].Sites["doubao"]; got != newer {
				t.Fatal("newer state was changed")
			}
		})
	}
}

func TestCancelledCheckRollsBackOnlyItsOwnIndeterminateRuntimeRead(t *testing.T) {
	for _, updated := range []string{"error", "need_captcha", "expired", "not_logged_in", "logged_in"} {
		t.Run(updated, func(t *testing.T) {
			p, prior := cancelledCheckFixture(t)
			m := New("/nonexistent/chrome", t.TempDir(), prior.Phone)
			snapshot, _ := p.beginCheckState("default", "doubao", m)
			observed := State{SiteID: "doubao", Status: "error", Message: "read interrupted", UpdatedAt: "own observation"}
			current := observed
			current.Status = updated
			m.states["doubao"] = &current
			p.restoreCancelledCheck("default", "doubao", snapshot, m, &observed)
			if updated == "error" {
				if *m.states["doubao"] != snapshot.runtimePrior {
					t.Fatal("an interrupted runtime read could poison a future account sync")
				}
			} else if *m.states["doubao"] != current {
				t.Fatal("explicit runtime captcha/login/logout evidence was overwritten")
			}
		})
	}
}

func TestCheckContextCancellationDoesNotLeaveCheckingOrStartBrowser(t *testing.T) {
	p, prior := cancelledCheckFixture(t)
	m := New("/nonexistent/chrome", t.TempDir(), prior.Phone)
	// Reuse a fixture runtime with an invalid CDP context. It cannot create a
	// browser or make a network request; EnsurePage's retry wait is cancellable.
	m.started, m.ready = true, true
	m.tabs["doubao"] = context.Background()
	p.runtimes["default"] = m
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err := p.checkContext(ctx, "default", "doubao", true)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("probe did not stop at its caller's deadline: %v", err)
	}
	if got := p.accounts["default"].Sites["doubao"]; got != prior {
		t.Fatalf("cancelled check persisted a synthetic account state: %#v", got)
	}
	if m.Busy() || len(p.opSem) != 0 {
		t.Fatal("cancelled probe retained its site lock or operation slot")
	}
}
