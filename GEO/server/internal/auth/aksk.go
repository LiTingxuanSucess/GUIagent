package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"
)

const (
	HeaderAK        = "X-Geo-AK"
	HeaderTimestamp = "X-Geo-Timestamp"
	HeaderNonce     = "X-Geo-Nonce"
	HeaderSign      = "X-Geo-Signature"
	maxSkew         = 5 * time.Minute
	nonceTTL        = 10 * time.Minute
)

type Key struct {
	AK             string `json:"ak"`
	SK             string `json:"sk"`
	Name           string `json:"name"`
	User           string `json:"user,omitempty"`
	PasswordBcrypt string `json:"password_bcrypt,omitempty"`
	Enabled        bool   `json:"enabled"`
	CreatedAt      string `json:"created_at"`
}

type keyFile struct {
	Keys []Key `json:"keys"`
}

type KeyStore struct {
	path string
	mu   sync.Mutex
	keys map[string]Key
	seen map[string]time.Time
}

func LoadKeys(path string) (*KeyStore, *Key, error) {
	s := &KeyStore{path: path, keys: map[string]Key{}, seen: map[string]time.Time{}}
	raw, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, nil, err
	}
	if err == nil && len(raw) > 0 {
		var f keyFile
		if err := json.Unmarshal(raw, &f); err != nil {
			return nil, nil, fmt.Errorf("aksk json: %w", err)
		}
		for _, k := range f.Keys {
			if k.AK != "" && k.SK != "" {
				s.keys[k.AK] = k
			}
		}
	}
	var created *Key
	if len(s.keys) == 0 {
		k, err := newKey("default")
		if err != nil {
			return nil, nil, err
		}
		s.keys[k.AK] = *k
		if err := s.saveLocked(); err != nil {
			return nil, nil, err
		}
		created = k
	}
	return s, created, nil
}

func newKey(name string) (*Key, error) {
	akb := make([]byte, 8)
	skb := make([]byte, 32)
	if _, err := rand.Read(akb); err != nil {
		return nil, err
	}
	if _, err := rand.Read(skb); err != nil {
		return nil, err
	}
	return &Key{
		AK:        "geo_" + hex.EncodeToString(akb),
		SK:        hex.EncodeToString(skb),
		Name:      name,
		Enabled:   true,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	}, nil
}

func (s *KeyStore) saveLocked() error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	f := keyFile{Keys: make([]Key, 0, len(s.keys))}
	for _, k := range s.keys {
		f.Keys = append(f.Keys, k)
	}
	raw, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(s.path, raw, 0o600)
}

func Canonical(method, path, ts, nonce, bodyHash string) string {
	return strings.ToUpper(method) + "\n" + path + "\n" + ts + "\n" + nonce + "\n" + bodyHash
}

