package jobs

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"geo-server/internal/sessions"
)

// Official chat APIs used after the webpage has already failed. Each provider
// speaks the OpenAI chat-completions shape. Keys stay in the process environment
// (GEO/server/.env via systemd EnvironmentFile) and are never written into job results.
type apiProvider struct {
	id           string
	name         string
	keyEnv       string
	baseEnv      string
	modelEnv     string
	defaultBase  string
	defaultModel string
	// Native search parameters are separate from the unchanged user question.
	// Retrieval failures remain explicit if a plain answer is used afterward.
	search map[string]any
}

var apiProviders = []apiProvider{
	{
		id: "doubao", name: "豆包",
		keyEnv: "GEO_DOUBAO_API_KEY", baseEnv: "GEO_DOUBAO_API_BASE", modelEnv: "GEO_DOUBAO_API_MODEL",
		defaultBase: "https://ark.cn-beijing.volces.com/api/v3", defaultModel: "doubao-seed-2-1-lite-260915",
	},
	{
		id: "yuanbao", name: "腾讯元宝",
		keyEnv: "GEO_YUANBAO_API_KEY", baseEnv: "GEO_YUANBAO_API_BASE", modelEnv: "GEO_YUANBAO_API_MODEL",
		defaultBase: "https://tokenhub.tencentmaas.com/v1", defaultModel: "hy3",
		search: map[string]any{"web_search_options": map[string]any{"enable": true, "search_source": "lite"}},
	},
	{
		id: "kimi", name: "Kimi",
		keyEnv: "GEO_KIMI_API_KEY", baseEnv: "GEO_KIMI_API_BASE", modelEnv: "GEO_KIMI_API_MODEL",
		defaultBase: "https://api.moonshot.cn/v1", defaultModel: "kimi-k3",
	},
	{
		id: "yiyan", name: "文心一言",
		keyEnv: "GEO_YIYAN_API_KEY", baseEnv: "GEO_YIYAN_API_BASE", modelEnv: "GEO_YIYAN_API_MODEL",
		defaultBase: "https://qianfan.baidubce.com/v2", defaultModel: "ernie-4.5-turbo-128k",
		search: map[string]any{"web_search": map[string]any{"enable": true, "enable_trace": true, "enable_citation": true, "enable_status": true}},
	},
	{
		id: "qianwen", name: "通义千问",
		keyEnv: "GEO_QIANWEN_API_KEY", baseEnv: "GEO_QIANWEN_API_BASE", modelEnv: "GEO_QIANWEN_API_MODEL",
		defaultBase: "https://dashscope.aliyuncs.com/compatible-mode/v1", defaultModel: "qwen-plus",
		search: map[string]any{"enable_search": true, "search_options": map[string]any{"forced_search": true}},
	},
	{
		id: "deepseek", name: "DeepSeek",
		keyEnv: "GEO_DEEPSEEK_API_KEY", baseEnv: "GEO_DEEPSEEK_API_BASE", modelEnv: "GEO_DEEPSEEK_API_MODEL",
		defaultBase: "https://api.deepseek.com", defaultModel: "deepseek-v4-pro",
	},
}

// The call context enforces both the provider budget and the request deadline.
// A fixed client timeout would silently truncate longer provider budgets.
var officialAPIHTTP = &http.Client{}

func apiFallbackEnabled() bool {
	switch strings.ToLower(geoEnv("GEO_API_FALLBACK")) {
	case "0", "false", "off", "no":
		return false
	default:
		return true
	}
}

func providerFor(site string) (apiProvider, bool) {
	id := internalSiteID(site)
	for _, provider := range apiProviders {
		if provider.id == id {
			return provider, true
		}
	}
	return apiProvider{}, false
}

func officialAPIConfigured(site string) bool {
	if !siteAPIFallbackEnabled(site) {
		return false
	}
	provider, ok := providerFor(site)
	if !ok {
		return false
	}
	return provider.apiKey() != ""
}

// Per-site overrides keep unrelated platforms web-only.
func siteAPIFallbackEnabled(site string) bool {
	if provider, ok := providerFor(site); ok {
		switch strings.ToLower(geoEnv("GEO_" + strings.ToUpper(provider.id) + "_API_FALLBACK")) {
		case "1", "true", "on", "yes":
			return true
		case "0", "false", "off", "no":
			return false
		}
	}
	return apiFallbackEnabled()
}

