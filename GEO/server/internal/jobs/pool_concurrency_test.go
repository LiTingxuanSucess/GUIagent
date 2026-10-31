package jobs

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

type concurrentPool struct{ leases []*sessions.Lease }

func (p *concurrentPool) AcquireContext(ctx context.Context, site string) (*sessions.Lease, error) {
	for {
		for _, lease := range p.leases {
			if lease.Manager.TryAcquireSite("") {
				return lease, nil
			}
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(5 * time.Millisecond):
		}
	}
}

func (p *concurrentPool) AcquireForLoginContext(ctx context.Context, site string) (*sessions.Lease, error) {
	return p.AcquireContext(ctx, site)
}

func (p *concurrentPool) AcquireContextExcept(ctx context.Context, site string, skip map[string]bool) (*sessions.Lease, error) {
	return nil, fmt.Errorf("no retry expected")
}

func (p *concurrentPool) LoggedInAccountIDs(string) []string {
	var ids []string
	for _, lease := range p.leases {
		ids = append(ids, lease.AccountID)
	}
	return ids
}

func concurrentPoolFixture(t *testing.T, accounts []string, body string) *Queue {
	t.Helper()
	t.Setenv("GEO_MIN_WEB_EXECUTION_SEC", "1")
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
	root := t.TempDir()
	pool := &concurrentPool{}
	for _, id := range accounts {
		profile := filepath.Join(root, id)
		if err := os.Mkdir(profile, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte("12345\n"), 0600); err != nil {
			t.Fatal(err)
		}
		pool.leases = append(pool.leases, &sessions.Lease{AccountID: id, Phone: id, Manager: sessions.New("", profile, "")})
	}
	script := filepath.Join(root, "scrape.py")
	if err := os.WriteFile(script, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	return &Queue{GeoRoot: root, Python: "python3", Script: script, Sess: pool, siteSem: make(chan struct{}, 2)}
}

func runConcurrentPoolQuestions(t *testing.T, q *Queue) (map[string]any, map[string]any) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	results := make(chan map[string]any, 2)
	for _, id := range []string{"job-first", "job-second"} {
		go func(id string) { results <- q.runOneSiteContext(ctx, "doubao", "current question", id, 1) }(id)
	}
	return <-results, <-results
}

func TestLocalPoolRunsSamePlatformOnIndependentAccounts(t *testing.T) {
	q := concurrentPoolFixture(t, []string{"first", "second"}, `import json,sys,time
from pathlib import Path
phone=sys.argv[sys.argv.index('--phone')+1]
Path('entered-'+phone).touch()
deadline=time.monotonic()+2
while len(list(Path('.').glob('entered-*')))<2 and time.monotonic()<deadline:time.sleep(.01)
both=len(list(Path('.').glob('entered-*')))==2
print(json.dumps({'ok':both,'answer':'independent account answer' if both else '', 'source':'web-fixture','citations':['https://example.org/real'], 'error_code':'' if both else 'platform_busy'}))
`)
	first, second := runConcurrentPoolQuestions(t, q)
	if first["ok"] != true || second["ok"] != true || first["account_id"] == second["account_id"] {
		t.Fatalf("independent accounts did not overlap: first=%v second=%v", first, second)
	}
}

func TestLocalPoolDoesNotShareAnAccountBetweenQuestions(t *testing.T) {
	q := concurrentPoolFixture(t, []string{"only"}, `import json,os,time
from pathlib import Path
lock=Path('active-account')
try:
 fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
except FileExistsError:
 print(json.dumps({'ok':False,'error_code':'platform_busy'}))
else:
 time.sleep(.1)
 os.close(fd)
 lock.unlink()
 print(json.dumps({'ok':True,'answer':'account stayed exclusive','source':'web-fixture','citations':['https://example.org/real']}))
`)
	first, second := runConcurrentPoolQuestions(t, q)
	if first["ok"] != true || second["ok"] != true || first["account_id"] != "only" || second["account_id"] != "only" {
		t.Fatalf("single-account lease was not exclusive: first=%v second=%v", first, second)
	}
}

func TestDisabledDoubaoAPIDoesNotShortenPoolWaitOrWebBudget(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "unfunded-fixture")
	t.Setenv("GEO_DOUBAO_API_FALLBACK", "0")
	t.Setenv("GEO_SINGLE_WEB_BUDGET_SEC", "120")
	accepted := time.Now()
	overall := accepted.Add(270 * time.Second)
	ctx, cancel := context.WithDeadline(context.Background(), overall)
	defer cancel()
	admission, stop := poolBrowserAdmissionContext(ctx, "doubao")
	defer stop()
	if deadline, ok := admission.Deadline(); !ok || !deadline.Equal(overall) {
		t.Fatal("web-only admission shortened the explicit caller deadline")
	}
	// The fourth question begins after 108 seconds of pool waiting.
	start := accepted.Add(108 * time.Second)
	deadline := requestWebDeadline(ctx, "doubao", overall, start, 1)
	if !deadline.Equal(start.Add(120*time.Second)) || officialAPIConfigured("doubao") {
		t.Fatalf("queued local flow was truncated for an unavailable API: %s", deadline.Sub(start))
	}
}
