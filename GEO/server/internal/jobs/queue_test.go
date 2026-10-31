package jobs

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestStripInternalResultsDoesNotMutateStoredJobCopy(t *testing.T) {
	stored := &Job{
		ID: "job",
		Results: []map[string]any{{
			"site_id": "deepseek", "ok": true, "account_id": "acct_secret",
			"citations":        []any{"https://www.example.com/article"},
			"citation_domains": []any{"example.com"},
		}},
	}
	public := copyJob(stored)
	stripInternalResults(public)
	if _, ok := public.Results[0]["account_id"]; ok {
		t.Fatal("public result still exposes account_id")
	}
	details, ok := public.Results[0]["citation_details"].([]map[string]any)
	if !ok || len(details) != 1 || details[0]["site_name"] != "example.com" || details[0]["index"] != 1 {
		t.Fatalf("legacy citations were not converted: %#v", public.Results[0]["citation_details"])
	}
	citations, ok := public.Results[0]["citations"].([]map[string]any)
	if !ok || len(citations) != 1 || citations[0]["url"] != "https://www.example.com/article" {
		t.Fatalf("public citations were not enriched: %#v", public.Results[0]["citations"])
	}
	if got := stored.Results[0]["account_id"]; got != "acct_secret" {
		t.Fatalf("stored job was mutated: account_id=%v", got)
	}
}

func TestStripInternalResultsNormalizesExistingCitationDetails(t *testing.T) {
	originalDetail := map[string]any{
		"url": "https://example.com/a", "title": "示例", "site_name": "示例站", "icon": "",
	}
	stored := &Job{Results: []map[string]any{{
		"citation_details": []any{originalDetail},
	}}}
	public := copyJob(stored)
	stripInternalResults(public)
	details, ok := public.Results[0]["citation_details"].([]map[string]any)
	if !ok || len(details) != 1 || details[0]["index"] != 1 {
		t.Fatalf("normalized details = %#v", public.Results[0]["citation_details"])
	}
	citations, ok := public.Results[0]["citations"].([]map[string]any)
	if !ok || len(citations) != 1 || citations[0]["title"] != "示例" {
		t.Fatalf("public citations = %#v", public.Results[0]["citations"])
	}
	if _, ok := originalDetail["index"]; ok {
		t.Fatal("stored citation detail was mutated")
	}
}

func TestPublicReferencesKeepSourcesWhenDetailsAreEmptyOrPartial(t *testing.T) {
	for _, test := range []struct {
		name      string
		citations any
		details   any
	}{
		{"empty_details", []any{"https://example.org/a", "https://example.org/b"}, []any{}},
		{"null_details", []string{"https://example.org/a", "https://example.org/b"}, nil},
		{"objects_without_details", []any{map[string]any{"url": "https://example.org/a", "title": "来源甲"}, map[string]any{"url": "https://example.org/b"}}, nil},
		{"partial_details", []any{"https://example.org/a", "https://example.org/b"}, []any{map[string]any{"url": "https://example.org/a", "title": "来源甲"}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			stored := &Job{Results: []map[string]any{{"citations": test.citations, "citation_details": test.details, "citations_status": "missing"}}}
			before, _ := json.Marshal(stored)
			public := copyJob(stored)
			stripInternalResults(public)
			refs, ok := public.Results[0]["citations"].([]map[string]any)
			if !ok || len(refs) != 2 || refs[0]["url"] != "https://example.org/a" || refs[1]["url"] != "https://example.org/b" {
				t.Fatalf("source links disappeared in public conversion: %#v", public.Results[0])
			}
			if public.Results[0]["citations_status"] != "available" {
				t.Fatal("recovered references were still marked missing")
			}
			if test.name == "partial_details" || test.name == "objects_without_details" {
				if refs[0]["title"] != "来源甲" {
					t.Fatal("source title was lost")
				}
			}
			after, _ := json.Marshal(stored)
			if string(after) != string(before) {
				t.Fatal("stored history was modified")
			}
			first, _ := json.Marshal(public.Results[0]["citations"])
			stripInternalResults(public)
			second, _ := json.Marshal(public.Results[0]["citations"])
			if string(first) != string(second) {
				t.Fatal("repeated reads changed reference links")
			}
		})
	}
}

func TestStripInternalResultsCleansAnswersWithoutMutatingHistory(t *testing.T) {
	stored := &Job{Results: []map[string]any{{
		"answer":     "第一句[citation:8][reference:0]，第二句【citation:12】。",
		"answer_raw": "内部原文",
	}}}
	public := copyJob(stored)
	stripInternalResults(public)
	if got := public.Results[0]["answer"]; got != "第一句，第二句。" {
		t.Fatalf("public answer = %q", got)
	}
	if _, ok := public.Results[0]["answer_raw"]; ok {
		t.Fatal("public result exposed answer_raw")
	}
	if got := stored.Results[0]["answer"]; got != "第一句[citation:8][reference:0]，第二句【citation:12】。" {
		t.Fatalf("stored answer was mutated: %q", got)
	}
}

