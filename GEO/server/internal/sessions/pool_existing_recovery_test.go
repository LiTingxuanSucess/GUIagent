package sessions

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/chromedp/cdproto/target"
	"github.com/chromedp/chromedp"
)

func attachedExistingRecoveryFixture(t *testing.T) (*Pool, *Manager, context.Context) {
	t.Helper()
	p := recoveryLeaseFixture(t)
	m := p.runtimes["healthy"]
	allocator, cancelAllocator := chromedp.NewExecAllocator(context.Background(), chromedp.ExecPath("/nonexistent/chrome"))
	ctx, cancel := chromedp.NewContext(allocator)
	state := chromedp.FromContext(ctx)
	state.Target = &chromedp.Target{TargetID: target.ID("original-target")}
	m.tabs["doubao"] = ctx
	m.opened["doubao"] = false
	t.Cleanup(func() {
		// The context carries a synthetic target only; never send a CDP close.
		state.Target = nil
		cancel()
		cancelAllocator()
	})
	return p, m, ctx
}

func TestExistingRecoveryLeaseReusesExactLoggedInAccountTargetWithoutNavigation(t *testing.T) {
	p, m, tab := attachedExistingRecoveryFixture(t)
	prior := p.accounts["healthy"].Sites["doubao"]
	lease, err := p.AcquireExistingForRecoveryContext(context.Background(), "doubao", "healthy")
	if err != nil || lease.AccountID != "healthy" || lease.Manager != m {
		t.Fatalf("existing logged-in page was not leased: lease=%v err=%v", lease, err)
	}
	defer lease.Release()
	if m.tabs["doubao"] != tab || m.opened["doubao"] || chromedp.FromContext(tab).Target.TargetID != "original-target" || p.accounts["healthy"].Sites["doubao"] != prior {
		t.Fatal("existing recovery navigated, replaced target or refreshed persisted login")
	}
	if m.TryAcquireSite("doubao") {
		m.ReleaseSite("doubao")
		t.Fatal("original target was not protected by the returned site lease")
	}
	if guard, err := lease.BeginWebRecovery(); err != nil {
		t.Fatal("existing target lease was incompatible with the recovery guard")
	} else {
		guard()
	}
}

func TestExistingRecoveryMissingRuntimeTargetOrCDPFailsWithoutSubstitution(t *testing.T) {
	for _, failure := range []string{"runtime", "tab", "target", "cdp", "disabled"} {
		t.Run(failure, func(t *testing.T) {
			p, m, tab := attachedExistingRecoveryFixture(t)
			switch failure {
			case "runtime":
				delete(p.runtimes, "healthy")
			case "target":
				chromedp.FromContext(tab).Target = nil
			case "tab":
				delete(m.tabs, "doubao")
			case "cdp":
				if err := os.Remove(filepath.Join(m.ProfileDir, "DevToolsActivePort")); err != nil {
					t.Fatal(err)
				}
			case "disabled":
				p.accounts["healthy"].Enabled = false
			}
			before := len(p.runtimes)
			lease, err := p.AcquireExistingForRecoveryContext(context.Background(), "doubao", "healthy")
			if lease != nil || err == nil || len(p.runtimes) != before || m.Busy() || m.opened["doubao"] {
				t.Fatalf("missing original page started/substituted a browser or leaked lease: lease=%v err=%v", lease, err)
			}
		})
	}
}

func TestExistingRecoveryLeaseBusyWaitRespectsCancellation(t *testing.T) {
	p, m, _ := attachedExistingRecoveryFixture(t)
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	_, err := p.AcquireExistingForRecoveryContext(ctx, "doubao", "healthy")
	if !errors.Is(err, context.DeadlineExceeded) || !m.isBusy("doubao") {
		t.Fatalf("busy wait ignored cancellation or released another action's lease: %v", err)
	}
}
