package jobs

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestConsoleSummaryPagesStaySmallAndKeepDetails(t *testing.T) {
	q := &Queue{jobs: map[string]*Job{}}
	for i := 0; i < 123; i++ {
		id := fmt.Sprintf("%03d", i)
		q.jobs[id] = &Job{ID: id, AK: "tenant-a", CreatedAt: "same-time", Prompt: strings.Repeat("中文", 10000),
			Status: "succeeded", Sites: []string{"doubao"}, CallbackURL: "https://callback.example/?token=private",
			Results: []map[string]any{{"site_id": "doubao", "ok": true, "answer": strings.Repeat("payload", 10000), "account_id": "private-account"}}}
	}
	q.jobs["other"] = &Job{ID: "other", AK: "tenant-b", CreatedAt: "newer"}
	rows, total := q.ListSummariesVisible("tenant-a", false, 50, 50)
	if total != 123 || len(rows) != 50 || rows[0].ID != "072" || rows[49].ID != "023" {
		t.Fatalf("incorrect filtered page: total=%d len=%d first=%s", total, len(rows), rows[0].ID)
	}
	raw, _ := json.Marshal(rows)
	if len(raw) > 100000 || strings.Contains(string(raw), "payload") || strings.Contains(string(raw), "private") || strings.Contains(string(raw), "tenant-a") {
		t.Fatal("summary leaked full payload, callback credentials, or tenant-only metadata")
	}
	if len([]rune(rows[0].Prompt)) != 160 || !rows[0].HasCallback || rows[0].Results[0]["ok"] != true {
		t.Fatal("summary lost visible fields or split UTF-8")
	}
	rows[0].Results[0]["ok"] = false
	rows[0].Sites[0] = "changed"
	stored, _ := q.GetVisible("072", "tenant-a", false)
	if len(stored.Prompt) <= len(rows[0].Prompt) || stored.Results[0]["ok"] != true || stored.Sites[0] != "doubao" || !strings.Contains(stored.Results[0]["answer"].(string), "payload") {
		t.Fatal("listing mutated or truncated stored details")
	}
	last, _ := q.ListSummariesVisible("tenant-a", false, 100, 50)
	if len(last) != 23 {
		t.Fatal("last page lost history")
	}
	empty, n := q.ListSummariesVisible("tenant-a", false, 9999, 50)
	if len(empty) != 0 || n != 123 {
		t.Fatal("out-of-range page invalid")
	}
	_, all := q.ListSummariesVisible("", true, 0, 50)
	_, anonymous := q.ListSummariesVisible("", false, 0, 50)
	if all != 124 || anonymous != 0 {
		t.Fatal("admin/tenant visibility invalid")
	}
}
