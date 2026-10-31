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
)

func TestWebAuthenticationExhaustionNeedsEveryCandidateAndNoPendingSend(t *testing.T) {
	for _, test := range []struct {
		name                        string
		candidates                  []string
		preparation, authentication map[string]bool
		code                        string
		pending, want               bool
	}{
		{"both_verified", []string{"a", "b"}, nil, map[string]bool{"a": true, "b": true}, "platform_auth_unavailable", false, true},
		{"mixed_actual_failures", []string{"a", "b"}, map[string]bool{"a": true}, map[string]bool{"b": true}, "platform_auth_unavailable", false, true},
		{"mixed_last_setup_failure", []string{"a", "b"}, map[string]bool{"b": true}, map[string]bool{"a": true}, "platform_session_unavailable", false, true},
		{"mixed_last_browser_failure", []string{"a", "b"}, map[string]bool{"b": true}, map[string]bool{"a": true}, "browser_start_failed", false, true},
		{"remaining_account", []string{"a", "b"}, nil, map[string]bool{"a": true}, "platform_auth_unavailable", false, false},
		{"no_accounts", nil, nil, map[string]bool{"a": true}, "platform_auth_unavailable", false, false},
		{"busy", []string{"a"}, nil, map[string]bool{"a": true}, "browser_admission_timeout", false, false},
		{"unknown_send", []string{"a"}, nil, map[string]bool{"a": true}, "platform_auth_unavailable", true, false},
		{"no_probe", []string{"a"}, nil, nil, "platform_auth_unavailable", false, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			result := map[string]any{"error_code": test.code, "delivery_resume_required": test.pending}
			if got := exhaustedWebAuthentication(test.candidates, test.preparation, test.authentication, result); got != test.want {
				t.Fatalf("exhaustion=%v want=%v", got, test.want)
			}
		})
	}
}

func sessionUnavailableWaitResult() map[string]any {
	return map[string]any{
		"ok":              false,
		"error_code":      "platform_session_unavailable",
		"web_skipped":     true,
		"web_skip_reason": "session_unavailable",
		"attempts": []map[string]any{{
			"channel": "web", "ok": false, "error_code": "platform_session_unavailable",
			"skipped": true, "skip_reason": "session_unavailable",
		}},
	}
}

func TestDeliverySessionWaitDefaultsToTheFirstWait(t *testing.T) {
	t.Setenv("GEO_DELIVERY_SESSION_WAIT_LIMIT", "")
	if !deliverySessionWaitExhausted(nil, sessionUnavailableWaitResult()) {
		t.Fatal("first recovery-page wait was left as a customer-visible failure")
	}
}

func TestDeliverySessionWaitRequiresRepeatedIdenticalWaits(t *testing.T) {
	t.Setenv("GEO_DELIVERY_SESSION_WAIT_LIMIT", "2")
	first := sessionUnavailableWaitResult()
	if deliverySessionWaitExhausted(nil, first) {
		t.Fatal("first temporary recovery-page wait was exhausted")
	}
	if !deliverySessionWaitExhausted(first, sessionUnavailableWaitResult()) {
		t.Fatal("repeated recovery-page wait was not exhausted")
	}
	other := sessionUnavailableWaitResult()
	other["web_skip_reason"] = "session_setup"
	if deliverySessionWaitExhausted(first, other) {
		t.Fatal("different setup failure was counted as a repeated recovery-page wait")
	}
	pending := sessionUnavailableWaitResult()
	pending["delivery_resume_required"] = true
	if deliverySessionWaitExhausted(first, pending) {
		t.Fatal("uncertain prior send was allowed to fall back")
	}
}

