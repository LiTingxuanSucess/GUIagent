package httpserver

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"testing/fstest"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/jobs"
)

func TestSignedCancelEnforcesTenantAndIdempotency(t *testing.T) {
	root := t.TempDir()
	raw, _ := json.Marshal(map[string]any{"expires_at": time.Now().Add(time.Minute).Unix(), "allow": []string{}})
	if err := os.WriteFile(filepath.Join(root, ".deployment-drain.json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	cfg := config.Default(root)
	keys, key, err := auth.LoadKeys(filepath.Join(root, "keys.json"))
	if err != nil {
		t.Fatal(err)
	}
	q := jobs.New(root, nil, keys, filepath.Join(root, "jobs"))
	t.Cleanup(func() {
		for _, job := range q.List() {
			if _, err := q.CancelVisible(job.ID, "", true); err != nil {
				t.Error(err)
			}
		}
	})
	one, _, err := q.Submit("cancel me", []string{"doubao"}, "", key.AK, "one")
	if err != nil {
		t.Fatal(err)
	}
	other, _, err := q.Submit("other tenant", []string{"doubao"}, "", "other-ak", "two")
	if err != nil {
		t.Fatal(err)
	}
	s := New(cfg, auth.New(cfg), keys, nil, nil, q, nil, fstest.MapFS{})
	sequence := 0
	request := func(method, path string, signed bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, nil)
		if signed {
			sequence++
			ts := fmt.Sprint(time.Now().Unix())
			nonce := fmt.Sprintf("cancel-nonce-%d", sequence)
			r.Header.Set(auth.HeaderAK, key.AK)
			r.Header.Set(auth.HeaderTimestamp, ts)
			r.Header.Set(auth.HeaderNonce, nonce)
			r.Header.Set(auth.HeaderSign, auth.Sign(key.SK, auth.Canonical(method, r.URL.Path, ts, nonce, auth.BodyHash(nil))))
		}
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, r)
		return w
	}
	path := "/api/v1/jobs/" + one.ID + "/cancel"
	if w := request("POST", path, false); w.Code != http.StatusUnauthorized {
		t.Fatalf("unsigned: %d", w.Code)
	}
	if w := request("GET", path, true); w.Code != 405 {
		t.Fatalf("wrong method: %d", w.Code)
	}
	for _, id := range []string{other.ID, "missing"} {
		if w := request("POST", "/api/v1/jobs/"+id+"/cancel", true); w.Code != 404 {
			t.Fatalf("ownership: %d %s", w.Code, w.Body.String())
		}
	}
	for i := 0; i < 2; i++ {
		w := request("POST", path, true)
		var got jobs.Job
		if json.Unmarshal(w.Body.Bytes(), &got) != nil || w.Code != 200 || got.Status != "cancelled" || got.AK != "" {
			t.Fatalf("cancel response: %d %s", w.Code, w.Body.String())
		}
	}
	if w := request("GET", "/api/v1/jobs?status=cancelled", true); w.Code != 200 {
		t.Fatalf("cancelled filter: %s", w.Body.String())
	}
}