func BodyHash(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

func Sign(sk, canonical string) string {
	mac := hmac.New(sha256.New, []byte(sk))
	_, _ = mac.Write([]byte(canonical))
	return hex.EncodeToString(mac.Sum(nil))
}

func (s *KeyStore) Verify(r *http.Request, body []byte) (*Key, error) {
	ak := r.Header.Get(HeaderAK)
	ts := r.Header.Get(HeaderTimestamp)
	nonce := r.Header.Get(HeaderNonce)
	sign := r.Header.Get(HeaderSign)
	if ak == "" {
		ak = r.URL.Query().Get("ak")
		ts = r.URL.Query().Get("ts")
		nonce = r.URL.Query().Get("nonce")
		sign = r.URL.Query().Get("sign")
	}
	if ak == "" || ts == "" || nonce == "" || sign == "" {
		return nil, fmt.Errorf("缺少 AK/SK 签名头")
	}
	if len(nonce) < 8 || len(nonce) > 64 {
		return nil, fmt.Errorf("nonce 无效")
	}
	sec, err := strconv.ParseInt(ts, 10, 64)
	if err != nil {
		return nil, fmt.Errorf("时间戳无效")
	}
	t := time.Unix(sec, 0)
	if d := time.Since(t); d > maxSkew || d < -maxSkew {
		return nil, fmt.Errorf("时间戳过期")
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.purgeLocked()
	if _, dup := s.seen[ak+"|"+nonce]; dup {
		return nil, fmt.Errorf("nonce 已使用")
	}
	k, ok := s.keys[ak]
	if !ok || !k.Enabled || k.SK == "" {
		return nil, fmt.Errorf("AK 无效")
	}
	canon := Canonical(r.Method, r.URL.Path, ts, nonce, BodyHash(body))
	want := Sign(k.SK, canon)
	if subtle.ConstantTimeCompare([]byte(strings.ToLower(sign)), []byte(want)) != 1 {
		return nil, fmt.Errorf("签名错误")
	}
	s.seen[ak+"|"+nonce] = time.Now().Add(nonceTTL)
	cp := k
	return &cp, nil
}

func (s *KeyStore) purgeLocked() {
	now := time.Now()
	for k, exp := range s.seen {
		if now.After(exp) {
			delete(s.seen, k)
		}
	}
}

func (s *KeyStore) Lookup(ak string) (Key, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k, ok := s.keys[ak]
	if !ok || !k.Enabled {
		return Key{}, false
	}
	return k, true
}

func (s *KeyStore) CheckSK(ak, sk string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	k, ok := s.keys[ak]
	if !ok || !k.Enabled || k.SK == "" || sk == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(k.SK), []byte(sk)) == 1
}

func (s *KeyStore) userTakenLocked(user, exceptAK string) bool {
	for _, k := range s.keys {
		if k.User != "" && k.User == user && k.AK != exceptAK {
			return true
		}
	}
	return false
}

func (s *KeyStore) AuthenticateUser(user, password string) (Key, error) {
	user = strings.TrimSpace(user)
	if user == "" || password == "" {
		return Key{}, fmt.Errorf("用户名或密码错误")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, k := range s.keys {
		if !k.Enabled || k.User == "" || k.PasswordBcrypt == "" {
			continue
		}
		if subtle.ConstantTimeCompare([]byte(k.User), []byte(user)) != 1 {
			continue
		}
		if bcrypt.CompareHashAndPassword([]byte(k.PasswordBcrypt), []byte(password)) != nil {
			return Key{}, fmt.Errorf("用户名或密码错误")
		}
		return k, nil
	}
	return Key{}, fmt.Errorf("用户名或密码错误")
}

func (s *KeyStore) Create(name, user, password, reservedAdmin string) (*Key, error) {
	name = strings.TrimSpace(name)
	user = strings.TrimSpace(user)
	if name == "" {
		name = "tenant"
	}
	if user == "" {
		return nil, fmt.Errorf("需要登录用户名")
	}
	if len(password) < 8 {
		return nil, fmt.Errorf("密码至少 8 位")
	}
	if strings.EqualFold(user, reservedAdmin) || strings.EqualFold(user, "admin") {
		return nil, fmt.Errorf("用户名不可用")
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return nil, err
	}
	k, err := newKey(name)
	if err != nil {
		return nil, err
	}
	k.User = user
	k.PasswordBcrypt = string(hash)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.userTakenLocked(user, "") {
		return nil, fmt.Errorf("用户名已存在")
	}
	s.keys[k.AK] = *k
	if err := s.saveLocked(); err != nil {
		delete(s.keys, k.AK)
		return nil, err
	}
	cp := *k
	return &cp, nil
}

func (s *KeyStore) SetLogin(ak, user, password, reservedAdmin string) error {
	ak = strings.TrimSpace(ak)
	user = strings.TrimSpace(user)
	if ak == "" || user == "" {
		return fmt.Errorf("需要 AK 和用户名")
	}
	if len(password) < 8 {
		return fmt.Errorf("密码至少 8 位")
	}
	if strings.EqualFold(user, reservedAdmin) || strings.EqualFold(user, "admin") {
		return fmt.Errorf("用户名不可用")
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	k, ok := s.keys[ak]
	if !ok {
		return fmt.Errorf("AK 不存在")
	}
	if s.userTakenLocked(user, ak) {
		return fmt.Errorf("用户名已存在")
	}
	k.User = user
	k.PasswordBcrypt = string(hash)
	s.keys[ak] = k
	return s.saveLocked()
}

func (s *KeyStore) PublicList() []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]map[string]any, 0, len(s.keys))
	for _, k := range s.keys {
		out = append(out, map[string]any{
			"ak":         k.AK,
			"name":       k.Name,
			"user":       k.User,
			"has_login":  k.User != "" && k.PasswordBcrypt != "",
			"enabled":    k.Enabled,
			"created_at": k.CreatedAt,
		})
	}
	return out
}

func (s *KeyStore) DefaultAK() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, k := range s.keys {
		if k.Enabled {
			return k.AK
		}
	}
	return ""
}

func ReadBody(r *http.Request) ([]byte, error) {
	if r.Body == nil {
		return nil, nil
	}
	defer r.Body.Close()
	return io.ReadAll(io.LimitReader(r.Body, 1<<20))
}
