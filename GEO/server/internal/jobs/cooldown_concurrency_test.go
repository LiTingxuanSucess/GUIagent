package jobs

import (
	"encoding/json"
	"os"
	"sync"
	"testing"
)

func TestConcurrentWebFailuresPreserveCooldownHistory(t *testing.T) {
	clearOfficialAPIKeys(t)
	t.Setenv("GEO_DOUBAO_API_KEY", "fixture")
	t.Setenv("GEO_SITE_COOLDOWN_ENABLED", "1")
	q := &Queue{DataDir: t.TempDir()}
	var wg sync.WaitGroup
	for i := 0; i < 24; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			q.recordSiteCooldown("doubao", siteError("doubao", "question", "platform_busy", "still generating", false, 1))
		}()
	}
	wg.Wait()
	raw, err := os.ReadFile(q.cooldownPath("doubao"))
	if err != nil {
		t.Fatal(err)
	}
	var state siteCooldownState
	if err := json.Unmarshal(raw, &state); err != nil {
		t.Fatal(err)
	}
	if state.Failures != 24 || q.siteCooldown("doubao", "doubao", "question") == nil {
		t.Fatalf("parallel failures lost history or cooldown: failures=%d", state.Failures)
	}
}
