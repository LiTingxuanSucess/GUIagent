package httpserver

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"testing/fstest"

	"geo-server/internal/config"
)

func TestACMEChallengeServesOnlyValidatedTokens(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "web", ".well-known", "acme-challenge")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	token, response := "Abc_-0123", "Abc_-0123.fixture-thumbprint"
	if err := os.WriteFile(filepath.Join(dir, token), []byte(response), 0600); err != nil {
		t.Fatal(err)
	}
	private := filepath.Join(root, "private.txt")
	if err := os.WriteFile(private, []byte("private credential"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(private, filepath.Join(dir, "symlink")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "directory"), 0700); err != nil {
		t.Fatal(err)
	}
	server := New(config.Default(root), nil, nil, nil, nil, nil, nil, fstest.MapFS{})
	handler := server.Handler()
	for _, method := range []string{http.MethodGet, http.MethodHead} {
		request := httptest.NewRequest(method, acmeChallengePrefix+token, nil)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, request)
		if w.Code != http.StatusOK || w.Header().Get("Content-Length") != strconv.Itoa(len(response)) || w.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("valid %s challenge failed: %d %q %v", method, w.Code, w.Body.String(), w.Header())
		}
		if method == http.MethodGet && w.Body.String() != response || method == http.MethodHead && w.Body.Len() != 0 {
			t.Fatalf("unexpected %s challenge body: %q", method, w.Body.String())
		}
	}
	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodDelete} {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(method, acmeChallengePrefix+token, nil))
		if w.Code != http.StatusMethodNotAllowed || w.Header().Get("Allow") != "GET, HEAD" || strings.Contains(w.Body.String(), response) {
			t.Fatalf("unsupported method exposed a challenge: %s %d %q", method, w.Code, w.Body.String())
		}
	}
	for _, invalid := range []string{"", "missing", ".", "..", "../../private.txt", "a/b", "a%2Fb", "private.txt", "a.b", strings.Repeat("a", 257), "directory", "symlink"} {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, acmeChallengePrefix+invalid, nil))
		if w.Code == http.StatusOK || strings.Contains(w.Body.String(), "private credential") || strings.Contains(w.Body.String(), response) {
			t.Fatalf("invalid token exposed file contents: %q %d %q", invalid, w.Code, w.Body.String())
		}
	}
}

func TestACMEChallengeRejectsSymlinkedDirectories(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "token"), []byte("private credential"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "web", ".well-known"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "web", ".well-known", "acme-challenge")); err != nil {
		t.Fatal(err)
	}
	server := New(config.Default(root), nil, nil, nil, nil, nil, nil, fstest.MapFS{})
	w := httptest.NewRecorder()
	server.Handler().ServeHTTP(w, httptest.NewRequest(http.MethodGet, acmeChallengePrefix+"token", nil))
	if w.Code != http.StatusNotFound || strings.Contains(w.Body.String(), "private credential") {
		t.Fatal("a symlinked challenge directory escaped the configured webroot")
	}
}