func apiTimeout(site string) time.Duration {
	seconds := apiEnvInt("GEO_API_TOTAL_TIMEOUT_SEC", 90, 5, 300)
	switch internalSiteID(site) {
	case "doubao":
		seconds = apiEnvInt("GEO_DOUBAO_API_TIMEOUT_SEC", 60, 5, 300)
	case "deepseek":
		seconds = apiEnvInt("GEO_DEEPSEEK_API_TIMEOUT_SEC", 60, 5, 300)
	case "kimi":
		seconds = apiEnvInt("GEO_KIMI_API_TIMEOUT_SEC", seconds, 5, 300)
	case "qianwen":
		seconds = apiEnvInt("GEO_QIANWEN_API_TIMEOUT_SEC", seconds, 5, 300)
	case "yiyan":
		seconds = apiEnvInt("GEO_YIYAN_API_TIMEOUT_SEC", seconds, 5, 300)
	case "yuanbao":
		seconds = apiEnvInt("GEO_YUANBAO_API_TIMEOUT_SEC", seconds, 5, 300)
	}
	return time.Duration(seconds) * time.Second
}

func apiSearchEnabled() bool {
	switch strings.ToLower(geoEnv("GEO_API_ENABLE_SEARCH")) {
	case "0", "false", "off", "no":
		return false
	default:
		return true
	}
}

func (p apiProvider) baseURL() string {
	if value := geoEnv(p.baseEnv); value != "" {
		return strings.TrimRight(value, "/")
	}
	return p.defaultBase
}

func (p apiProvider) model() string {
	if value := geoEnv(p.modelEnv); value != "" {
		return value
	}
	return p.defaultModel
}

func (p apiProvider) apiKey() string {
	return geoEnv(p.keyEnv)
}

// geoEnv prefers the process environment, then GEO/server/.env. systemd only
// reads that file at process start; reloading it here lets a newly saved API
// key take effect on the next question without another restart.
func geoEnv(name string) string {
	if value, ok := os.LookupEnv(name); ok {
		return strings.TrimSpace(value)
	}
	return loadDotEnv()[name]
}

func apiEnvInt(name string, def, min, max int) int {
	raw := geoEnv(name)
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < min || n > max {
		return def
	}
	return n
}

var (
	dotEnvMu   sync.Mutex
	dotEnvMod  time.Time
	dotEnvVals map[string]string
)

func loadDotEnv() map[string]string {
	path := dotEnvPath()
	st, err := os.Stat(path)
	dotEnvMu.Lock()
	defer dotEnvMu.Unlock()
	if err != nil {
		dotEnvVals = map[string]string{}
		dotEnvMod = time.Time{}
		return dotEnvVals
	}
	if dotEnvVals != nil && st.ModTime().Equal(dotEnvMod) {
		return dotEnvVals
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		dotEnvVals = map[string]string{}
		dotEnvMod = time.Time{}
		return dotEnvVals
	}
	dotEnvVals = parseDotEnv(string(raw))
	dotEnvMod = st.ModTime()
	return dotEnvVals
}

func dotEnvPath() string {
	root := strings.TrimSpace(os.Getenv("GEO_SERVER_ROOT"))
	if root == "" {
		root = "."
	}
	return filepath.Join(root, ".env")
}

func parseDotEnv(raw string) map[string]string {
	vals := map[string]string{}
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if len(value) >= 2 {
			if (value[0] == '"' && value[len(value)-1] == '"') || (value[0] == '\'' && value[len(value)-1] == '\'') {
				value = value[1 : len(value)-1]
			}
		}
		if key != "" {
			vals[key] = value
		}
	}
	return vals
}

// applyAPIFallback replaces a failed webpage result with one official API answer.
// Login, captcha, quota, and exhausted retries all land here so the caller still
// receives text. A missing key or an API error keeps the original webpage failure.
func (q *Queue) applyAPIFallback(site, prompt string, failure map[string]any) map[string]any {
	return q.applyAPIFallbackContext(context.Background(), site, prompt, failure)
}

