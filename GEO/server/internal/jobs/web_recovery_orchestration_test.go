package jobs

import (
	"context"
	"encoding/json"
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

func webRecoveryFixture(t *testing.T, mode string, accounts ...string) *Queue {
	t.Helper()
	q := attemptFixtureQueue(t, `import json,os,pathlib,sys
args=sys.argv
site=args[args.index('--site')+1]; prompt=args[args.index('--prompt')+1]
phone=args[args.index('--phone')+1]; question=args[args.index('--question-id')+1]
action=args[args.index('--recovery-action')+1] if '--recovery-action' in args else 'normal'
mode=os.environ['GEO_FIXTURE_RECOVERY_MODE']
with pathlib.Path('events').open('a',encoding='utf-8') as f:f.write(action+' '+phone+'\n')
repaired=pathlib.Path('repaired').exists()
state=pathlib.Path(os.environ['GEO_WEB_RECOVERY_FILE'])
if action=='probe':
 ready=repaired or mode=='generation'
 if mode=='unstable':
  counter=pathlib.Path('probe-count');n=int(counter.read_text())+1 if counter.exists() else 1;counter.write_text(str(n))
  if n>=3:ready=False
 target='other-target' if mode=='changed_target' and repaired else 'target-fixture'
 if mode=='acquisition' and not state.exists():
  state.parent.mkdir(parents=True,exist_ok=True)
  state.write_text(json.dumps({'send_state':'before_send'}),encoding='utf-8')
 waiting=mode=='sms' and not ready
 logged_out=mode=='logged_out' and not ready
 print(json.dumps({'ok':ready,'recovery_action':'probe','context_valid':True,'ready':ready,'target_id':target,'send_state':'sent','phase':'sent','challenge_visible':not ready and not waiting and not logged_out,'waiting_sms':waiting,'logged_out':logged_out}))
elif action=='resume':
 assert state.exists()
 if mode=='acquisition':
  with pathlib.Path('sent-once').open('a',encoding='utf-8') as f:f.write(phone+'\n')
 with pathlib.Path('resumed').open('a',encoding='utf-8') as f:f.write(phone+'\n')
 if mode=='resume_failed':
  print(json.dumps({'ok':False,'error_code':'generation_timeout','retryable':False}))
 else:
  print(json.dumps({'ok':True,'answer':'original webpage answer','answer_origin':'web','source':'web','citations':['https://example.org/source']}))
elif mode=='second_account' and phone=='second':
 print(json.dumps({'ok':True,'answer':'second webpage answer','answer_origin':'web','source':'web'}))
else:
 state.parent.mkdir(parents=True,exist_ok=True)
 state.write_text(json.dumps({'send_state':'sent','target_id':'target-fixture','site_id':site,'question_id':question,'prompt':prompt,'account_id':os.environ['GEO_WEB_RECOVERY_ACCOUNT_ID']}),encoding='utf-8')
 with pathlib.Path('sent-once').open('a',encoding='utf-8') as f:f.write(phone+'\n')
 code='generation_timeout' if mode=='generation' else 'platform_auth_unavailable' if mode in ('sms','logged_out') else 'platform_risk_control'
 print(json.dumps({'ok':False,'error_code':code,'error':'current verification','retryable':False}))
`, accounts...)
	t.Setenv("GEO_FIXTURE_RECOVERY_MODE", mode)
	t.Setenv("GEO_CURSOR_WEB_RECOVERY", "1")
	t.Setenv("GEO_CURSOR_CAPTCHA_AGENT", "1")
	t.Setenv("GEO_CURSOR_WORKSPACE", q.GeoRoot)
	t.Setenv("GEO_DATA_DIR", filepath.Join(q.GeoRoot, "data"))
	projectRoot, err := filepath.Abs("../../..")
	if configured := os.Getenv("GEO_TEST_RECOVERY_ROOT"); configured != "" {
		projectRoot = configured
	}
	if err != nil {
		t.Fatal(err)
	}
	legacy, err := os.ReadFile(filepath.Join(projectRoot, "docs", "DOUBAO_CAPTCHA_SOP.md"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(q.GeoRoot, "docs"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(q.GeoRoot, "docs", "DOUBAO_CAPTCHA_SOP.md"), legacy, 0600); err != nil {
		t.Fatal(err)
	}
	for _, site := range []string{"deepseek", "doubao", "yuanbao", "kimi", "qianwen", "yiyan"} {
		skill := filepath.Join(".cursor", "skills", "geo-"+site+"-web-recovery", "SKILL.md")
		sop := filepath.Join("docs", strings.ToUpper(site)+"_WEB_RECOVERY_SOP.md")
		for _, name := range []string{skill, sop} {
			body, err := os.ReadFile(filepath.Join(projectRoot, name))
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(q.GeoRoot, name)
			if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, body, 0600); err != nil {
				t.Fatal(err)
			}
		}
	}
	cli := filepath.Join(q.GeoRoot, "fixture-cursor")
	body := `#!/usr/bin/python3
import os,pathlib,time,json
print(json.dumps({'type':'tool_call','subtype':'started'}),flush=True)
with pathlib.Path('cli-calls').open('a',encoding='utf-8') as f:f.write('called\n')
mode=os.environ['GEO_FIXTURE_RECOVERY_MODE']
if mode=='cancel':time.sleep(30)
if mode=='logged_out':
 print('recovery-stopped: sms_required',flush=True)
 raise SystemExit
if mode not in ('unverified','second_account','api_last'):
 pathlib.Path('repaired').touch()
print(json.dumps({'type':'tool_call','subtype':'completed'}),flush=True)
print('recovery-candidate',flush=True)
`
	if err := os.WriteFile(cli, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GEO_CURSOR_AGENT", cli)
	q.DataDir = filepath.Join(q.GeoRoot, "data", "jobs")
	return q
}

func TestEveryPlatformWaitsForCLIAndResumesSameWebRequest(t *testing.T) {
	for _, site := range []string{"deepseek", "doubao", "yuanbao", "kimi", "qianwen", "wenxinyiyan"} {
		t.Run(site, func(t *testing.T) {
			q := webRecoveryFixture(t, "success", "first")
			result := q.runOneSiteContext(context.Background(), site, "original question", "recovery-fixture", 1)
			meta, _ := result["web_recovery"].(map[string]any)
			if result["ok"] != true || result["source"] != "web" || result["answer"] != "original webpage answer" || meta["verified"] != true || meta["resumed"] != true || meta["attempts"] != 1 {
				t.Fatalf("webpage was not resumed after verified CLI: %v", result)
			}
			sends, _ := os.ReadFile(filepath.Join(q.GeoRoot, "sent-once"))
			events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
			if string(sends) != "first\n" || string(events) != "normal first\nprobe first\nprobe first\nprobe first\nresume first\n" || len(q.siteSem) != 0 {
				t.Fatalf("duplicate send or resource leak: sends=%q events=%q", sends, events)
			}
			calls, _ := filepath.Glob(filepath.Join(q.GeoRoot, "data", "cursor-calls", "*.json"))
			if len(calls) != 1 {
				t.Fatalf("CLI calls=%d", len(calls))
			}
			var call map[string]any
			raw, _ := os.ReadFile(calls[0])
			_ = json.Unmarshal(raw, &call)
			if call["verified"] != true || call["resume_ok"] != true || call["ok"] != true {
				t.Fatalf("recovery evidence missing: %s", raw)
			}
		})
	}
}

func TestUnstableChallengeTeardownCannotResumeOrResend(t *testing.T) {
	q := webRecoveryFixture(t, "unstable", "first")
	lease := q.Sess.(*recoveryPool).leases[0]
	ctx := withWebRecovery(context.Background(), webRecoveryExecution{StatePath: q.webRecoveryPath("unstable-job", "doubao", lease.AccountID), AccountID: lease.AccountID})
	_, _ = q.scrapeForContext(ctx, "http://127.0.0.1:12345", "doubao", "question", "unstable-job", lease.Phone, 5*time.Second)
	_, err, meta := q.recoverWebAttempt(ctx, lease, "http://127.0.0.1:12345", "doubao", "question", "unstable-job", 1, false, recoveryFailure("platform_risk_control"))
	if err == nil || meta["error_code"] != "recovery_unstable" {
		t.Fatalf("unstable page accepted: %v %v", meta, err)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "resumed")); !os.IsNotExist(err) {
		t.Fatal("unstable page resumed")
	}
	sent, _ := os.ReadFile(filepath.Join(q.GeoRoot, "sent-once"))
	if string(sent) != "first\n" {
		t.Fatalf("question resent: %q", sent)
	}
}

func TestUnverifiedCLITriesAnotherWebAccountBeforeAPI(t *testing.T) {
	q := webRecoveryFixture(t, "second_account", "first", "second")
	var apiCalls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		apiCalls.Add(1)
		fmt.Fprint(w, `{"choices":[{"message":{"content":"API answer"}}]}`)
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	result := q.runOneSite("doubao", "original question", "other-account")
	if result["ok"] != true || result["account_id"] != "second" || apiCalls.Load() != 0 {
		t.Fatalf("API displaced another webpage: %v", result)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "resumed")); !os.IsNotExist(err) {
		t.Fatal("unverified CLI caused resume")
	}
}

func TestAPIStartsOnlyAfterCLIAndWebCandidatesAreExhausted(t *testing.T) {
	q := webRecoveryFixture(t, "api_last", "first")
	var apiCalls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		apiCalls.Add(1)
		calls, err := os.ReadFile(filepath.Join(q.GeoRoot, "cli-calls"))
		if err != nil || string(calls) != "called\n" {
			t.Error("API started before CLI finished")
		}
		events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
		if !strings.HasSuffix(string(events), "probe first\n") {
			t.Error("API started before independent verification")
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"fallback API answer"}}]}`)
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")
	result := q.runOneSite("doubao", "original question", "api-last")
	meta, _ := result["web_recovery"].(map[string]any)
	if result["source"] != "official_api" || apiCalls.Load() != 1 || meta["verified"] != false || meta["status"] != "failed" {
		t.Fatalf("fallback lost recovery evidence: %v", result)
	}
}

func TestCLIMarkerCannotAuthorizeChangedTarget(t *testing.T) {
	q := webRecoveryFixture(t, "changed_target", "first")
	result := q.runOneSite("doubao", "original question", "changed-target")
	meta, _ := result["web_recovery"].(map[string]any)
	if result["ok"] == true || meta["verified"] != false || meta["error_code"] != "cli_verification_failed" {
		t.Fatalf("CLI changed target without independent binding: %v", result)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "resumed")); !os.IsNotExist(err) {
		t.Fatal("resumed different target")
	}
}

func TestGenerationContinuationReadsOriginalPageWithoutCLIOrResend(t *testing.T) {
	q := webRecoveryFixture(t, "generation", "first")
	result := q.runOneSite("kimi", "original question", "generation")
	if result["ok"] != true || result["source"] != "web" {
		t.Fatalf("lost original generation: %v", result)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "cli-calls")); !os.IsNotExist(err) {
		t.Fatal("normal slow generation invoked CLI")
	}
	sends, _ := os.ReadFile(filepath.Join(q.GeoRoot, "sent-once"))
	if string(sends) != "first\n" {
		t.Fatal("generation continuation resent original question")
	}
}

func TestCancellationDuringCLINeverStartsResumeOrAPI(t *testing.T) {
	q := webRecoveryFixture(t, "cancel", "first")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 250*time.Millisecond)
	defer cancel()
	result := q.runOneSiteContext(ctx, "doubao", "original question", "cancel", 1)
	meta, _ := result["web_recovery"].(map[string]any)
	if meta["status"] != "cancelled" || calls.Load() != 0 || len(q.siteSem) != 0 {
		t.Fatalf("cancelled recovery continued or leaked: %v", result)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "resumed")); !os.IsNotExist(err) {
		t.Fatal("cancelled CLI caused resume")
	}
}

func TestRecoveryProgressAndCallIDsStayAdministratorOnly(t *testing.T) {
	job := &Job{RecoveryStates: map[string]map[string]any{"doubao": {"phase": "waiting_cli", "account_id": "private-account"}}, Results: []map[string]any{{"web_recovery": map[string]any{"status": "succeeded", "cli_call_id": "private-call", "verified": true}}}}
	copy := copyJob(job)
	stripInternalResults(copy)
	raw, _ := json.Marshal(copy)
	if strings.Contains(string(raw), "private-") || job.RecoveryStates == nil || job.Results[0]["web_recovery"].(map[string]any)["cli_call_id"] != "private-call" {
		t.Fatal("private recovery metadata leaked or stored evidence mutated")
	}
}

func TestExistingSMSCallbackWaitsWithoutAnotherCLIOrQuestion(t *testing.T) {
	q := webRecoveryFixture(t, "sms", "first")
	done := make(chan error, 1)
	go func() {
		time.Sleep(150 * time.Millisecond)
		done <- os.WriteFile(filepath.Join(q.GeoRoot, "repaired"), []byte("callback completed"), 0600)
	}()
	result := q.runOneSite("deepseek", "original question", "sms-callback")
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if result["ok"] != true || result["source"] != "web" {
		t.Fatalf("existing SMS did not resume original webpage: %v", result)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "cli-calls")); !os.IsNotExist(err) {
		t.Fatal("in-flight SMS needlessly launched CLI")
	}
	sends, _ := os.ReadFile(filepath.Join(q.GeoRoot, "sent-once"))
	if string(sends) != "first\n" {
		t.Fatal("SMS recovery resent question")
	}
}

func TestExistingSMSWaitIsBoundedAndPreservesSMSFailure(t *testing.T) {
	q := webRecoveryFixture(t, "sms", "first")
	t.Setenv("GEO_WEB_RECOVERY_SMS_WAIT_SEC", "1")
	start := time.Now()
	result := q.runOneSite("deepseek", "original question", "sms-timeout")
	if result["error_code"] != "sms_timeout" || time.Since(start) > 2*time.Second || len(q.siteSem) != 0 {
		t.Fatalf("SMS wait lost timeout or lease: %v", result)
	}
}

func TestPlainLoginPageUsesCLIThenServerSMS(t *testing.T) {
	q := webRecoveryFixture(t, "logged_out", "first")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	result := q.runOneSiteContext(ctx, "kimi", "original question", "logged-out", 1)
	meta, _ := result["web_recovery"].(map[string]any)
	if result["ok"] == true || meta["error_code"] != "sms_required" || meta["attempts"] != 1 || meta["cli_assisted"] == true {
		t.Fatalf("plain login page did not hand the CLI login to SMS: %v", result)
	}
	if _, err := os.Stat(filepath.Join(q.GeoRoot, "cli-calls")); err != nil {
		t.Fatal("observed login page did not launch CLI")
	}
	sends, _ := os.ReadFile(filepath.Join(q.GeoRoot, "sent-once"))
	if string(sends) != "first\n" {
		t.Fatal("login recovery resent question")
	}
}

type acquisitionRecoveryPool struct{ *recoveryPool }

func (p *acquisitionRecoveryPool) LoggedInAccountIDs(string) []string { return nil }
func (p *acquisitionRecoveryPool) RecoveryAccountIDs(site string) []string {
	return p.recoveryPool.LoggedInAccountIDs(site)
}
func (p *acquisitionRecoveryPool) AcquireForRecoveryContext(ctx context.Context, site string, skip map[string]bool) (*sessions.Lease, error) {
	return p.recoveryPool.AcquireContextExcept(ctx, site, skip)
}

func TestAccountWithoutLoginIsRecoveredBeforeFirstWebSubmission(t *testing.T) {
	q := webRecoveryFixture(t, "acquisition", "first")
	q.Sess = &acquisitionRecoveryPool{q.Sess.(*recoveryPool)}
	result := q.runOneSite("qianwen", "original question", "before-send")
	if result["ok"] != true || result["source"] != "web" {
		t.Fatalf("pending login was sent directly to API: %v", result)
	}
	events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
	sends, _ := os.ReadFile(filepath.Join(q.GeoRoot, "sent-once"))
	if strings.Contains(string(events), "normal ") || string(sends) != "first\n" {
		t.Fatalf("pre-send recovery sent extra question: %s / %s", events, sends)
	}
}

func TestActualRateLimitBackoffWaitsThenUsesWebpage(t *testing.T) {
	q := attemptFixtureQueue(t, "import json\nprint(json.dumps({'ok':True,'answer':'web answer','source':'web'}))\n", "first")
	q.DataDir = filepath.Join(q.GeoRoot, "data", "jobs")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "1")
	r := siteError("deepseek", "question", "platform_rate_limited", "rate limited", false, 1)
	r["retry_after_sec"] = int64(1)
	q.recordSiteCooldown("deepseek", r)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	start := time.Now()
	result := q.runOneSiteContext(ctx, "deepseek", "question", "rate-backoff", 1)
	if result["ok"] != true || result["source"] != "web" || time.Since(start) < 900*time.Millisecond {
		t.Fatalf("backoff skipped webpage or ignored platform limit: %v", result)
	}
}

func TestActualRateLimitCarriesOnlyBoundedRetryAfter(t *testing.T) {
	q := attemptFixtureQueue(t, "import json\nprint(json.dumps({'ok':False,'error_code':'platform_rate_limited','retryable':False,'retry_after_sec':125}))\n", "first")
	_, err := q.scrapeForContext(context.Background(), "http://127.0.0.1:9", "kimi", "question", "rate-metadata", "first", time.Second)
	failure, ok := err.(*scrapeFailure)
	if !ok || failure.code != "platform_rate_limited" || failure.retryAfter != 125 || failure.retryable {
		t.Fatalf("invalid rate metadata: %#v", err)
	}
}
