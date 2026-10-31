package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestSixAPISearchPathsPreserveOriginalQuestion(t *testing.T) {
	const prompt = "全人智能科技是做什么的？"
	for _, site := range []string{"doubao", "yuanbao", "qianwen", "yiyan", "kimi", "deepseek"} {
		t.Run(site, func(t *testing.T) {
			clearOfficialAPIKeys(t)
			t.Setenv("GEO_API_ENABLE_SEARCH", "1")
			t.Setenv("GEO_QIANWEN_API_THINKING", "1")
			var searches, answers atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if r.URL.Path == "/tools/search" || r.URL.Path == "/v1/tools/search" {
					searches.Add(1)
					if r.URL.Path == "/v1/tools/search" {
						if body["query"] != prompt || body["ori_question"] != prompt {
							t.Error("DeepSeek native search changed the original question")
						}
						fmt.Fprint(w, `{"results":[{"url":"https://example.org/source","title":"verified fixture","summary":"official summary"}]}`)
						return
					}
					if body["text_query"] != prompt {
						t.Error("structured search changed the original question")
					}
					fmt.Fprint(w, `{"search_results":[{"url":"https://example.org/source","title":"verified fixture"}]}`)
					return
				}
				answers.Add(1)
				if body["model"] != "fixture-"+site {
					t.Error("search changed the provider's answer model")
				}
				if site == "doubao" {
					tools, _ := body["tools"].([]any)
					if r.URL.Path != "/responses" || body["input"] != prompt || body["tool_choice"] != "required" || len(tools) != 1 || tools[0].(map[string]any)["type"] != "web_search" {
						t.Error("Doubao did not force its web search tool with the unchanged question")
					}
					fmt.Fprint(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"fixture answer","annotations":[{"type":"url_citation","url":"https://example.org/source"}]}]}]}`)
					return
				}
				messages, _ := body["messages"].([]any)
				if site == "kimi" || site == "deepseek" {
					if len(messages) != 3 || messages[1].(map[string]any)["content"] != prompt || !strings.Contains(messages[2].(map[string]any)["content"].(string), "https://example.org/source") {
						t.Error("retrieval changed the question or did not feed real sources to the answer model")
					}
					if body["tools"] != nil || body["enable_search"] != nil {
						t.Error("external retrieval was mislabeled as native built-in search")
					}
				} else if len(messages) != 1 || messages[0].(map[string]any)["content"] != prompt {
					t.Error("native search appended instructions to the user's question")
				}
				switch site {
				case "qianwen":
					options, _ := body["search_options"].(map[string]any)
					if body["enable_search"] != true || options["forced_search"] != true || body["enable_thinking"] != true {
						t.Error("Qwen compatible-mode search was not forced or reasoning options were lost")
					}
				case "yuanbao":
					options, _ := body["web_search_options"].(map[string]any)
					if options["enable"] != true {
						t.Error("Yuanbao native search was not enabled")
					}
				case "yiyan":
					options, _ := body["web_search"].(map[string]any)
					if options["enable"] != true || options["enable_trace"] != true || options["enable_citation"] != true || options["enable_status"] != true {
						t.Error("Yiyan native search/source reporting was not enabled")
					}
					fmt.Fprintln(w, `data: {"choices":[{"delta":{"content":"fixture answer"}}],"search_results":[{"url":"https://example.org/source"}]}`)
					fmt.Fprintln(w, "\ndata: [DONE]")
					return
				}
				fmt.Fprint(w, `{"choices":[{"finish_reason":"stop","message":{"content":"fixture answer","search_results":[{"url":"https://example.org/source"}]}}]}`)
			}))
			defer api.Close()
			prefix := "GEO_" + strings.ToUpper(site)
			t.Setenv(prefix+"_API_KEY", "fixture-key")
			t.Setenv(prefix+"_API_BASE", api.URL)
			t.Setenv(prefix+"_API_MODEL", "fixture-"+site)
			t.Setenv("GEO_KIMI_API_KEY", "fixture-search-key")
			t.Setenv("GEO_KIMI_API_BASE", api.URL)
			result := (&Queue{}).applyAPIFallback(site, prompt, siteError(site, prompt, "platform_busy", "busy", true, 10))
			if result["ok"] != true || result["prompt"] != prompt || result["web_search_status"] != "used" || answers.Load() != 1 {
				t.Fatal("search lost the answer, changed the question or misreported evidence")
			}
			policy := "automatic"
			if site == "doubao" || site == "qianwen" {
				policy = "forced"
			}
			if site == "kimi" || site == "deepseek" {
				policy = "required_retrieval"
				if searches.Load() != 1 {
					t.Error("structured search was skipped")
				}
			} else if searches.Load() != 0 {
				t.Error("confirmed native sources caused redundant retrieval")
			}
			if result["web_search_policy"] != policy {
				t.Error("actual search policy was not reported accurately")
			}
		})
	}
}

func TestNativeDashScopeForcesSearchWithoutQuestionSuffix(t *testing.T) {
	const prompt = "行业数据在哪里找？"
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		parameters := body["parameters"].(map[string]any)
		options := parameters["search_options"].(map[string]any)
		messages := body["input"].(map[string]any)["messages"].([]any)
		if len(messages) != 1 || messages[0].(map[string]any)["content"] != prompt || parameters["enable_search"] != true || options["forced_search"] != true || options["enable_source"] != true {
			t.Error("DashScope lost forced retrieval, sources or the unchanged question")
		}
		fmt.Fprint(w, `{"output":{"choices":[{"finish_reason":"stop","message":{"content":"fixture answer"}}],"search_info":{"search_results":[{"url":"https://example.org/source"}]}}}`)
	}))
	defer api.Close()
	answer, sources, err := postDashScopeSearch(context.Background(), api.URL, "fixture-key", "qwen-plus", prompt)
	if err != nil || answer == "" || len(sources) != 1 {
		t.Fatal("DashScope search lost valid output")
	}
}

func TestNativeMissingSourcesRecoversWithSameModelAndKeepsCompletedAnswerOnFailure(t *testing.T) {
	for _, site := range []string{"yuanbao", "doubao", "qianwen"} {
		for _, scenario := range []string{"recover", "search_down", "regeneration_down"} {
			t.Run(site+"/"+scenario, func(t *testing.T) {
				clearOfficialAPIKeys(t)
				t.Setenv("GEO_API_ENABLE_SEARCH", "1")
				const prompt = "机器人品牌推荐"
				var searches, regenerated atomic.Int32
				api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var body map[string]any
					_ = json.NewDecoder(r.Body).Decode(&body)
					if r.URL.Path == "/tools/search" {
						searches.Add(1)
						if body["text_query"] != prompt {
							t.Error("retrieval query changed the question")
						}
						if scenario == "search_down" {
							w.WriteHeader(503)
							fmt.Fprint(w, `{"error":{"message":"fixture search down"}}`)
							return
						}
						fmt.Fprint(w, `{"search_results":[{"url":"https://example.org/retrieved","snippet":"retrieved evidence"}]}`)
						return
					}
					if body["model"] != "fixture-"+site {
						t.Error("recovery changed the answer provider/model")
					}
					messages, _ := body["messages"].([]any)
					if r.URL.Path == "/responses" {
						fmt.Fprint(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"native completed answer"}]}]}`)
						return
					}
					if len(messages) == 1 {
						if messages[0].(map[string]any)["content"] != prompt {
							t.Error("native call appended a search instruction")
						}
						fmt.Fprint(w, `{"choices":[{"finish_reason":"stop","message":{"content":"native completed answer"}}]}`)
						return
					}
					regenerated.Add(1)
					if len(messages) != 3 || messages[1].(map[string]any)["content"] != prompt || !strings.Contains(messages[2].(map[string]any)["content"].(string), "https://example.org/retrieved") {
						t.Error("regeneration did not consume real retrieved sources with the original question")
					}
					if scenario == "regeneration_down" {
						w.WriteHeader(503)
						fmt.Fprint(w, `{"error":{"message":"fixture generation down"}}`)
						return
					}
					fmt.Fprint(w, `{"choices":[{"finish_reason":"stop","message":{"content":"answer from retrieved evidence[1]"}}]}`)
				}))
				defer api.Close()
				prefix := "GEO_" + strings.ToUpper(site)
				t.Setenv(prefix+"_API_KEY", "fixture-key")
				t.Setenv(prefix+"_API_BASE", api.URL)
				t.Setenv(prefix+"_API_MODEL", "fixture-"+site)
				t.Setenv("GEO_KIMI_API_KEY", "fixture-search-key")
				t.Setenv("GEO_KIMI_API_BASE", api.URL)
				r := (&Queue{}).applyAPIFallback(site, prompt, map[string]any{"ok": false, "error_code": "platform_busy"})
				if r["ok"] != true || r["prompt"] != prompt || searches.Load() != 1 {
					t.Fatal("missing-source recovery lost a completed answer or skipped retrieval")
				}
				refs := r["citations"].([]map[string]any)
				if scenario == "recover" {
					if len(refs) != 1 || regenerated.Load() != 1 || r["web_search_status"] != "used" || r["web_search_policy"] != "required_retrieval" || r["model"] != "fixture-"+site {
						t.Fatal("successful recovery did not expose the same model's grounded answer")
					}
				} else if len(refs) != 0 || r["answer"] != "native completed answer" || r["web_search_status"] != "not_confirmed" {
					t.Fatal("recovery failure attached unrelated sources or discarded the completed native answer")
				}
			})
		}
	}
}