// Search/share quality does not turn a completed webpage answer into an API
// fallback opportunity. Keep its answer, source, share and real citations bound
// to the original conversation; the monitor reports any missing evidence.
func (q *Queue) recoverWebSearchContext(parent context.Context, site, prompt string, web map[string]any) map[string]any {
	if web == nil || web["ok"] != true || strings.TrimSpace(stringField(web, "answer")) == "" || stringField(web, "source") == "official_api" || stringField(web, "answer_origin") == "official_api" {
		return web
	}
	kept := make(map[string]any, len(web)+2)
	for key, value := range web {
		kept[key] = value
	}
	issues := []string{}
	switch existing := web["quality_issues"].(type) {
	case []string:
		issues = append(issues, existing...)
	case []any:
		for _, value := range existing {
			if code, ok := value.(string); ok {
				issues = append(issues, code)
			}
		}
	}
	addIssue := func(code string) {
		for _, existing := range issues {
			if existing == code {
				return
			}
		}
		issues = append(issues, code)
	}
	if status := stringField(web, "web_search_status"); status == "not_confirmed" || status == "failed" {
		code := stringField(web, "web_search_error_code")
		if code == "" {
			code = "search_not_confirmed"
			if status == "failed" {
				code = "search_request_failed"
			}
			kept["web_search_error_code"] = code
		}
		addIssue(code)
	}
	if status := stringField(web, "share_status"); status == "generation_failed" || status == "verification_failed" || status == "failed" {
		code := stringField(web, "share_error_code")
		if code == "" {
			code = "share_generation_failed"
			kept["share_error_code"] = code
		}
		addIssue(code)
	}
	if len(issues) > 0 {
		kept["quality_issues"] = issues
	}
	return kept
}

func (q *Queue) applyAPIFallbackContext(parent context.Context, site, prompt string, failure map[string]any) (out map[string]any) {
	return q.applyAPIContext(parent, site, prompt, failure, false)
}