func TestStripInternalResultsOnlyExposesVerifiedShareURLs(t *testing.T) {
	stored := &Job{Results: []map[string]any{
		{"site_id": "deepseek", "url": "https://chat.deepseek.com/share/abc123_xyz"},
		{"site_id": "qianwen", "url": "https://www.qianwen.com/share?shareId=abc123xyz"},
		{"site_id": "yuanbao", "url": "https://yuanbao.tencent.com/bot/app/share/chat/abc123xyz"},
		{"site_id": "kimi", "url": "https://www.kimi.com/share/zh/1a0e81b1-b692-81cc-8000-095942a572b5"},
		{"site_id": "kimi", "url": "https://www.kimi.com/share/1a0edc54-80f2-8e5b-8000-00001566eab2"},
		{"site_id": "doubao", "url": "https://www.doubao.com/s/abc123xyz"},
		{"site_id": "doubao", "url": "https://www.doubao.com/thread/xshUuUfTeBHlyEMbv"},
		{"site_id": "weixinyiyan", "url": "https://chat.baidu.com/csaitab/history/share?share_id=abc123xyz&v=2"},
		{"site_id": "wenxinyiyan", "url": "https://chat.baidu.com/csaitab/history/share?share_id=wenxin12xyz&v=2"},
		{"site_id": "yiyan", "url": "https://wenxin.baidu.com/csaitab/history/share?share_id=internal12&v=2"},
		{"site_id": "deepseek", "url": "https://chat.deepseek.com/a/chat/s/private-id"},
		{"site_id": "yiyan", "url": "https://wenxin.baidu.com/search/123?enter_type=chat_url"},
		{"site_id": "qianwen", "url": "https://www.qianwen.com/chat/private-id"},
		{"site_id": "doubao", "url": "https://www.doubao.com/chat/private-id"},
		{"site_id": "yuanbao", "url": "https://yuanbao.tencent.com/chat/naQivTmsDa/private"},
		{"site_id": "kimi", "url": "https://www.kimi.com/chat/private-id"},
		{"site_id": "yiyan", "url": "https://yiyan.baidu.com/share?shareId=abc123xyz"},
		{"site_id": "deepseek", "url": "https://chat.deepseek.com/share/abc123/extra"},
	}}
	public := copyJob(stored)
	stripInternalResults(public)
	wantKeep := []string{
		"https://chat.deepseek.com/share/abc123_xyz",
		"https://www.qianwen.com/share?shareId=abc123xyz",
		"https://yuanbao.tencent.com/bot/app/share/chat/abc123xyz",
		"https://www.kimi.com/share/zh/1a0e81b1-b692-81cc-8000-095942a572b5",
		"https://www.kimi.com/share/1a0edc54-80f2-8e5b-8000-00001566eab2",
		"https://www.doubao.com/s/abc123xyz",
		"https://www.doubao.com/thread/xshUuUfTeBHlyEMbv",
		"https://chat.baidu.com/csaitab/history/share?share_id=abc123xyz&v=2",
		"https://chat.baidu.com/csaitab/history/share?share_id=wenxin12xyz&v=2",
		"https://wenxin.baidu.com/csaitab/history/share?share_id=internal12&v=2",
	}
	for i, want := range wantKeep {
		if got := public.Results[i]["url"]; got != want {
			t.Fatalf("result %d share URL = %v, want %v", i, got, want)
		}
	}
	for i := len(wantKeep); i < len(public.Results); i++ {
		if got := public.Results[i]["url"]; got != "" {
			t.Fatalf("result %d exposed unverified URL: %v", i, got)
		}
	}
	if got := stored.Results[len(wantKeep)]["url"]; got != "https://chat.deepseek.com/a/chat/s/private-id" {
		t.Fatalf("stored result mutated: %v", got)
	}
}

func TestDoubaoLocalSuccessMarksLoggedInButCloudCaptchaDoesNot(t *testing.T) {
	local := map[string]any{"ok": true, "browser_backend": "local"}
	if doubaoSessionEffect(local) != "logged_in" {
		t.Fatal("local doubao success should write logged_in")
	}
	cloud := map[string]any{
		"ok": true, "browser_backend": "agentbay",
		"local_error": "豆包触发语义安全验证，请人工完成验证后重试",
	}
	if doubaoSessionEffect(cloud) != "need_captcha" {
		t.Fatal("agentbay success after local captcha must not count as logged_in")
	}
	if doubaoSessionEffect(map[string]any{"ok": true, "browser_backend": "local-fallback"}) != "" {
		t.Fatal("unknown backend should not change the account")
	}
}

