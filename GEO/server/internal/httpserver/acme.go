package httpserver

import (
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

const acmeChallengePrefix = "/.well-known/acme-challenge/"

var acmeTokenPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,256}$`)

func (s *Server) acmeChallenge(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	token := strings.TrimPrefix(r.URL.Path, acmeChallengePrefix)
	if !strings.HasPrefix(r.URL.Path, acmeChallengePrefix) || !acmeTokenPattern.MatchString(token) {
		http.NotFound(w, r)
		return
	}
	challenge, err := openACMEChallenge(s.cfg.Root, token)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer challenge.Close()
	info, err := challenge.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > 16*1024 {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Content-Length", strconv.FormatInt(info.Size(), 10))
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.Method == http.MethodGet {
		_, _ = io.CopyN(w, challenge, info.Size())
	}
}

// Anchor every component to an open directory and reject symlinks. A token can
// never select a private file, even if a challenge path is replaced mid-read.
func openACMEChallenge(root, token string) (*os.File, error) {
	directoryFlags := unix.O_RDONLY | unix.O_DIRECTORY | unix.O_CLOEXEC | unix.O_NOFOLLOW
	directory, err := unix.Open(root, directoryFlags, 0)
	if err != nil {
		return nil, err
	}
	defer unix.Close(directory)
	for _, name := range []string{"web", ".well-known", "acme-challenge"} {
		next, err := unix.Openat(directory, name, directoryFlags, 0)
		if err != nil {
			return nil, err
		}
		directory = next
		defer unix.Close(directory)
	}
	fd, err := unix.Openat(directory, token, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), token), nil
}