func (q *Queue) applyAPIContext(parent context.Context, site, prompt string, failure map[string]any, direct bool) (out map[string]any) {
	if failure == nil {
		return nil
	}
	// Local waiting/setup failures cannot substitute API for a webpage that
	// did not get an execution opportunity.
	if !direct && !webFailureAllowsAPI(failure) {
		return failure
	}
	if ok, _ := failure["ok"].(bool); ok {
		return failure
	}
	if !direct && stringField(failure, "source") == "official_api" {
		return failure
	}
	if !direct && !officialAPIConfigured(site) {
		return failure
	}
	parent, attempts := ensureAttemptContext(parent, failure)
	defer func() { attempts.attach(out) }()
	apiAccountID := "api:" + internalSiteID(site)
	apiWaitStart := time.Now()
	if direct {
		provider, known := providerFor(site)
		if !known || provider.apiKey() == "" {
			failure["api_error_code"], failure["api_error"] = "api_not_configured", "此平台尚未配置 API 凭据"
			failure["api_skipped"], failure["api_skip_reason"] = true, "api_not_configured"
			attempts.skip("api", apiAccountID, "api_not_configured", "api_not_configured", apiWaitStart)
			return failure
		}
	}
	if parent.Err() != nil {
		failure["request_deadline_exceeded"] = true
		return apiSkipped(failure, "request_deadline_exceeded", "request_deadline_exceeded", apiWaitStart, attempts, apiAccountID)
	}
	if !hasUsefulAPIWindow(parent, site, apiWaitStart) {
		return apiSkipped(failure, "insufficient_api_budget", "insufficient_api_budget", apiWaitStart, attempts, apiAccountID)
	}
	if q.apiSem != nil {
		admission, cancelAdmission := apiAdmissionContext(parent, site)
		admitted := acquireSlot(admission, q.apiSem)
		cancelAdmission()
		if !admitted {
			if parent.Err() != nil {
				failure["request_deadline_exceeded"] = true
			}
			return apiSkipped(failure, "api_admission_timeout", "api_admission", apiWaitStart, attempts, apiAccountID)
		}
		defer func() { <-q.apiSem }()
	}
	started := time.Now()
	if !hasUsefulAPIWindow(parent, site, started) {
		return apiSkipped(failure, "insufficient_api_budget", "insufficient_api_budget", apiWaitStart, attempts, apiAccountID)
	}
	ctx, cancel := context.WithTimeout(parent, apiTimeout(site))
	defer cancel()
	jobID, _ := parent.Value(jobExecutionKey{}).(string)
	finishActivity := q.beginPlatformActivity(jobID)
	defer finishActivity()
	q.extendJobDeadline(jobID, started.Add(apiTimeout(site)))
	answer, model, citations, search, err := askOfficialAPIWithSearch(ctx, site, prompt)
	executionMS := time.Since(started).Milliseconds()
	q.recordAPIDiagnostic(failure, started, err)
	if err != nil {
		if !direct {
			failure["api_fallback_failed"] = true
		}
		failure["api_elapsed_ms"] = executionMS
		recordAPITimings(failure, nil, started.Sub(apiWaitStart).Milliseconds(), executionMS)
		failure["api_error_code"] = apiFailureCode(err)
		failure["web_search_status"] = search.Status
		failure["web_search_policy"] = search.Policy
		if search.ErrorCode != "" {
			failure["web_search_error_code"] = search.ErrorCode
		}
		if ctx.Err() != nil {
			failure["api_error_code"] = "api_timeout"
		}
		if direct {
			log.Printf("direct api failed site=%s api_error_code=%s", site, stringField(failure, "api_error_code"))
		} else {
			logAPIFallback(site, stringField(failure, "error_code"), stringField(failure, "api_error_code"))
		}
		attempts.record("api", apiAccountID, false, stringField(failure, "api_error_code"), started)
		failure["api_error"] = apiFailureMessage(stringField(failure, "api_error_code"))
		return failure
	}
	attempts.record("api", apiAccountID, true, "", started)
	one := officialAPIResult(site, prompt, answer, model, citations, executionMS)
	one["web_search_status"] = search.Status
	one["web_search_provider"] = search.Provider
	one["web_search_mode"] = search.Mode
	one["web_search_policy"] = search.Policy
	one["search_result_count"] = len(citations)
	options := providerReasoningOptions(internalSiteID(site))
	if thinking, ok := options["thinking"].(map[string]any); ok {
		one["api_thinking_enabled"] = thinking["type"] == "enabled"
	}
	if enabled, ok := options["enable_thinking"].(bool); ok {
		one["api_thinking_enabled"] = enabled
	}
	if internalSiteID(site) == "yiyan" && model == "ernie-5.0-thinking-preview" {
		one["api_thinking_enabled"] = true
	}
	if effort, ok := options["reasoning_effort"].(string); ok {
		one["api_reasoning_effort"] = effort
	}
	if search.Mode == "external" || search.Mode == "native_search_api" {
		one["search_requested_limit"] = apiEnvInt("GEO_STRUCTURED_SEARCH_LIMIT", 20, 1, 20)
	}
	one["citations_origin"] = "search_results"
	if len(citations) == 0 {
		one["citations_origin"] = "none"
		if search.Status == "not_confirmed" {
			one["web_search_error_code"] = "search_not_confirmed"
			one["quality_issues"] = append(one["quality_issues"].([]string), "api_search_not_confirmed")
		}
	}
	if search.ErrorCode != "" {
		one["web_search_error_code"] = search.ErrorCode
		one["quality_issues"] = append(one["quality_issues"].([]string), "api_search_failed")
	}
	if internalSiteID(site) == "deepseek" {
		one["native_web_search_supported"] = search.Mode == "native_search_api"
	}
	one["api_elapsed_ms"] = one["elapsed_ms"]
	recordAPITimings(one, failure, started.Sub(apiWaitStart).Milliseconds(), executionMS)
	for _, field := range []string{"platform_retries", "account_retries"} {
		if retries, exists := failure[field]; exists {
			one[field] = retries
		}
	}
	if account := stringField(failure, "account_id"); account != "" {
		one["account_id"] = account
	}
	if !direct {
		one["web_error_code"] = stringField(failure, "error_code")
		one["web_elapsed_ms"] = failure["elapsed_ms"]
	} else {
		one["degraded"] = false
		issues, _ := one["quality_issues"].([]string)
		for i, issue := range issues {
			if issue == "api_fallback_no_web_share" {
				issues[i] = "api_no_web_share"
			}
		}
		one["quality_issues"] = issues
	}
	if direct {
		log.Printf("direct api succeeded site=%s model=%s api_elapsed_ms=%d", site, model, executionMS)
	} else {
		apiFallbackSuccessLog(site, stringField(failure, "error_code"), model, executionMS)
	}
	for _, key := range []string{"cooldown_until", "retry_after_sec", "web_skipped", "web_skip_reason", "web_prepare_exhausted", "web_recovery_exhausted", "web_setup_wait_exhausted", "web_setup_wait_count", "web_setup_wait_started_at", "web_setup_wait_ms", "fallback_reason", "diagnostic_id", "web_recovery"} {
		if value, exists := failure[key]; exists {
			one[key] = value
		}
	}
	return one
}

func logAPIFallback(site, webCode, apiCode string) {
	// Imported log is used by queue.go in this package. Keep the call there
	// so this file does not need its own logger in tests that replace nothing.
	apiFallbackLog(site, webCode, apiCode)
}

func askOfficialAPI(ctx context.Context, site, prompt string) (string, string, []map[string]any, error) {
	answer, model, citations, _, err := askOfficialAPIWithSearch(ctx, site, prompt)
	return answer, model, citations, err
}

