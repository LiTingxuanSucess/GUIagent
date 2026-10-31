package httpserver

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMonitorPendingDeliveryMetadataSurvivesListAndDetail(t *testing.T) {
	h, token, root := monitorTestServer(t)
	now := time.Now()
	row := map[string]any{"id": "pending_job", "status": "queued", "created": now.Unix(), "updated": now.Unix(), "delivery_policy": "eventual", "retry_at": now.Add(time.Minute).Format(time.RFC3339Nano), "retry_count": 3, "wait_reason": "recovery_state_missing", "prompt": "INDEX_SECRET_PROMPT", "journal_path": "INDEX_SECRET_PATH", "results": []any{map[string]any{"site": "doubao", "ok": false, "delivery_pending": true, "retry_state": "awaiting_confirmation", "attempts_truncated": true, "attempts_dropped": 4, "error_code": "recovery_state_missing", "attempts": []any{map[string]any{"channel": "web", "ok": false, "status": "failed", "error_code": "generation_timeout"}}}}}
	writeMonitorJSON(t, filepath.Join(root, "data/monitor-requests.json"), map[string]any{"updated_at": now.Unix(), "rows": []any{row}})
	for _, path := range []string{"/api/admin/monitor/requests?window=today&status=queued&channel=web&outcome=failed", "/api/admin/monitor/requests/pending_job"} {
		w := monitorTestRequest(h, token, http.MethodGet, path)
		if w.Code != 200 || strings.Contains(w.Body.String(), "INDEX_SECRET") {
			t.Fatalf("unsafe or unavailable pending metadata: %d %s", w.Code, w.Body.String())
		}
		var actual monitorRequest
		if strings.Contains(path, "?") {
			var list struct {
				Rows []monitorRequest `json:"rows"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &list); err != nil || len(list.Rows) != 1 {
				t.Fatalf("actual failed attempt was lost behind queued status: %s", w.Body.String())
			}
			actual = list.Rows[0]
		} else if err := json.Unmarshal(w.Body.Bytes(), &actual); err != nil {
			t.Fatal(err)
		}
		if actual.DeliveryPolicy != "eventual" || actual.RetryCount != 3 || actual.WaitReason != "recovery_state_missing" || actual.RetryAt == "" || len(actual.Results) != 1 || !actual.Results[0].DeliveryPending || actual.Results[0].RetryState != "awaiting_confirmation" || actual.Results[0].AttemptsDropped != 4 {
			t.Fatalf("pending contract was dropped: %+v", actual)
		}
	}
}

func TestMonitorDeliveryMetadataRejectsUnrecognizedOptionalValues(t *testing.T) {
	for _, raw := range []string{
		`{"delivery_policy":"SECRET_POLICY","retry_at":"javascript:SECRET_TOKEN","results":[{"retry_state":"SECRET_STATE"}]}`,
		`{"delivery_policy":{},"retry_at":42,"results":[{"retry_state":true}]}`,
	} {
		var row monitorRequest
		if err := json.Unmarshal([]byte(raw), &row); err != nil {
			t.Fatal(err)
		}
		out, _ := json.Marshal(row)
		if row.DeliveryPolicy != "" || row.RetryAt != "" || row.Results[0].RetryState != "" || strings.Contains(string(out), "SECRET_") {
			t.Fatalf("arbitrary delivery metadata survived decoding: %s", out)
		}
	}
}
