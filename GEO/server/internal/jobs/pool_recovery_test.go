package jobs

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"geo-server/internal/sessions"
)

type recoveryPool struct {
	leases     []*sessions.Lease
	loginCalls int
}

func (p *recoveryPool) AcquireContext(ctx context.Context, site string) (*sessions.Lease, error) {
	return p.AcquireContextExcept(ctx, site, nil)
}
func (p *recoveryPool) AcquireForLoginContext(ctx context.Context, site string) (*sessions.Lease, error) {
	p.loginCalls++
	return p.AcquireContext(ctx, site)
}
func (p *recoveryPool) AcquireContextExcept(ctx context.Context, site string, skip map[string]bool) (*sessions.Lease, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	for _, lease := range p.leases {
		if !skip[lease.AccountID] {
			return lease, nil
		}
	}
	return nil, fmt.Errorf("pool exhausted")
}
func (p *recoveryPool) LoggedInAccountIDs(string) []string {
	var ids []string
	for _, lease := range p.leases {
		ids = append(ids, lease.AccountID)
	}
	return ids
}

func TestLoggedInPoolRecoversBeforeConfiguredAPI(t *testing.T) {
	for _, site := range []string{"doubao", "yuanbao", "kimi", "yiyan"} {
		t.Run(site, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_DOUBAO_BACKEND", "local-only")
			t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "0")
			t.Setenv("GEO_API_ENABLE_SEARCH", "0")
			var apiCalls atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				apiCalls.Add(1)
				w.WriteHeader(403)
				_, _ = w.Write([]byte(`{"error":{"message":"account has an overdue balance"}}`))
			}))
			defer api.Close()
			t.Setenv("GEO_"+strings.ToUpper(site)+"_API_KEY", "fixture")
			t.Setenv("GEO_"+strings.ToUpper(site)+"_API_BASE", api.URL)
			root := t.TempDir()
			pool := &recoveryPool{}
			for _, id := range []string{"first", "second"} {
				profile := filepath.Join(root, id)
				if err := os.Mkdir(profile, 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte("12345\n"), 0600); err != nil {
					t.Fatal(err)
				}
				manager := sessions.New("", profile, "")
				manager.AcquireSite("")
				pool.leases = append(pool.leases, &sessions.Lease{AccountID: id, Phone: id, Manager: manager})
			}
			script := filepath.Join(root, "scrape.py")
			body := `import json,sys
phone=sys.argv[sys.argv.index('--phone')+1]
with open('attempts','a',encoding='utf-8') as f:f.write(phone+'\n')
if phone=='first':
 print(json.dumps({'ok':False,'error_code':'platform_risk_control','error':'verification required','retryable':False}))
else:
 print(json.dumps({'ok':True,'answer':'answer from second account','source':'api:captured','citations':['https://example.org/real'],'browser_backend':'local'}))
`
			if err := os.WriteFile(script, []byte(body), 0600); err != nil {
				t.Fatal(err)
			}
			q := &Queue{GeoRoot: root, Python: "python3", Script: script, Sess: pool, siteSem: make(chan struct{}, 6)}
			result := q.runOneSite(site, "current question", "fixture")
			attempts, _ := os.ReadFile(filepath.Join(root, "attempts"))
			if result["ok"] != true || result["account_id"] != "second" || result["account_retries"] != 1 || result["source"] != "api:captured" || string(attempts) != "first\nsecond\n" || apiCalls.Load() != 0 || pool.loginCalls != 0 {
				t.Fatalf("result=%v attempts=%q api_calls=%d fresh_logins=%d", result, attempts, apiCalls.Load(), pool.loginCalls)
			}
		})
	}
}

func TestPoolAttemptDoesNotTruncateHealthyGeneration(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_SITE_TIMEOUT_SEC", "240")
	if got := poolAttemptTimeout("doubao", time.Now().Add(120*time.Second)); got < 119*time.Second || got > 120*time.Second {
		t.Fatalf("healthy generation lost its original window: %s", got)
	}
	if got := poolAttemptTimeout("doubao", time.Now().Add(20*time.Second)); got < 19*time.Second || got > 20*time.Second {
		t.Fatalf("short request budget changed: %s", got)
	}
}

func TestPoolAdmissionWaitDoesNotReserveAPIOrWebExecutionTime(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_DOUBAO_API_TIMEOUT_SEC", "120")
	deadline := time.Now().Add(270 * time.Second)
	parent, cancel := context.WithDeadline(context.Background(), deadline)
	defer cancel()
	wait, stop := poolBrowserAdmissionContext(parent, "doubao")
	defer stop()
	got, ok := wait.Deadline()
	if !ok || !got.Equal(deadline) {
		t.Fatalf("local pool wait shortened the caller's deadline: %s", time.Until(got))
	}
	short, cancelShort := context.WithDeadline(context.Background(), deadline.Add(-140*time.Second))
	defer cancelShort()
	shortWait, stopShort := poolBrowserAdmissionContext(short, "doubao")
	defer stopShort()
	shortDeadline, _ := short.Deadline()
	gotShort, _ := shortWait.Deadline()
	if !gotShort.Equal(shortDeadline) {
		t.Fatal("local pool wait still reserved API or minimum webpage execution time")
	}
	noDeadline, stopUnbounded := poolBrowserAdmissionContext(context.Background(), "doubao")
	defer stopUnbounded()
	if _, exists := noDeadline.Deadline(); exists {
		t.Fatal("standalone browser admission still has an artificial wait timeout")
	}
}