// Thinking depth and retrieval coverage are independent. Preserve the explicit
// DeepSeek switch, and avoid silently forcing Kimi's lowest reasoning effort.
func providerReasoningOptions(site string) map[string]any {
	extra := map[string]any{}
	switch site {
	case "deepseek":
		thinking := "disabled"
		switch strings.ToLower(geoEnv("GEO_DEEPSEEK_API_THINKING")) {
		case "1", "true", "on", "yes":
			thinking = "enabled"
		}
		extra["thinking"] = map[string]any{"type": thinking}
		extra["max_tokens"] = apiEnvInt("GEO_DEEPSEEK_API_MAX_TOKENS", 4096, 256, 32768)
		if thinking == "enabled" {
			extra["reasoning_effort"] = apiReasoningEffort("GEO_DEEPSEEK_API_REASONING_EFFORT", "high")
		}
	case "kimi":
		extra["reasoning_effort"] = apiReasoningEffort("GEO_KIMI_API_REASONING_EFFORT", "high")
	case "qianwen":
		extra["enable_thinking"] = apiEnvInt("GEO_QIANWEN_API_THINKING", 0, 0, 1) == 1
	}
	return extra
}

func apiReasoningEffort(name, fallback string) string {
	switch value := strings.ToLower(geoEnv(name)); value {
	case "low", "high", "max":
		return value
	default:
		return fallback
	}
}

func askOfficialAPIWithSearch(ctx context.Context, site, prompt string) (string, string, []map[string]any, searchEvidence, error) {
	evidence := searchEvidence{Status: "disabled", Mode: "native", Provider: internalSiteID(site), Policy: "disabled"}
	provider, ok := providerFor(site)
	if !ok {
		return "", "", nil, evidence, fmt.Errorf("站点 %s 没有官方 API 兜底", site)
	}
	key := provider.apiKey()
	if key == "" {
		return "", "", nil, evidence, fmt.Errorf("未配置 %s", provider.keyEnv)
	}
	model := provider.model()
	if provider.id == "deepseek" {
		extra := providerReasoningOptions(provider.id)
		if apiSearchEnabled() {
			answer, citations, search, err := postRetrievedAnswer(ctx, provider, prompt, extra)
			return answer, model, citations, search, err
		}
		evidence.Status = "not_supported"
		answer, citations, err := postChatCompletion(ctx, provider.baseURL(), key, model, prompt, extra)
		return answer, model, citations, evidence, err
	}
	if provider.id == "kimi" && apiSearchEnabled() {
		answer, citations, search, err := postRetrievedAnswer(ctx, provider, prompt, providerReasoningOptions(provider.id))
		return answer, model, citations, search, err
	}
	if apiSearchEnabled() {
		evidence.Status = "requested"
		evidence.Policy = "automatic"
		if provider.id == "doubao" || provider.id == "qianwen" {
			evidence.Policy = "forced"
		}
		answer, citations, err := searchOfficialAPI(ctx, provider, key, model, prompt)
		if err == nil && strings.TrimSpace(answer) != "" {
			evidence.Status = "not_confirmed"
			if len(citations) > 0 {
				evidence.Status = "used"
			} else if nativeSearchRecoveryEnabled(provider.id) {
				// A native answer without structured sources must not acquire
				// unrelated links after the fact. Retrieve evidence, then have
				// the same provider's model answer again from that evidence.
				recovered, refs, search, recoveryErr := postRetrievedAnswerRequired(ctx, provider, prompt, providerReasoningOptions(provider.id))
				if recoveryErr == nil && len(refs) > 0 && strings.TrimSpace(recovered) != "" {
					return recovered, model, refs, search, nil
				}
				// Keep the already completed native answer on retrieval failure.
				// Empty references remain explicit rather than fabricated.
				evidence.ErrorCode = search.ErrorCode
				if recoveryErr != nil && evidence.ErrorCode == "" {
					evidence.ErrorCode = "search_regeneration_failed"
				}
			}
			return answer, model, citations, evidence, nil
		}
		evidence.Status, evidence.ErrorCode = "failed", "search_"+strings.TrimPrefix(apiFailureCode(err), "api_")
		// Authentication, billing and risk rejections do not recover by
		// submitting the same prompt again without search tools.
		if permanentAPIRejection(err) {
			return "", model, nil, evidence, err
		}
		// Search and plain fallback share the same budget. A timed-out
		// search must not start another full-length request.
		if ctx.Err() != nil {
			return "", model, nil, evidence, ctx.Err()
		}
	}
	answer, citations, err := postChatCompletion(ctx, provider.baseURL(), key, model, prompt, providerReasoningOptions(provider.id))
	if err != nil {
		return "", "", nil, evidence, err
	}
	return answer, model, citations, evidence, nil
}

