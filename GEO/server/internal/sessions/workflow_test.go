package sessions

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
)

func TestWorkflowReservationBlocksWholeBrowserAndBackgroundLogin(t *testing.T) {
	p := NewPool("/not/chrome", t.TempDir(), "13800138000", filepath.Join(t.TempDir(), "accounts.json"))
	if _, err := p.ReserveWorkflow("job-a/kimi", "kimi", "default"); err != nil {
		t.Fatal(err)
	}
	if _, err := p.ReserveWorkflow("job-b/doubao", "doubao", "default"); !errors.Is(err, ErrWorkflowBusy) {
		t.Fatal("another platform took manual browser", err)
	}
	if p.automaticLoginStillNeeded("default", "doubao") {
		t.Fatal("background login can alter reserved browser")
	}
	if _, err := p.managerForLockedContext(context.Background(), "default"); !errors.Is(err, ErrWorkflowBusy) {
		t.Fatal("normal operation entered reserved browser", err)
	}
	if _, err := p.SetEnabled("default", false); !errors.Is(err, ErrWorkflowBusy) {
		t.Fatal("reserved account disabled", err)
	}
	if err := p.Delete("default"); !errors.Is(err, ErrWorkflowBusy) {
		t.Fatal("reserved account deleted", err)
	}
	other, err := p.Add("13900139000")
	if err != nil {
		t.Fatal(err)
	}
	chosen, err := p.ReserveWorkflow("job-b/doubao", "doubao", "")
	if err != nil || chosen.ID != other.ID {
		t.Fatal("independent account cannot continue", err)
	}
	p.ReleaseWorkflow("job-a/kimi")
	if _, err := p.ReserveWorkflow("job-c/kimi", "kimi", "default"); err != nil {
		t.Fatal("release left browser reserved", err)
	}
}

func TestWorkflowSixSitesAreIndependentAndSameSiteRemainsExclusive(t *testing.T) {
	t.Setenv("GEO_WORKFLOW_SITE_ISOLATION", "1")
	p := NewPool("/not/chrome", t.TempDir(), "13800138000", filepath.Join(t.TempDir(), "accounts.json"))
	for _, site := range Sites {
		if _, err := p.ReserveWorkflow("job/"+site.ID, site.ID, "default"); err != nil {
			t.Fatal(site.ID, err)
		}
	}
	if len(p.workflowSiteOwners) != 6 {
		t.Fatalf("wanted six owned tabs: %v", p.workflowSiteOwners)
	}
	if _, err := p.ReserveWorkflow("another/kimi", "kimi", "default"); !errors.Is(err, ErrWorkflowBusy) {
		t.Fatal("same tab reused", err)
	}
	ctx := context.WithValue(context.Background(), workflowContextKey{}, "job/kimi")
	ctx = context.WithValue(ctx, workflowSiteContextKey{}, "kimi")
	if !p.workflowAccessAllowedLocked("default", ctx) || p.workflowAccessAllowedLocked("default", context.Background()) {
		t.Fatal("workflow scope leaked")
	}
	wrong := context.WithValue(ctx, workflowSiteContextKey{}, "doubao")
	if p.workflowAccessAllowedLocked("default", wrong) {
		t.Fatal("owner entered different tab")
	}
	p.ReleaseWorkflow("job/kimi")
	if _, err := p.ReserveWorkflow("another/kimi", "kimi", "default"); err != nil {
		t.Fatal(err)
	}
	if _, err := p.SetEnabled("default", false); !errors.Is(err, ErrWorkflowBusy) {
		t.Fatal("disabled account with live workflows", err)
	}
	p.ReleaseWorkflow("another/kimi")
	for _, site := range Sites {
		p.ReleaseWorkflow("job/" + site.ID)
	}
	if len(p.workflowOwners) != 0 || len(p.workflowSiteOwners) != 0 {
		t.Fatal("reservation leaked")
	}
}
