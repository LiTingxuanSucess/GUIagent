package jobs

import (
	"errors"
	"path/filepath"
	"testing"

	"geo-server/internal/sessions"
)

func TestOperatorLoginSharesBrowserCapacityAndReleasesOnFailure(t *testing.T) {
	q := &Queue{siteSem: make(chan struct{}, 1)}
	q.siteSem <- struct{}{}
	if _, err := q.CheckAccountLogin(nil, "account", "kimi", nil); !errors.Is(err, sessions.ErrOperatorLoginBusy) || len(q.siteSem) != 1 {
		t.Fatal("operator exceeded customer capacity")
	}
	<-q.siteSem
	pool := sessions.NewPool("/nonexistent/chrome", t.TempDir(), "", filepath.Join(t.TempDir(), "accounts.json"))
	if _, err := q.CheckAccountLogin(pool, "missing", "kimi", nil); err == nil || len(q.siteSem) != 0 {
		t.Fatal("failed operation leaked a browser permit")
	}
}