func TestRepeatedDeliverySessionWaitUsesFinalAPI(t *testing.T) {
	t.Setenv("GEO_DELIVERY_SESSION_WAIT_LIMIT", "2")
	var calls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"choices":[{"message":{"content":"final API answer"}}]}`)
	}))
	defer api.Close()
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
	t.Setenv("GEO_API_ENABLE_SEARCH", "0")

	result := (&Queue{}).applyDeliverySessionWaitFallback(
		context.Background(), "doubao", "original question",
		sessionUnavailableWaitResult(), sessionUnavailableWaitResult(),
	)
	if result["ok"] != true || result["source"] != "official_api" || result["web_recovery_exhausted"] != true || calls.Load() != 1 {
		t.Fatalf("repeated session wait did not use final API: result=%v calls=%d", result, calls.Load())
	}
	attempts := cloneAttemptList(result["attempts"])
	if len(attempts) != 2 || attempts[0]["channel"] != "web" || attempts[1]["channel"] != "api" || attempts[1]["ok"] != true {
		t.Fatalf("fallback attempt evidence=%v", attempts)
	}
}

func TestEventualLoginOnlyRecoveryPermitsAPIOnlyAfterAllActualRecoveriesFail(t *testing.T) {
	for _, mode := range []string{"unverified", "mixed_setup_failure", "cli_disabled", "cancelled"} {
		t.Run(mode, func(t *testing.T) {
			fixtureMode := "unverified"
			if mode == "cancelled" {
				fixtureMode = "cancel"
			}
			q := webRecoveryFixture(t, fixtureMode, "first", "second")
			q.Sess = &acquisitionRecoveryPool{q.Sess.(*recoveryPool)}
			if mode == "mixed_setup_failure" {
				port := filepath.Join(q.Sess.(*acquisitionRecoveryPool).leases[1].Manager.ProfileDir, "DevToolsActivePort")
				if err := os.Remove(port); err != nil {
					t.Fatal(err)
				}
			}
			job := deliveryFixtureJob(t, q, "doubao")
			job.Status, job.Prompt = "running", "original question"
			var calls atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				fmt.Fprint(w, `{"choices":[{"message":{"content":"final API answer"}}]}`)
			}))
			defer api.Close()
			t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
			t.Setenv("GEO_DOUBAO_API_BASE", api.URL)
			t.Setenv("GEO_API_ENABLE_SEARCH", "0")
			if mode == "cli_disabled" {
				t.Setenv("GEO_CURSOR_WEB_RECOVERY", "0")
			}
			ctx := context.WithValue(context.Background(), eventualDeliveryKey{}, true)
			if mode == "cancelled" {
				var cancel context.CancelFunc
				ctx, cancel = context.WithTimeout(ctx, 150*time.Millisecond)
				defer cancel()
			}
			result := q.runOneSiteContext(ctx, "doubao", job.Prompt, job.ID, 1)
			if mode == "unverified" || mode == "mixed_setup_failure" {
				if result["ok"] != true || result["source"] != "official_api" || result["web_recovery_exhausted"] != true || result["web_prepare_exhausted"] == true || calls.Load() != 1 {
					t.Fatalf("explicit login exhaustion did not use final API: result=%v calls=%d", result, calls.Load())
				}
				cli, _ := os.ReadFile(filepath.Join(q.GeoRoot, "cli-calls"))
				events, _ := os.ReadFile(filepath.Join(q.GeoRoot, "events"))
				wantCLI, wantProbes := "called\ncalled\n", 4
				if mode == "mixed_setup_failure" {
					wantCLI, wantProbes = "called\n", 2
				}
				if string(cli) != wantCLI || strings.Count(string(events), "probe ") != wantProbes || strings.Contains(string(events), "normal ") || strings.Contains(string(events), "resume ") {
					t.Fatalf("API preceded full recovery or sent a question: cli=%q events=%q", cli, events)
				}
				for _, entry := range cloneAttemptList(result["attempts"]) {
					if entry["channel"] == "web" && entry["skipped"] != true {
						t.Fatal("login probes counted as actual webpage submissions")
					}
				}
			} else if calls.Load() != 0 || result["web_recovery_exhausted"] == true {
				t.Fatalf("incomplete recovery triggered API: result=%v calls=%d", result, calls.Load())
			}
		})
	}
}