func nativeSearchRecoveryEnabled(site string) bool {
	search, _ := providerFor("kimi")
	if search.apiKey() == "" {
		return false
	}
	switch site {
	case "yiyan":
		return apiEnvInt("GEO_YIYAN_API_REQUIRE_SEARCH", 1, 0, 1) == 1 && apiEnvInt("GEO_YIYAN_EXTERNAL_SEARCH", 0, 0, 1) == 1
	case "yuanbao":
		return apiEnvInt("GEO_YUANBAO_API_REQUIRE_SEARCH", 1, 0, 1) == 1 && apiEnvInt("GEO_YUANBAO_EXTERNAL_SEARCH", 1, 0, 1) == 1
	case "doubao", "qianwen":
		return true
	}
	return false
}

func searchOfficialAPI(ctx context.Context, provider apiProvider, key, model, prompt string) (string, []map[string]any, error) {
	switch provider.id {
	case "doubao":
		return postResponses(ctx, provider.baseURL(), key, model, prompt, []any{
			map[string]any{"type": "web_search", "max_keyword": 2, "limit": 10},
		}, "required")
	case "deepseek":
		answer, citations, _, err := postRetrievedAnswer(ctx, provider, prompt, providerReasoningOptions(provider.id))
		return answer, citations, err
	case "kimi":
		answer, citations, _, err := postRetrievedAnswer(ctx, provider, prompt, providerReasoningOptions(provider.id))
		return answer, citations, err
	case "qianwen":
		if endpoint := dashscopeEndpoint(provider.baseURL()); endpoint != "" {
			return postDashScopeSearch(ctx, endpoint, key, model, prompt)
		}
	case "yiyan":
		return postYiyanSearch(ctx, provider.baseURL(), key, model, prompt)
	}
	if len(provider.search) == 0 {
		return "", nil, fmt.Errorf("站点 %s 没有联网搜索参数", provider.id)
	}
	extra := providerReasoningOptions(provider.id)
	for key, value := range provider.search {
		extra[key] = value
	}
	return postChatCompletion(ctx, provider.baseURL(), key, model, prompt, extra)
}

func postResponses(ctx context.Context, base, apiKey, model, prompt string, tools []any, toolChoice any) (string, []map[string]any, error) {
	body := map[string]any{"model": model, "input": prompt, "tools": tools}
	if toolChoice != nil {
		body["tool_choice"] = toolChoice
	}
	payload, err := doAPI(ctx, http.MethodPost, strings.TrimRight(base, "/")+"/responses", apiKey, body)
	if err != nil {
		return "", nil, err
	}
	return parseResponses(payload)
}

func parseResponses(payload []byte) (string, []map[string]any, error) {
	var body map[string]any
	if err := json.Unmarshal(payload, &body); err != nil {
		return "", nil, fmt.Errorf("解析 API 响应失败: %w", err)
	}
	if message := nestedString(body, "error", "message"); message != "" {
		return "", nil, fmt.Errorf("%s", message)
	}
	if status, _ := body["status"].(string); status == "incomplete" || status == "failed" {
		return "", nil, fmt.Errorf("API 回答未完整生成: %s", status)
	}
	var b strings.Builder
	output, _ := body["output"].([]any)
	for _, raw := range output {
		item, _ := raw.(map[string]any)
		if item == nil {
			continue
		}
		if kind, _ := item["type"].(string); kind == "reasoning" {
			continue
		}
		appendText(&b, item["text"])
		appendText(&b, item["content"])
	}
	answer := strings.TrimSpace(b.String())
	if answer == "" {
		return "", nil, fmt.Errorf("API 返回了空回答")
	}
	return answer, collectAPICitations(body), nil
}

func appendText(b *strings.Builder, value any) {
	switch typed := value.(type) {
	case string:
		if text := strings.TrimSpace(typed); text != "" {
			if b.Len() > 0 {
				b.WriteByte('\n')
			}
			b.WriteString(text)
		}
	case []any:
		for _, part := range typed {
			item, _ := part.(map[string]any)
			if item == nil {
				continue
			}
			if kind, _ := item["type"].(string); kind == "reasoning" {
				continue
			}
			appendText(b, item["text"])
		}
	}
}

func dashscopeEndpoint(base string) string {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	if base == "" || strings.Contains(base, "127.0.0.1") || strings.Contains(base, "localhost") {
		return ""
	}
	if strings.Contains(base, "/api/v1/services/") {
		return base
	}
	host := base
	if i := strings.Index(base, "/compatible-mode/"); i >= 0 {
		host = base[:i]
	} else if i := strings.Index(base, "/v1"); i > len("https://") {
		host = base[:i]
	}
	if !strings.Contains(host, "aliyuncs.com") && !strings.Contains(host, "maas.") {
		return ""
	}
	return strings.TrimRight(host, "/") + "/api/v1/services/aigc/text-generation/generation"
}

