package httpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"testing/fstest"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/jobs"
	"geo-server/internal/sessions"
	"golang.org/x/crypto/bcrypt"
)

type recoveryHTTPPool struct{ leases map[string]*sessions.Lease }

func (p *recoveryHTTPPool) AcquireContext(ctx context.Context, site string) (*sessions.Lease, error) {
	return p.AcquireContextExcept(ctx, site, nil)
}
func (p *recoveryHTTPPool) AcquireForLoginContext(ctx context.Context, site string) (*sessions.Lease, error) {
	return p.AcquireContextExcept(ctx, site, nil)
}
func (p *recoveryHTTPPool) AcquireContextExcept(ctx context.Context, site string, skip map[string]bool) (*sessions.Lease, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	lease := p.leases[site]
	if lease == nil || skip[lease.AccountID] {
		return nil, fmt.Errorf("fixture account unavailable")
	}
	return lease, nil
}
func (p *recoveryHTTPPool) LoggedInAccountIDs(site string) []string {
	if lease := p.leases[site]; lease != nil {
		return []string{lease.AccountID}
	}
	return nil
}

func TestHTTPSixPlatformWebRecoveryWaitsForCLIAndKeepsTenantDataPrivate(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "data")
	for key, value := range map[string]string{
		"GEO_DATA_DIR": data, "GEO_CURSOR_WORKSPACE": root, "GEO_DOUBAO_BACKEND": "local-only",
		"GEO_CURSOR_WEB_RECOVERY": "1", "GEO_CURSOR_CAPTCHA_AGENT": "1", "GEO_CURSOR_RECOVERY_TIMEOUT_SEC": "10",
		"GEO_QUEUE_WORKERS": "2", "GEO_SITE_CONCURRENCY": "2", "GEO_SITE_COOLDOWN_ENABLED": "0",
		"GEO_DIAGNOSTICS_ENABLED": "0", "GEO_API_ENABLE_SEARCH": "0", "GEO_API_FALLBACK": "1",
		"GEO_API_FALLBACK_WEB_RETRIES": "0", "GEO_SITE_FAILURE_RETRIES": "0",
		"GEO_SITE_TIMEOUT_SEC": "30", "GEO_MULTI_WEB_BUDGET_SEC": "10",
	} {
		t.Setenv(key, value)
	}
	write := func(name, body string, mode os.FileMode) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, []byte(body), mode); err != nil {
			t.Fatal(err)
		}
	}
	project, err := filepath.Abs("../../..")
	if configured := os.Getenv("GEO_TEST_RECOVERY_ROOT"); configured != "" {
		project = configured
	}
	if err != nil {
		t.Fatal(err)
	}
	internalSites := []string{"deepseek", "doubao", "kimi", "yuanbao", "qianwen", "yiyan"}
	pool := &recoveryHTTPPool{leases: map[string]*sessions.Lease{}}
	for _, site := range internalSites {
		for _, name := range []string{filepath.Join(".cursor", "skills", "geo-"+site+"-web-recovery", "SKILL.md"), filepath.Join("docs", strings.ToUpper(site)+"_WEB_RECOVERY_SOP.md")} {
			body, err := os.ReadFile(filepath.Join(project, name))
			if err != nil {
				t.Fatal(err)
			}
			write(filepath.Join(root, name), string(body), 0600)
		}
		profile := filepath.Join(root, "profiles", site)
		write(filepath.Join(profile, "DevToolsActivePort"), "12345\n", 0600)
		manager := sessions.New("", profile, "")
		manager.AcquireSite("")
		pool.leases[site] = &sessions.Lease{AccountID: "private-account-" + site, Phone: site, Manager: manager}
	}
	legacy, err := os.ReadFile(filepath.Join(project, "docs", "DOUBAO_CAPTCHA_SOP.md"))
	if err != nil {
		t.Fatal(err)
	}
	write(filepath.Join(root, "docs", "DOUBAO_CAPTCHA_SOP.md"), string(legacy), 0600)
	write(filepath.Join(root, "fixture-cursor"), `#!/usr/bin/python3
import pathlib,re,sys,time,json
print(json.dumps({'type':'tool_call','subtype':'started'}),flush=True)
site=re.search(r'geo-(deepseek|doubao|kimi|yuanbao|qianwen|yiyan)-web-recovery/SKILL',sys.argv[-1]).group(1)
pathlib.Path('cli-entered-'+site).touch()
end=time.monotonic()+8
while not pathlib.Path('release-cli').exists():
 if time.monotonic()>end:raise RuntimeError('fixture release was not received')
 time.sleep(.02)
pathlib.Path('repaired-'+site).touch()
print(json.dumps({'type':'tool_call','subtype':'completed'}),flush=True)
print('recovery-candidate',flush=True)
`, 0700)
	t.Setenv("GEO_CURSOR_AGENT", filepath.Join(root, "fixture-cursor"))
	write(filepath.Join(root, "fixture-scrape.py"), `import json,os,pathlib,sys
args=sys.argv
site=args[args.index('--site')+1]; prompt=args[args.index('--prompt')+1]; question=args[args.index('--question-id')+1]
account=os.environ['GEO_WEB_RECOVERY_ACCOUNT_ID']; state=pathlib.Path(os.environ['GEO_WEB_RECOVERY_FILE'])
action=args[args.index('--recovery-action')+1] if '--recovery-action' in args else 'normal'
assert account=='private-account-'+site
if action=='normal':
 with pathlib.Path('sent-'+site).open('x',encoding='utf-8') as stream:stream.write(question)
 state.parent.mkdir(parents=True,exist_ok=True)
 state.write_text(json.dumps({'site_id':site,'question_id':question,'prompt':prompt,'account_id':account,'send_state':'sent','target_id':'target-'+site}),encoding='utf-8')
 os.chmod(state,0o600)
 print(json.dumps({'ok':False,'error_code':'platform_risk_control','error':'current verification','retryable':False}))
elif action=='probe':
 saved=json.loads(state.read_text(encoding='utf-8'))
 assert saved['site_id']==site and saved['question_id']==question and saved['prompt']==prompt and saved['account_id']==account
 ready=pathlib.Path('repaired-'+site).exists()
 print(json.dumps({'ok':ready,'recovery_action':'probe','ready':ready,'context_valid':True,'target_id':'target-'+site,'send_state':'sent','phase':'sent','challenge_visible':not ready,'authenticated':ready}))
else:
 assert action=='resume' and pathlib.Path('repaired-'+site).exists()
 saved=json.loads(state.read_text(encoding='utf-8'))
 assert saved['site_id']==site and saved['question_id']==question and saved['prompt']==prompt and saved['account_id']==account
 with pathlib.Path('resumed-'+site).open('x',encoding='utf-8') as stream:stream.write(question)
 print(json.dumps({'ok':True,'site_id':site,'question_id':question,'prompt':prompt,'answer':'web answer '+site,'answer_origin':'web','source':'web','citations':['https://example.org/source/'+site],'web_search_status':'used','url':'https://example.org/share/'+site}))
`, 0600)
	var apiCalls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		apiCalls.Add(1)
		_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"unexpected API answer"}}]}`)
	}))
	defer api.Close()
	for _, site := range internalSites {
		prefix := "GEO_" + strings.ToUpper(site)
		t.Setenv(prefix+"_API_KEY", "integration-fixture")
		t.Setenv(prefix+"_API_BASE", api.URL)
		t.Setenv(prefix+"_API_FALLBACK", "1")
		t.Setenv(prefix+"_EXTERNAL_SEARCH", "0")
	}
	keys, tenant, err := auth.LoadKeys(filepath.Join(data, "keys.json"))
	if err != nil {
		t.Fatal(err)
	}
	cfg := config.Default(root)
	hash, _ := bcrypt.GenerateFromPassword([]byte("fixture-password"), bcrypt.MinCost)
	cfg.Admin.PasswordBcrypt = string(hash)
	a := auth.New(cfg)
	admin, err := a.Login(cfg.Admin.User, "fixture-password")
	if err != nil {
		t.Fatal(err)
	}
	q := jobs.New(root, nil, keys, filepath.Join(data, "jobs"))
	q.Sess, q.Python, q.Script = pool, "python3", filepath.Join(root, "fixture-scrape.py")
	server := httptest.NewServer(New(cfg, a, keys, nil, nil, q, nil, fstest.MapFS{}).Handler())
	defer server.Close()
	client := &http.Client{Timeout: 3 * time.Second}
	var nonce atomic.Int64
	request := func(method, path string, body []byte, administrator bool) (int, []byte) {
		t.Helper()
		r, err := http.NewRequest(method, server.URL+path, bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if administrator {
			r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: admin})
		} else {
			ts, n := strconv.FormatInt(time.Now().Unix(), 10), fmt.Sprintf("integration-%d", nonce.Add(1))
			r.Header.Set(auth.HeaderAK, tenant.AK)
			r.Header.Set(auth.HeaderTimestamp, ts)
			r.Header.Set(auth.HeaderNonce, n)
			r.Header.Set(auth.HeaderSign, auth.Sign(tenant.SK, auth.Canonical(method, strings.SplitN(path, "?", 2)[0], ts, n, auth.BodyHash(body))))
		}
		response, err := client.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		raw, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		return response.StatusCode, raw
	}
	payload := []byte(`{"sites":["deepseek","doubao","kimi","yuanbao","qianwen","wenxinyiyan"],"prompt":"original integration question","request_id":"recovery-http-fixture"}`)
	status, raw := request("POST", "/api/v1/ask", payload, false)
	var submitted jobs.Job
	if status != http.StatusAccepted || json.Unmarshal(raw, &submitted) != nil || submitted.ID == "" {
		t.Fatalf("submit: status=%d body=%s", status, raw)
	}
	defer func() {
		_ = os.WriteFile(filepath.Join(root, "release-cli"), nil, 0600)
		for until := time.Now().Add(5 * time.Second); time.Now().Before(until); time.Sleep(20 * time.Millisecond) {
			job, _ := q.Get(submitted.ID)
			if job != nil && job.Status != "queued" && job.Status != "running" {
				break
			}
		}
	}()
	path := "/api/v1/jobs/" + submitted.ID
	var during jobs.Job
	seenRecovery := false
	for until := time.Now().Add(5 * time.Second); time.Now().Before(until); time.Sleep(20 * time.Millisecond) {
		status, raw = request("GET", path, nil, true)
		if status != 200 || json.Unmarshal(raw, &during) != nil {
			t.Fatalf("administrator poll: status=%d body=%s", status, raw)
		}
		for _, recovery := range during.RecoveryStates {
			if recovery["phase"] == "waiting_cli" {
				seenRecovery = true
			}
		}
		entered, _ := filepath.Glob(filepath.Join(root, "cli-entered-*"))
		if seenRecovery && len(entered) > 0 {
			break
		}
	}
	if !seenRecovery || during.Status != "running" || apiCalls.Load() != 0 {
		t.Fatalf("recovery terminated or used API before CLI: job=%+v API_calls=%d", during, apiCalls.Load())
	}
	status, raw = request("GET", path, nil, false)
	if status != 200 || strings.Contains(string(raw), "account_id") || strings.Contains(string(raw), "private-account-") || strings.Contains(string(raw), "cli_call_id") || strings.Contains(string(raw), "recovery_states") {
		t.Fatalf("running tenant data leaked recovery internals: %d %s", status, raw)
	}
	write(filepath.Join(root, "release-cli"), "continue", 0600)
	var final jobs.Job
	for until := time.Now().Add(10 * time.Second); time.Now().Before(until); time.Sleep(20 * time.Millisecond) {
		status, raw = request("GET", path, nil, true)
		if status != 200 || json.Unmarshal(raw, &final) != nil {
			t.Fatalf("terminal poll: status=%d body=%s", status, raw)
		}
		if final.Status != "queued" && final.Status != "running" {
			break
		}
	}
	if final.Status != "succeeded" || len(final.Results) != 6 || apiCalls.Load() != 0 || len(final.RecoveryStates) != 6 {
		t.Fatalf("six-site HTTP recovery failed: %+v API_calls=%d", final, apiCalls.Load())
	}
	seen := map[string]bool{}
	for _, result := range final.Results {
		site, _ := result["site_id"].(string)
		internal := site
		if internal == "wenxinyiyan" {
			internal = "yiyan"
		}
		meta, _ := result["web_recovery"].(map[string]any)
		callID, _ := meta["cli_call_id"].(string)
		if seen[site] || result["answer"] != "web answer "+internal || result["answer_origin"] != "web" || meta["verified"] != true || meta["resumed"] != true || meta["cli_assisted"] != true || callID == "" {
			t.Fatalf("site/answer/recovery identity crossed: %#v", result)
		}
		seen[site] = true
		for _, name := range []string{"sent-" + internal, "resumed-" + internal} {
			body, err := os.ReadFile(filepath.Join(root, name))
			if err != nil || string(body) != "job_"+submitted.ID {
				t.Fatalf("missing or repeated original question: %s %q %v", name, body, err)
			}
		}
	}
	status, raw = request("GET", path, nil, false)
	if status != 200 || strings.Contains(string(raw), "account_id") || strings.Contains(string(raw), "private-account-") || strings.Contains(string(raw), "cli_call_id") || strings.Contains(string(raw), "recovery_states") || strings.Contains(string(raw), "target-") {
		t.Fatalf("final tenant data leaked recovery internals: %d %s", status, raw)
	}
	var public jobs.Job
	if err := json.Unmarshal(raw, &public); err != nil || public.ID != submitted.ID || public.Status != "succeeded" || len(public.Results) != 6 {
		t.Fatalf("tenant terminal polling lost completed results: %v %s", err, raw)
	}
}