func TestVerifiedPublicShareURLAcceptsYuanbaoLetterOnlyShortID(t *testing.T) {
	share := "https://yb.tencent.com/s/YztGTQLWVktv"
	stored := &Job{Results: []map[string]any{{
		"site_id": "yuanbao", "url": share,
	}}}
	public := copyJob(stored)
	stripInternalResults(public)
	if got := public.Results[0]["url"]; got != share {
		t.Fatalf("public yuanbao URL = %v", got)
	}
	if verifiedPublicShareURL("yuanbao", "https://yb.tencent.com/s/YztGTQLWVktv?from=chat") {
		t.Fatal("yuanbao accepted a short share URL with a query")
	}
	if verifiedPublicShareURL("yuanbao", "https://yb.tencent.com/s/short") {
		t.Fatal("yuanbao accepted an id shorter than 8 characters")
	}
	if verifiedPublicShareURL("yuanbao", "https://yb.tencent.com/s/YztGTQLWVktv/extra") {
		t.Fatal("yuanbao accepted an extra path segment")
	}
}

func TestVerifiedPublicShareURLAcceptsObservedDoubaoLetterOnlyID(t *testing.T) {
	share := "https://www.doubao.com/thread/xshUuUfTeBHlyEMbv"
	if !verifiedPublicShareURL("doubao", share) {
		t.Fatal("doubao rejected observed public share URL with a letter-only ID")
	}
	if verifiedPublicShareURL("doubao", "https://www.doubao.com/thread/too/long") {
		t.Fatal("doubao accepted a share URL containing an extra path segment")
	}
}

func TestVerifiedPublicShareURLAcceptsWenxinAliases(t *testing.T) {
	share := "https://chat.baidu.com/csaitab/history/share?share_id=abc123xyz&v=2"
	for _, siteID := range []string{"weixinyiyan", "wenxinyiyan", "yiyan"} {
		if !verifiedPublicShareURL(siteID, share) {
			t.Fatalf("%s rejected verified 文心 share URL", siteID)
		}
	}
	short := "https://mr.baidu.com/r/23GC0PRap44?f=ot&u=5667a6dc1910455a"
	if !verifiedPublicShareURL("weixinyiyan", short) {
		t.Fatal("weixinyiyan rejected observed Baidu public share short URL")
	}
	if verifiedPublicShareURL("weixinyiyan", "https://mr.baidu.com/r/23GC0PRap44?f=other&u=5667a6dc1910455a") {
		t.Fatal("weixinyiyan accepted an unexpected Baidu short-link shape")
	}
	if verifiedPublicShareURL("weixinyiyan", "https://wenxin.baidu.com/search/123?enter_type=chat_url") {
		t.Fatal("weixinyiyan accepted a private search URL")
	}
}

func TestVerifiedPublicShareURLAcceptsObservedQianwenHost(t *testing.T) {
	share := "https://qianwen.my.cn/share/chat/a7b435e82031412ba034aa42b78680d6?lang=zh-CN"
	if !verifiedPublicShareURL("qianwen", share) {
		t.Fatal("qianwen rejected observed public share URL")
	}
	if verifiedPublicShareURL("qianwen", "https://qianwen.my.cn/share/chat/a7b435e82031412ba034aa42b78680d6?lang=bad") {
		t.Fatal("qianwen accepted an unexpected public share language")
	}
}

func TestSubmitIsIdempotentPerAKAndRequestID(t *testing.T) {
	q := &Queue{jobs: map[string]*Job{}, ch: make(chan string, 4)}
	first, created, err := q.Submit("问题", []string{"deepseek"}, "", "geo_a", "request-1")
	if err != nil || !created {
		t.Fatalf("first submit: created=%v err=%v", created, err)
	}
	second, created, err := q.Submit("问题", []string{"deepseek"}, "", "geo_a", "request-1")
	if err != nil || created {
		t.Fatalf("replay submit: created=%v err=%v", created, err)
	}
	if first.ID != second.ID || len(q.jobs) != 1 {
		t.Fatalf("idempotent replay created another job: first=%s second=%s jobs=%d", first.ID, second.ID, len(q.jobs))
	}
	if _, _, err := q.Submit("另一个问题", []string{"deepseek"}, "", "geo_a", "request-1"); err == nil {
		t.Fatal("same request id accepted a different payload")
	}
	third, created, err := q.Submit("问题", []string{"deepseek"}, "", "geo_b", "request-1")
	if err != nil || !created || third.ID == first.ID {
		t.Fatalf("request id was incorrectly shared across AKs: %#v created=%v err=%v", third, created, err)
	}
}