func postDashScopeSearch(ctx context.Context, endpoint, apiKey, model, prompt string) (string, []map[string]any, error) {
	body := map[string]any{
		"model": model,
		"input": map[string]any{"messages": []map[string]string{{"role": "user", "content": prompt}}},
		"parameters": map[string]any{
			"result_format":  "message",
			"enable_search":  true,
			"search_options": map[string]any{"enable_source": true, "forced_search": true},
		},
	}
	for key, value := range providerReasoningOptions("qianwen") {
		body["parameters"].(map[string]any)[key] = value
	}
	payload, err := doAPI(ctx, http.MethodPost, endpoint, apiKey, body)
	if err != nil {
		return "", nil, err
	}
	var parsed map[string]any
	if err := json.Unmarshal(payload, &parsed); err != nil {
		return "", nil, fmt.Errorf("解析 API 响应失败: %w", err)
	}
	if message := nestedString(parsed, "error", "message"); message != "" {
		return "", nil, fmt.Errorf("%s", message)
	}
	output, _ := parsed["output"].(map[string]any)
	if output == nil {
		return "", nil, fmt.Errorf("API 未返回回答")
	}
	answer := ""
	if choices, _ := output["choices"].([]any); len(choices) > 0 {
		choice, _ := choices[0].(map[string]any)
		if reason, _ := choice["finish_reason"].(string); reason == "length" || reason == "content_filter" {
			return "", nil, fmt.Errorf("API 回答未完整生成: %s", reason)
		}
		message, _ := choice["message"].(map[string]any)
		answer = strings.TrimSpace(contentText(message["content"]))
	}
	if answer == "" {
		return "", nil, fmt.Errorf("API 返回了空回答")
	}
	return answer, collectAPICitations(output), nil
}

func postYiyanSearch(ctx context.Context, base, apiKey, model, prompt string) (string, []map[string]any, error) {
	body := map[string]any{
		"model":  model,
		"stream": true,
		"messages": []map[string]string{
			{"role": "user", "content": prompt},
		},
		"web_search": map[string]any{
			"enable": true, "enable_trace": true, "enable_citation": true, "enable_status": true,
		},
	}
	payload, err := doAPI(ctx, http.MethodPost, strings.TrimRight(base, "/")+"/chat/completions", apiKey, body)
	if err != nil {
		return "", nil, err
	}
	var b strings.Builder
	merged := map[string]any{}
	for _, line := range strings.Split(string(payload), "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "" || data == "[DONE]" {
			continue
		}
		var event map[string]any
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			continue
		}
		for _, key := range []string{"search_results", "search_info", "references"} {
			if event[key] != nil {
				batches, _ := merged[key].([]any)
				merged[key] = append(batches, event[key])
			}
		}
		choices, _ := event["choices"].([]any)
		if len(choices) == 0 {
			continue
		}
		choice, _ := choices[0].(map[string]any)
		if reason, _ := choice["finish_reason"].(string); reason == "length" || reason == "content_filter" {
			return "", nil, fmt.Errorf("API 回答未完整生成: %s", reason)
		}
		if tag, _ := choice["delta_tag"].(string); tag == "search_status" {
			continue
		}
		delta, _ := choice["delta"].(map[string]any)
		if delta == nil {
			continue
		}
		b.WriteString(contentText(delta["content"]))
	}
	answer := strings.TrimSpace(b.String())
	if answer == "" {
		return "", nil, fmt.Errorf("API 返回了空回答")
	}
	return answer, collectAPICitations(merged), nil
}

func doAPI(ctx context.Context, method, endpoint, apiKey string, body any) ([]byte, error) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := officialAPIHTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, compactAPIError(payload))
	}
	return payload, nil
}

func postChatCompletion(ctx context.Context, base, apiKey, model, prompt string, extra map[string]any) (string, []map[string]any, error) {
	body := map[string]any{
		"model": model,
		"messages": []map[string]string{
			{"role": "user", "content": prompt},
		},
	}
	for key, value := range extra {
		body[key] = value
	}
	rawBody, err := json.Marshal(body)
	if err != nil {
		return "", nil, err
	}
	endpoint := strings.TrimRight(base, "/") + "/chat/completions"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(rawBody))
	if err != nil {
		return "", nil, err
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := officialAPIHTTP.Do(req)
	if err != nil {
		return "", nil, err
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return "", nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, compactAPIError(payload))
	}
	answer, citations, err := parseChatCompletion(payload)
	if err != nil {
		return "", nil, err
	}
	return answer, citations, nil
}

