package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCompletedAnswerSurvivesLaterStructuredFailure(t *testing.T) {
	for _, code := range []string{"answer_extract_timeout", "share_timeout", "platform_auth_unavailable"} {
		t.Run(code, func(t *testing.T) {
			root := t.TempDir()
			script := filepath.Join(root, "after-answer.py")
			checkpoint, _ := json.Marshal(map[string]any{"web_answer_completed": true, "ok": true, "site_id": "deepseek", "question_id": "job_current", "prompt": "question", "answer": "complete current answer", "answer_origin": "web", "source": "api:history", "citations": []string{"https://example.org/source"}, "url": "https://unverified.example.org/share"})
			body := fmt.Sprintf("import json\nprint(%q,flush=True)\nprint(json.dumps({'ok':False,'error_code':%q,'error':'optional stage failed','retryable':False}))\n", string(checkpoint), code)
			if err := os.WriteFile(script, []byte(body), 0600); err != nil {
				t.Fatal(err)
			}
			q := &Queue{GeoRoot: root, Python: "python3", Script: script}
			result, err := q.scrapeForContext(context.Background(), "http://127.0.0.1:9", "deepseek", "question", "current", "", 2*time.Second)
			if err != nil || result["ok"] != true || result["answer"] != "complete current answer" || result["url"] != "" || result["web_answer_recovered"] != true || result["optional_stage_error_code"] != code {
				t.Fatalf("completed answer discarded or unverified share published: result=%v err=%v", result, err)
			}
		})
	}
}
