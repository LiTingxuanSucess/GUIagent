package auth

import (
	"crypto/subtle"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"geo-server/internal/config"
)

const CookieName = "geo_admin"
const TenantCookie = "geo_tenant"

type tenantSess struct {
	AK  string
	Exp time.Time
}

type Store struct {
	cfg      *config.Config
	mu       sync.Mutex
	sessions map[string]time.Time
	tenants  map[string]tenantSess
}

func New(cfg *config.Config) *Store {
	return &Store{cfg: cfg, sessions: map[string]time.Time{}, tenants: map[string]tenantSess{}}
}

func (s *Store) Login(user, password string) (string, error) {
	if subtle.ConstantTimeCompare([]byte(user), []byte(s.cfg.Admin.User)) != 1 {
		return "", errAuth
	}
	if !s.cfg.CheckPassword(password) {
		return "", errAuth
	}
	tok, err := config.RandomToken()
	if err != nil {
		return "", err
	}
	s.mu.Lock()
	s.sessions[tok] = time.Now().Add(24 * time.Hour)
	s.mu.Unlock()
	return tok, nil
}

func (s *Store) Logout(tok string) {
	s.mu.Lock()
	delete(s.sessions, tok)
	s.mu.Unlock()
}

func (s *Store) Valid(tok string) bool {
	if tok == "" {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	exp, ok := s.sessions[tok]
	if !ok {
		return false
	}
	if time.Now().After(exp) {
		delete(s.sessions, tok)
		return false
	}
	return true
}

func CookieToken(r *http.Request) string {
	c, err := r.Cookie(CookieName)
	if err != nil {
		return ""
	}
	return c.Value
}

func (s *Store) IsAdmin(r *http.Request) bool {
	return s.Valid(CookieToken(r))
}

func (s *Store) IsAPIKey(r *http.Request) bool {
	key := r.Header.Get("X-API-Key")
	if key == "" {
		key = r.URL.Query().Get("api_key")
	}
	if s.cfg.APIKey == "" || key == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(key), []byte(s.cfg.APIKey)) == 1
}

func IsLoopback(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func (s *Store) CanRead(r *http.Request) bool {
	if s.IsAdmin(r) || s.IsAPIKey(r) {
		return true
	}
	return os.Getenv("GEO_ALLOW_LOOPBACK_READ") != "0" && IsLoopback(r)
}

func cookieSecure(r *http.Request) bool {
	return r != nil && r.TLS != nil
}

func (s *Store) SetCookie(w http.ResponseWriter, r *http.Request, token string) {
	http.SetCookie(w, &http.Cookie{
		Name:     CookieName,
		Value:    token,
		Path:     "/",
		MaxAge:   86400,
		HttpOnly: true,
		Secure:   cookieSecure(r),
		SameSite: http.SameSiteLaxMode,
	})
}

func (s *Store) ClearCookie(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{
		Name:     CookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   cookieSecure(r),
		SameSite: http.SameSiteLaxMode,
	})
}

func (s *Store) LoginTenant(ak string) (string, error) {
	tok, err := config.RandomToken()
	if err != nil {
		return "", err
	}
	s.mu.Lock()
	s.tenants[tok] = tenantSess{AK: ak, Exp: time.Now().Add(24 * time.Hour)}
	s.mu.Unlock()
	return tok, nil
}

func (s *Store) TenantAK(r *http.Request) string {
	c, err := r.Cookie(TenantCookie)
	if err != nil || c.Value == "" {
		return ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.tenants[c.Value]
	if !ok {
		return ""
	}
	if time.Now().After(st.Exp) {
		delete(s.tenants, c.Value)
		return ""
	}
	return st.AK
}

func (s *Store) SetTenantCookie(w http.ResponseWriter, r *http.Request, token string) {
	http.SetCookie(w, &http.Cookie{
		Name:     TenantCookie,
		Value:    token,
		Path:     "/",
		MaxAge:   86400,
		HttpOnly: true,
		Secure:   cookieSecure(r),
		SameSite: http.SameSiteLaxMode,
	})
}

func (s *Store) ClearTenantCookie(w http.ResponseWriter, r *http.Request) {
	c, err := r.Cookie(TenantCookie)
	if err == nil && c.Value != "" {
		s.mu.Lock()
		delete(s.tenants, c.Value)
		s.mu.Unlock()
	}
	http.SetCookie(w, &http.Cookie{
		Name:     TenantCookie,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   cookieSecure(r),
		SameSite: http.SameSiteLaxMode,
	})
}

var errAuth = errString("用户名或密码错误")

type errString string

func (e errString) Error() string { return string(e) }

func BearerOrQuery(r *http.Request) string {
	if h := r.Header.Get("Authorization"); strings.HasPrefix(strings.ToLower(h), "bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}
