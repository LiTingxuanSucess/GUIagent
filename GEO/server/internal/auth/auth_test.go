package auth

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"geo-server/internal/config"
)

func TestReadAccessThroughLocalTunnel(t *testing.T) {
	store := New(&config.Config{APIKey: "test-api-key"})
	store.sessions["test-admin-session"] = time.Now().Add(time.Hour)
	for _, tc := range []struct {
		name, allowLoopback, remote, key, session string
		want                                      bool
	}{
		{"local default", "", "127.0.0.1:1234", "", "", true},
		{"tunnel anonymous", "0", "127.0.0.1:1234", "", "", false},
		{"tunnel IPv6 anonymous", "0", "[::1]:1234", "", "", false},
		{"tunnel API key", "0", "127.0.0.1:1234", "test-api-key", "", true},
		{"tunnel wrong API key", "0", "127.0.0.1:1234", "wrong-key", "", false},
		{"tunnel admin", "0", "127.0.0.1:1234", "", "test-admin-session", true},
		{"external anonymous", "", "203.0.113.1:1234", "", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("GEO_ALLOW_LOOPBACK_READ", tc.allowLoopback)
			r := httptest.NewRequest(http.MethodGet, "/api/tree", nil)
			r.RemoteAddr = tc.remote
			r.Header.Set("X-API-Key", tc.key)
			if tc.session != "" {
				r.AddCookie(&http.Cookie{Name: CookieName, Value: tc.session})
			}
			if got := store.CanRead(r); got != tc.want {
				t.Fatalf("CanRead = %v, want %v", got, tc.want)
			}
		})
	}
}