func compactAPIError(payload []byte) string {
	var body map[string]any
	if err := json.Unmarshal(payload, &body); err != nil {
		text := strings.TrimSpace(string(payload))
		if len(text) > 300 {
			text = text[:300]
		}
		if text == "" {
			return "空响应"
		}
		return text
	}
	if message := nestedString(body, "error", "message"); message != "" {
		return message
	}
	if message, _ := body["message"].(string); strings.TrimSpace(message) != "" {
		return strings.TrimSpace(message)
	}
	return "平台 API 拒绝了请求"
}

func nestedString(body map[string]any, key, child string) string {
	parent, _ := body[key].(map[string]any)
	if parent == nil {
		return ""
	}
	value, _ := parent[child].(string)
	return strings.TrimSpace(value)
}

func parseChatCompletion(payload []byte) (string, []map[string]any, error) {
	var body map[string]any
	if err := json.Unmarshal(payload, &body); err != nil {
		return "", nil, fmt.Errorf("解析 API 响应失败: %w", err)
	}
	if message := nestedString(body, "error", "message"); message != "" {
		return "", nil, fmt.Errorf("%s", message)
	}
	choices, _ := body["choices"].([]any)
	if len(choices) == 0 {
		return "", nil, fmt.Errorf("API 未返回回答")
	}
	choice, _ := choices[0].(map[string]any)
	if reason, _ := choice["finish_reason"].(string); reason == "length" || reason == "content_filter" {
		return "", nil, fmt.Errorf("API 回答未完整生成: %s", reason)
	}
	message, _ := choice["message"].(map[string]any)
	answer := strings.TrimSpace(contentText(message["content"]))
	if answer == "" {
		return "", nil, fmt.Errorf("API 返回了空回答")
	}
	return answer, collectAPICitations(body), nil
}

func contentText(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case []any:
		var b strings.Builder
		for _, part := range typed {
			item, _ := part.(map[string]any)
			if item == nil {
				continue
			}
			if text, _ := item["text"].(string); text != "" {
				if b.Len() > 0 {
					b.WriteByte('\n')
				}
				b.WriteString(text)
			}
		}
		return b.String()
	default:
		return ""
	}
}

func collectAPICitations(body map[string]any) []map[string]any {
	found := make([]map[string]any, 0)
	seen := map[string]bool{}
	var walk func(any)
	walk = func(value any) {
		switch typed := value.(type) {
		case map[string]any:
			title, _ := typed["title"].(string)
			if title == "" {
				title, _ = typed["name"].(string)
			}
			rawURL, _ := typed["url"].(string)
			if rawURL == "" {
				rawURL, _ = typed["link"].(string)
			}
			rawURL = strings.TrimSpace(rawURL)
			u, err := url.Parse(rawURL)
			if err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Hostname() != "" && u.User == nil && !seen[rawURL] {
				seen[rawURL] = true
				siteName, _ := typed["site_name"].(string)
				if siteName == "" {
					siteName, _ = typed["site"].(string)
				}
				found = append(found, map[string]any{
					"title": title, "url": rawURL, "site_name": siteName, "icon": "",
				})
			}
			for key, child := range typed {
				if key == "icon" {
					continue
				}
				walk(child)
			}
		case []any:
			for _, child := range typed {
				walk(child)
			}
		case []map[string]any:
			for _, child := range typed {
				walk(child)
			}
		}
	}
	walk(body)
	return found
}

func officialAPIResult(site, prompt, answer, model string, citations []map[string]any, elapsedMS int64) map[string]any {
	name := site
	if known, ok := sessions.SiteByID(internalSiteID(site)); ok {
		name = known.Name
	}
	if citations == nil {
		citations = []map[string]any{}
	}
	result := map[string]any{
		"site_id":          site,
		"site_name":        name,
		"ok":               true,
		"prompt":           prompt,
		"answer":           answer,
		"citations":        citations,
		"citation_details": citations,
		"source":           "official_api",
		"model":            model,
		"url":              "",
		"elapsed_ms":       elapsedMS,
		"answer_origin":    "official_api",
		"degraded":         true,
		"share_status":     "not_supported",
		"share_error_code": "api_no_web_share",
		"citations_status": "missing",
		"quality_issues":   []string{"api_fallback_no_web_share"},
	}
	if len(citations) > 0 {
		result["citations_status"] = "available"
	}
	if internalSiteID(site) == "deepseek" {
		result["web_search_status"] = "not_supported"
	}
	return result
}
