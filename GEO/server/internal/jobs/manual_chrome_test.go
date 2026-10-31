package jobs

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"geo-server/internal/sessions"
	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/cdproto/target"
	"github.com/chromedp/chromedp"
)

// Local fixture only: no platform traffic, accounts or SMS. Exercises the real
// queue, independent Chrome, durable journal and Python manual-confirm gate.
func TestManualWorkflowChromeIntegration(t *testing.T) {
	if runtime.GOOS != "darwin" || os.Getenv("GEO_TEST_MAC_CDP") != "1" {
		t.Skip("opt-in Mac Chrome integration")
	}
	root := t.TempDir()
	geoRoot, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("GEO_MANUAL_WORKFLOW", "1")
	t.Setenv("GEO_CHROME_MODE", "mac-cdp")
	t.Setenv("GEO_DATA_DIR", root)
	t.Setenv("GEO_DIAGNOSTIC_DIR", filepath.Join(root, "diagnostics"))
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<header><div class="user-avatar">fixture account</div></header><div id="captcha">请完成安全验证</div><textarea></textarea>`)
	}))
	defer srv.Close()
	prior := append([]sessions.Site(nil), sessions.Sites...)
	defer func() { sessions.Sites = prior }()
	for i := range sessions.Sites {
		if sessions.Sites[i].ID == "kimi" {
			sessions.Sites[i].Home = srv.URL
			sessions.Sites[i].Login = srv.URL
		}
	}
	profile := filepath.Join(root, "profile")
	pool := sessions.NewPool("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", profile, "13800138000", filepath.Join(root, "accounts.json"))
	q := New(geoRoot, pool, nil, filepath.Join(root, "jobs"))
	q.Python = filepath.Join(geoRoot, "..", ".venv", "bin", "python")
	q.Script = filepath.Join(root, "fixture.py")
	script := fmt.Sprintf(`import json,os,sys
from unittest.mock import patch
sys.path.insert(0,%q)
from playwright.sync_api import sync_playwright
from cn_ai.manual_workflow import run
from cn_ai.web_recovery import HOSTS,RecoveryJournal
HOSTS['kimi']=('127.0.0.1',)
args=sys.argv
def arg(name):return args[args.index(name)+1]
action=arg('--recovery-action');site=arg('--site');prompt=arg('--prompt');question=arg('--question-id')
with sync_playwright() as pw:
 b=pw.chromium.connect_over_cdp(arg('--cdp-url'))
 if action=='manual-auto':
  run(b,site,prompt,question,'manual-pause')
  result={'ok':False,'error_code':'platform_risk_control','error':'fixture verification'}
 else:
  def collect(browser,site,prompt,**kw):
   assert kw['recovery_action']=='resume'
   j=RecoveryJournal.from_env(site,prompt,kw['question_id']);assert j.data['send_state']=='sent'
   page=j.find(browser);assert page.locator('[data-message-role=user]').count()==1
   return {'ok':True,'answer':page.locator('#answer').inner_text(),'citations':[]}
  with patch('cn_ai.scraper.scrape_one',side_effect=collect):result=run(b,site,prompt,question,action)
 print(json.dumps(result,ensure_ascii=False))
`, geoRoot)
	if err := os.WriteFile(q.Script, []byte(script), 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Stop()
		m := sessions.New("", profile, "")
		endpoint, err := m.CDPEndpoint()
		if err != nil {
			return
		}
		alloc, acancel := chromedp.NewRemoteAllocator(context.Background(), endpoint)
		defer acancel()
		ctx, cancel := chromedp.NewContext(alloc)
		defer cancel()
		_ = chromedp.Run(ctx, chromedp.ActionFunc(func(ctx context.Context) error { return browser.Close().Do(ctx) }))
		time.Sleep(400 * time.Millisecond)
	})
	job, _, err := q.Submit("fixture question", []string{"kimi"}, "", "", "fixture-request")
	if err != nil {
		t.Fatal(err)
	}
	wait := func(want string) *Job {
		t.Helper()
		deadline := time.Now().Add(30 * time.Second)
		for time.Now().Before(deadline) {
			j, _ := q.Get(job.ID)
			if j.Status == want {
				return j
			}
			time.Sleep(100 * time.Millisecond)
		}
		j, _ := q.Get(job.ID)
		t.Fatalf("wanted %s, got %+v", want, j)
		return nil
	}
	paused := wait("waiting_manual")
	state := paused.ManualStates["kimi"]
	if state.TargetID == "" || state.ErrorCode != "platform_risk_control" {
		t.Fatalf("pause did not retain browser: %+v", state)
	}
	q.mu.Lock()
	active := q.activeRuns[job.ID]
	q.mu.Unlock()
	if active {
		time.Sleep(200 * time.Millisecond)
	}
	if _, err := q.ManualAction(context.Background(), job.ID, "kimi", "claim", state.Revision); err != nil {
		t.Fatal(err)
	}
	paused, _ = q.Get(job.ID)
	state = paused.ManualStates["kimi"]
	m := sessions.New("", profile, "")
	endpoint, err := m.CDPEndpoint()
	if err != nil {
		t.Fatal(err)
	}
	alloc, acancel := chromedp.NewRemoteAllocator(context.Background(), endpoint)
	defer acancel()
	ctx, cancel := chromedp.NewContext(alloc, chromedp.WithTargetID(target.ID(state.TargetID)))
	defer cancel()
	if err := chromedp.Run(ctx, chromedp.Evaluate(`document.querySelector('#captcha').remove(); const u=document.createElement('div');u.dataset.messageRole='user';u.dataset.messageId='manual-turn';u.textContent='fixture question';document.body.append(u);const a=document.createElement('div');a.id='answer';a.textContent='answer collected after manual submission';document.body.append(a)`, nil)); err != nil {
		t.Fatal(err)
	}
	if _, err := q.ManualAction(context.Background(), job.ID, "kimi", "submitted", state.Revision); err != nil {
		t.Fatal(err)
	}
	done := wait("succeeded")
	if len(done.Results) != 1 || done.Results[0]["answer"] != "answer collected after manual submission" || done.Results[0]["submission_mode"] != "manual" {
		t.Fatalf("manual answer missing: %+v", done.Results)
	}
	account, ok := pool.Get("default")
	if !ok || account.Sites["kimi"].Status != "logged_in" {
		t.Fatal("confirmed webpage success did not update account availability")
	}
}
