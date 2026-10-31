package jobs

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

// Opt-in smoke check: credentials come from the normal private environment.
// Never log the key, prompt, answer, or raw upstream error.
func TestDoubaoLiveAPISmoke(t *testing.T) {
	if os.Getenv("GEO_LIVE_API_SMOKE") != "1" {
		t.Skip("set GEO_LIVE_API_SMOKE=1 for a real provider call")
	}
	if !officialAPIConfigured("doubao") {
		t.Fatal("Doubao API is not configured")
	}
	ctx, cancel := context.WithTimeout(context.Background(), apiTimeout("doubao"))
	defer cancel()
	start := time.Now()
	answer, _, citations, err := askOfficialAPI(ctx, "doubao", "查询中国科学院官网最近一条科研新闻，给出标题、日期和来源链接，100字以内。")
	if err != nil {
		t.Fatalf("provider call failed (deadline_exceeded=%t, elapsed_ms=%d)", ctx.Err() != nil, time.Since(start).Milliseconds())
	}
	if strings.TrimSpace(answer) == "" {
		t.Fatal("provider returned no answer")
	}
	t.Logf("budget_sec=%d elapsed_ms=%d answer_chars=%d citations=%d", int(apiTimeout("doubao")/time.Second), time.Since(start).Milliseconds(), len([]rune(answer)), len(citations))
}