func TestSubmitAcceptsExternalWenxinSiteCode(t *testing.T) {
	q := &Queue{jobs: map[string]*Job{}, ch: make(chan string, 1)}
	job, created, err := q.Submit("问题", []string{"weixinyiyan"}, "", "geo_a", "wenxin-1")
	if err != nil || !created {
		t.Fatalf("submit weixinyiyan: created=%v err=%v", created, err)
	}
	if len(job.Sites) != 1 || job.Sites[0] != "weixinyiyan" {
		t.Fatalf("public sites = %v", job.Sites)
	}
	if got := internalSiteID(job.Sites[0]); got != "yiyan" {
		t.Fatalf("internal site = %q", got)
	}
	if got := internalSiteID("wenxinyiyan"); got != "yiyan" {
		t.Fatalf("compatible alias internal site = %q", got)
	}
}

func TestQueueMetricsAreLive(t *testing.T) {
	q := &Queue{jobs: map[string]*Job{}, ch: make(chan string, 4)}
	first, _, _ := q.Submit("一", []string{"deepseek"}, "", "geo_a", "one")
	second, _, _ := q.Submit("二", []string{"deepseek"}, "", "geo_a", "two")
	got, _ := q.Get(second.ID)
	if got.QueueLen != 2 || got.QueuePos != 1 {
		t.Fatalf("second metrics = len %d pos %d", got.QueueLen, got.QueuePos)
	}
	q.patch(first.ID, func(j *Job) { j.Status = "succeeded" })
	got, _ = q.Get(second.ID)
	if got.QueueLen != 1 || got.QueuePos != 0 {
		t.Fatalf("updated metrics = len %d pos %d", got.QueueLen, got.QueuePos)
	}
}

func TestFullQueueKeepsTheJobQueued(t *testing.T) {
	dir := t.TempDir()
	q := &Queue{DataDir: dir, jobs: map[string]*Job{}, ch: make(chan string, 1)}
	if _, _, err := q.Submit("一", []string{"deepseek"}, "", "geo_a", "one"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := q.Submit("二", []string{"deepseek"}, "", "geo_a", "two"); err != nil {
		t.Fatal(err)
	}
	files, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil || len(files) != 2 {
		t.Fatalf("persisted files = %v err=%v", files, err)
	}
	if len(q.jobs) != 2 {
		t.Fatalf("jobs = %d, want 2", len(q.jobs))
	}
	// This fixture has no deliveryScheduler goroutine. Drive its normal tick
	// after each buffer drain, instead of relying on Submit goroutine timing.
	received := map[string]bool{}
	for i := 0; i < 2; i++ {
		q.scheduleDeliveries(time.Now())
		select {
		case id := <-q.ch:
			if received[id] {
				t.Fatalf("duplicate queued job: %s", id)
			}
			received[id] = true
		case <-time.After(time.Second):
			t.Fatal("persisted queued job was not scheduled after capacity became available")
		}
	}
}

func TestLoadRestoresQueuedAndFailsRunning(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().Format(time.RFC3339)
	write := func(j Job) {
		raw, _ := json.Marshal(j)
		if err := os.WriteFile(filepath.Join(dir, j.ID+".json"), raw, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(Job{ID: "queued", Status: "queued", CreatedAt: now})
	write(Job{ID: "running", Status: "running", CreatedAt: now, CallbackURL: "https://example.com/callback"})
	q := &Queue{DataDir: dir, jobs: map[string]*Job{}}
	n, queued, interrupted := q.load()
	if n != 2 || len(queued) != 1 || queued[0] != "queued" || len(interrupted) != 1 || interrupted[0] != "running" {
		t.Fatalf("load result n=%d queued=%v interrupted=%v", n, queued, interrupted)
	}
	if got := q.jobs["running"]; got.Status != "failed" || got.ErrorCode != "service_interrupted" || !got.Retryable || got.CallbackStatus != "pending" {
		t.Fatalf("running job was not finalized: %#v", got)
	}
}

func TestCallbackPostsJobAndRecordsDelivery(t *testing.T) {
	received := make(chan Job, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Geo-Job-Id") != "job-1" {
			t.Errorf("missing job id header")
		}
		var body Job
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode callback: %v", err)
		}
		received <- body
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()
	q := &Queue{
		jobs: map[string]*Job{
			"job-1": {
				ID: "job-1", Status: "succeeded", CallbackURL: srv.URL, CallbackStatus: "pending",
				Results: []map[string]any{{
					"answer": "回调正文[citation:7]", "answer_raw": "内部原文",
					"citations":        []any{"https://example.org/source-a", "https://example.org/source-b"},
					"citation_details": []any{},
				}},
			},
		},
		CallbackClient:        srv.Client(),
		AllowPrivateCallbacks: true,
	}
	q.callback("job-1")
	select {
	case body := <-received:
		if body.ID != "job-1" || body.Status != "succeeded" {
			t.Fatalf("callback body = %#v", body)
		}
		if body.CallbackStatus != "" || body.CallbackAttempts != 0 {
			t.Fatalf("callback body exposed delivery state: %#v", body)
		}
		if len(body.Results) != 1 || body.Results[0]["answer"] != "回调正文" {
			t.Fatalf("callback answer was not cleaned: %#v", body.Results)
		}
		if _, ok := body.Results[0]["answer_raw"]; ok {
			t.Fatalf("callback exposed answer_raw: %#v", body.Results)
		}
		refs, ok := body.Results[0]["citations"].([]any)
		if !ok || len(refs) != 2 || refs[0].(map[string]any)["url"] != "https://example.org/source-a" || refs[1].(map[string]any)["url"] != "https://example.org/source-b" {
			t.Fatalf("callback lost available reference links: %#v", body.Results[0])
		}
	default:
		t.Fatal("callback was not delivered")
	}
	got := q.jobs["job-1"]
	if got.CallbackStatus != "ok" || got.CallbackAttempts != 1 {
		t.Fatalf("callback state = %#v", got)
	}
}

func TestValidateCallbackURLRejectsPrivateTargets(t *testing.T) {
	for _, raw := range []string{
		"http://127.0.0.1/callback",
		"http://localhost/callback",
		"http://user:pass@example.com/callback",
	} {
		if err := validateCallbackURL(raw); err == nil {
			t.Fatalf("accepted unsafe callback URL %q", raw)
		}
	}
}

func TestPublicScrapeErrorHidesLoginDetails(t *testing.T) {
	code, message, retryable := publicScrapeError(assertError("browser shows 未登录 for acct_secret"))
	if code != "platform_auth_unavailable" || message != "平台会话暂不可用，请稍后重试" || !retryable {
		t.Fatalf("normalized error = %q %q %v", code, message, retryable)
	}
	if code, _, retryable = publicScrapeError(assertError("context deadline exceeded")); code != "site_timeout" || !retryable {
		t.Fatalf("timeout normalization = %q %v", code, retryable)
	}
	code, message, retryable = publicScrapeError(&scrapeFailure{
		code: "platform_busy", raw: "upstream detail", retryable: true,
	})
	if code != "platform_busy" || message != "平台网页繁忙或当前回答尚未完成" || !retryable {
		t.Fatalf("busy normalization = %q %q %v", code, message, retryable)
	}
	code, message, retryable = publicScrapeError(&scrapeFailure{
		code: "platform_risk_control", raw: "captcha detail", retryable: false,
	})
	if code != "platform_risk_control" || message != "平台触发安全验证，请完成验证后重试" || retryable {
		t.Fatalf("risk normalization = %q %q %v", code, message, retryable)
	}
	code, message, retryable = publicScrapeError(&scrapeFailure{
		code: "platform_quota", raw: "Kimi 免费额度已用完，请等待额度重置后再试", retryable: false,
	})
	if code != "platform_quota" || message != "Kimi 免费额度已用完，请等待额度重置后再试" || retryable {
		t.Fatalf("quota normalization = %q %q %v", code, message, retryable)
	}
	code, message, retryable = publicScrapeError(&scrapeFailure{
		code: "sms_timeout", raw: "元宝验证码没有送达，请稍后重试", retryable: true,
	})
	if code != "sms_timeout" || message != "元宝验证码没有送达，请稍后重试" || !retryable {
		t.Fatalf("sms normalization = %q %q %v", code, message, retryable)
	}
}

func TestScrapePreservesStructuredPlatformFailure(t *testing.T) {
	dir := t.TempDir()
	script := filepath.Join(dir, "scrape.sh")
	body := "#!/bin/sh\nprintf '%s\\n' '{\"ok\":false,\"error_code\":\"platform_risk_control\",\"error\":\"captcha\",\"retryable\":false}'\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	q := &Queue{GeoRoot: dir, Python: "/bin/sh", Script: script}
	_, err := q.scrape("unused", "qianwen", "test", "job-test", "")
	failure, ok := err.(*scrapeFailure)
	if !ok {
		t.Fatalf("error = %T %v, want *scrapeFailure", err, err)
	}
	if failure.code != "platform_risk_control" || failure.retryable {
		t.Fatalf("failure = %#v", failure)
	}
}

func TestScrapeRetryLimitCoversLoginFlakesButNotCaptchaOrQuota(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_SITE_FAILURE_RETRIES", "")
	t.Setenv("GEO_PLATFORM_BUSY_RETRIES", "")
	t.Setenv("GEO_KIMI_TIMEOUT_RETRIES", "")
	auth := &scrapeFailure{code: "platform_auth_unavailable", raw: "平台登录已失效，请重新登录", retryable: true}
	if got := scrapeRetryLimit("doubao", auth); got != 0 {
		t.Fatalf("doubao login retries = %d, want 0", got)
	}
	if got := scrapeRetryLimit("deepseek", auth); got != 0 {
		t.Fatalf("deepseek login retries = %d, want 0", got)
	}
	if got := scrapeRetryLimit("yuanbao", &scrapeFailure{code: "sms_timeout", raw: "验证码没有送达", retryable: true}); got != 0 {
		t.Fatalf("sms retries = %d, want 0", got)
	}
	if got := scrapeRetryLimit("qianwen", &scrapeFailure{code: "platform_busy", raw: "busy", retryable: true}); got != 5 {
		t.Fatalf("qianwen busy retries = %d, want 5", got)
	}
	if got := scrapeRetryLimit("doubao", &scrapeFailure{code: "platform_risk_control", raw: "captcha", retryable: true}); got != 0 {
		t.Fatalf("captcha retries = %d, want 0", got)
	}
	if got := scrapeRetryLimit("kimi", &scrapeFailure{code: "platform_quota", raw: "额度已用完", retryable: false}); got != 0 {
		t.Fatalf("quota retries = %d, want 0", got)
	}
	if got := scrapeRetryLimit("yiyan", &scrapeFailure{code: "site_timeout", raw: "超时", retryable: true}); got != 1 {
		t.Fatalf("timeout retries = %d, want 1", got)
	}
	if got := scrapeRetryLimit("kimi", &scrapeFailure{code: "platform_busy", raw: "busy", retryable: true}); got != 1 {
		t.Fatalf("kimi busy retries = %d, want 1", got)
	}
	if got := scrapeRetryLimit("doubao", nil); got != 0 {
		t.Fatalf("success retries = %d, want 0", got)
	}
}

func TestIsKimiTransientRequiresRetryableStructuredFailure(t *testing.T) {
	for _, code := range []string{"platform_busy", "site_timeout", "empty_answer", "invalid_answer"} {
		if !isKimiTransient(&scrapeFailure{
			code: code, raw: "transient", retryable: true,
		}) {
			t.Errorf("retryable %s should be retried", code)
		}
	}
	if isKimiTransient(&scrapeFailure{
		code: "platform_busy", raw: "capacity", retryable: false,
	}) {
		t.Fatal("non-retryable platform_busy should not be retried")
	}
	if isKimiTransient(&scrapeFailure{
		code: "platform_risk_control", raw: "captcha", retryable: true,
	}) {
		t.Fatal("risk control should not be retried as a Kimi transient failure")
	}
	if isKimiTransient(assertError("platform_busy")) {
		t.Fatal("unstructured text error should not be retried")
	}
}

func TestYuanbaoStaysOnLocalChrome(t *testing.T) {
	for _, backend := range []string{"", "local", "agentbay", "local-only", "off", "volcengine", "huoshan"} {
		t.Setenv("GEO_DOUBAO_BACKEND", backend)
		if yuanbaoUsesAgentBay() {
			t.Fatalf("%s should keep yuanbao on the local browser", backend)
		}
	}
}

func TestIsYuanbaoTransientRetriesOnlyEmptyOrInvalidAnswer(t *testing.T) {
	for _, code := range []string{"empty_answer", "invalid_answer"} {
		if !isYuanbaoTransient(&scrapeFailure{
			code: code, raw: "transient", retryable: true,
		}) {
			t.Errorf("retryable %s should be retried", code)
		}
	}
	if isYuanbaoTransient(&scrapeFailure{
		code: "platform_auth_unavailable", raw: "logged out", retryable: true,
	}) {
		t.Fatal("authentication failure should use account failover, not Yuanbao content retry")
	}
	if isYuanbaoTransient(&scrapeFailure{
		code: "invalid_answer", raw: "invalid", retryable: false,
	}) {
		t.Fatal("non-retryable invalid answer should not be retried")
	}
}

func TestIsPlatformRiskRequiresStructuredRiskFailure(t *testing.T) {
	if !isPlatformRisk(&scrapeFailure{
		code: "platform_risk_control", raw: "captcha", retryable: false,
	}) {
		t.Fatal("structured platform risk should trigger the single Doubao retry")
	}
	if isPlatformRisk(&scrapeFailure{
		code: "platform_busy", raw: "busy", retryable: true,
	}) {
		t.Fatal("platform busy should not trigger the Doubao risk retry")
	}
	if isPlatformRisk(assertError("platform_risk_control")) {
		t.Fatal("unstructured text should not trigger the Doubao risk retry")
	}
}

func TestLoginAndCaptchaDoNotStackInsideTheSiteBudget(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_SITE_BUDGET_SEC", "")
	t.Setenv("GEO_SITE_TIMEOUT_SEC", "300")
	t.Setenv("GEO_SITE_FAILURE_RETRY_SEC", "3")
	if siteBudget() != 420*time.Second {
		t.Fatalf("budget = %s, want 7m", siteBudget())
	}
	if siteBudget() <= 300*time.Second {
		t.Fatal("budget must leave room for one full slow attempt")
	}
	auth := &scrapeFailure{code: "platform_auth_unavailable", raw: "平台登录已失效，请重新登录", retryable: true}
	captcha := &scrapeFailure{code: "platform_risk_control", raw: "豆包触发语义安全验证", retryable: true}
	timeout := &scrapeFailure{code: "site_timeout", raw: "平台响应超时（超过 5m0s）", retryable: true}
	far := time.Now().Add(10 * time.Minute)
	if shouldRetryScrape("doubao", auth, 0, far) {
		t.Fatal("login failure must not be retried on the same session")
	}
	if shouldRetryScrape("doubao", captcha, 0, far) {
		t.Fatal("captcha must not be retried on the same session")
	}
	if shouldRetryScrape("doubao", timeout, 0, time.Now().Add(20*time.Second)) {
		t.Fatal("a second full timeout must not start when the budget cannot hold it")
	}
	if !shouldRetryScrape("yiyan", timeout, 0, far) {
		t.Fatal("one timeout retry still fits when the budget has several minutes left")
	}
	if shouldRetryScrape("yiyan", timeout, 1, far) {
		t.Fatal("timeout retry limit is one")
	}
	if anotherAccountReady([]string{"acct_a"}, nil, "acct_a") {
		t.Fatal("the same logged-in account is not a failover target")
	}
	if anotherAccountReady(nil, nil, "acct_a") {
		t.Fatal("captcha-only pool must not failover")
	}
	if !anotherAccountReady([]string{"acct_a", "acct_b"}, nil, "acct_a") {
		t.Fatal("a different logged-in account should take the next attempt")
	}
	tried := map[string]bool{"acct_b": true}
	if !anotherAccountReady([]string{"acct_a", "acct_b", "acct_c"}, tried, "acct_a") {
		t.Fatal("a third logged-in account should still be tried")
	}
	if anotherAccountReady([]string{"acct_a", "acct_b"}, tried, "acct_a") {
		t.Fatal("an already failed account must not be selected again")
	}
}

func TestSiteBudgetCoversEachLoggedInAccount(t *testing.T) {
	t.Setenv("GEO_SITE_BUDGET_SEC", "")
	t.Setenv("GEO_SITE_TIMEOUT_SEC", "300")
	if siteBudgetForAccounts(1) != 420*time.Second {
		t.Fatalf("single account budget = %s", siteBudgetForAccounts(1))
	}
	if siteBudgetForAccounts(2) != 10*time.Minute {
		t.Fatalf("two account budget = %s", siteBudgetForAccounts(2))
	}
	if siteBudgetForAccounts(4) != 15*time.Minute {
		t.Fatalf("capped budget = %s", siteBudgetForAccounts(4))
	}
}

func TestAuthFailureIsNotRepeatedBeforeTheBudget(t *testing.T) {
	dir := t.TempDir()
	count := filepath.Join(dir, "count")
	script := filepath.Join(dir, "scrape.sh")
	body := "#!/bin/sh\nprintf x >> '" + count + "'\nprintf '%s\\n' '{\"ok\":false,\"error_code\":\"platform_auth_unavailable\",\"error\":\"平台登录已失效，请重新登录\",\"retryable\":true}'\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	q := &Queue{GeoRoot: dir, Python: "/bin/sh", Script: script}
	_, err, retries := q.scrapeWithRetries("unused", "doubao", "test", "job-budget", "", nil, time.Now().Add(5*time.Minute))
	failure, ok := err.(*scrapeFailure)
	if !ok || failure.code != "platform_auth_unavailable" || retries != 0 {
		t.Fatalf("result = %#v retries=%d", err, retries)
	}
	got, readErr := os.ReadFile(count)
	if readErr != nil || string(got) != "x" {
		t.Fatalf("attempts = %q err=%v, want one", got, readErr)
	}
	code, message, retryable := publicScrapeError(err)
	if code != "platform_auth_unavailable" || message != "平台会话暂不可用，请重新登录" || !retryable {
		t.Fatalf("public error = %q %q %v", code, message, retryable)
	}
}

func TestTimeoutRetryRequiresAFullAttemptWindow(t *testing.T) {
	t.Setenv("GEO_SITE_TIMEOUT_SEC", "300")
	err := &scrapeFailure{code: "site_timeout", raw: "timeout", retryable: true}
	if shouldRetryScrape("doubao", err, 0, time.Now().Add(2*time.Minute)) {
		t.Fatal("cannot start another 300s timeout with only 120s left")
	}
}

func TestExpiredBudgetNeverLaunchesScraper(t *testing.T) {
	q := &Queue{Python: "/does-not-exist"}
	_, err, retries := q.scrapeWithRetries("", "doubao", "test", "expired", "", nil, time.Now().Add(-time.Second))
	failure, ok := err.(*scrapeFailure)
	if !ok || failure.code != "site_timeout" || retries != 0 {
		t.Fatalf("expired budget should return before exec: %v", err)
	}
}

func TestAdmissionWaitCancelsAndUnrelatedSiteCanProceed(t *testing.T) {
	q := &Queue{}
	blocked := q.siteGate("doubao")
	blocked <- struct{}{}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if acquireSlot(ctx, blocked) {
		t.Fatal("occupied site admitted another request")
	}
	other := q.siteGate("deepseek")
	if !acquireSlot(context.Background(), other) {
		t.Fatal("unrelated site was blocked")
	}
	<-other
	<-blocked
}

func TestProgressSnapshotCannotBeOverwrittenByStaleSubmit(t *testing.T) {
	j := &Job{ID: "progress", Status: "running", CompletedSites: 1, Results: []map[string]any{{"site_id": "doubao", "ok": true}}}
	q := &Queue{DataDir: t.TempDir(), jobs: map[string]*Job{j.ID: j}}
	q.save(&Job{ID: j.ID, Status: "queued"})
	raw, err := os.ReadFile(q.jobPath(j.ID))
	if err != nil {
		t.Fatal(err)
	}
	var saved Job
	if err := json.Unmarshal(raw, &saved); err != nil || saved.Status != "running" || saved.CompletedSites != 1 {
		t.Fatalf("stale persistence lost progress: %s, %v", raw, err)
	}
}

func TestRunningJobPublishesCompletedSitesBeforeLastSiteFinishes(t *testing.T) {
	t.Setenv("GEO_DOUBAO_BACKEND", "volcengine")
	dir := t.TempDir()
	script := filepath.Join(dir, "scrape.sh")
	release := filepath.Join(dir, "release")
	body := "#!/bin/sh\nif [ -f first ]; then while [ ! -f release ]; do sleep 0.02; done; else touch first; fi\nprintf '%s\\n' '{\"ok\":true,\"answer\":\"test\"}'\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	j := &Job{ID: "progress", Status: "queued", Prompt: "test", Sites: []string{"doubao", "doubao"}, createdUnix: time.Now().UnixMilli()}
	q := &Queue{GeoRoot: dir, Python: "/bin/sh", Script: script, jobs: map[string]*Job{j.ID: j}, siteSem: make(chan struct{}, 1)}
	done := make(chan struct{})
	go func() { q.run(j.ID); close(done) }()
	defer os.WriteFile(release, nil, 0o600)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		current, _ := q.Get(j.ID)
		if current.CompletedSites == 1 {
			if current.Status != "running" || len(current.Results) != 1 {
				t.Fatalf("invalid incremental progress: %#v", current)
			}
			if err := os.WriteFile(release, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("last site did not complete")
			}
			final, _ := q.Get(j.ID)
			if final.Status != "succeeded" || final.CompletedSites != 2 || len(final.Results) != 2 {
				t.Fatalf("invalid final progress: %#v", final)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("first completed site was hidden until the entire job finished")
}

func TestSummarizeLoginFailures(t *testing.T) {
	results := []map[string]any{
		{"ok": false, "error_code": "platform_auth_unavailable"},
		{"ok": false, "error_code": "platform_auth_unavailable"},
	}
	status, code, message, retryable := summarizeResults(results)
	if status != "failed" || code != "platform_auth_unavailable" || message == "" || !retryable {
		t.Fatalf("summary = %q %q %q %v", status, code, message, retryable)
	}
}

func TestVisibleBodyDoesNotWaitOnJobLock(t *testing.T) {
	q := &Queue{
		jobs:         map[string]*Job{},
		requestIndex: map[string]string{},
		ch:           make(chan string, 1),
	}
	job, created, err := q.Submit("问题", []string{"deepseek"}, "", "geo_a", "poll-1")
	if err != nil || !created || job.ID == "" {
		t.Fatalf("submit: created=%v err=%v", created, err)
	}
	q.mu.Lock()
	done := make(chan struct{})
	go func() {
		defer close(done)
		body, ok := q.VisibleBody(job.ID, "geo_a", false)
		if !ok || !json.Valid(body) {
			t.Errorf("visible body ok=%v valid=%v", ok, json.Valid(body))
		}
		var got Job
		if err := json.Unmarshal(body, &got); err != nil {
			t.Errorf("unmarshal: %v", err)
			return
		}
		if got.Status != "queued" || got.AK != "" {
			t.Errorf("status=%s ak=%q", got.Status, got.AK)
		}
		if _, ok := q.VisibleBody(job.ID, "geo_b", false); ok {
			t.Error("other tenant can read the job")
		}
		admin, ok := q.VisibleBody(job.ID, "", true)
		if !ok {
			t.Error("admin body missing")
			return
		}
		var adminJob Job
		if err := json.Unmarshal(admin, &adminJob); err != nil {
			t.Errorf("admin unmarshal: %v", err)
			return
		}
		if adminJob.AK != "geo_a" {
			t.Errorf("admin ak=%q", adminJob.AK)
		}
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("poll waited on the job lock")
	}
	q.mu.Unlock()
}

type assertError string

func (e assertError) Error() string { return string(e) }
