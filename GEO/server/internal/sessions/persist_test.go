package sessions

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCookiesToRestoreKeepsLiveYuanbaoToken(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	profile := []cookieSnap{{
		Name: "hy_token", Value: "live-token", Domain: ".yuanbao.tencent.com",
		Expires: float64(now.Add(time.Hour).Unix()),
	}}
	stored := []cookieSnap{{
		Name: "hy_token", Value: "stale-token", Domain: ".yuanbao.tencent.com",
		Expires: float64(now.Add(2 * time.Hour).Unix()),
	}, {
		Name: "other", Value: "keep", Domain: ".example.com", Expires: float64(now.Add(time.Hour).Unix()),
	}}
	got := cookiesToRestore(profile, stored, now, nil)
	if len(got) != 1 || got[0].Name != "other" {
		t.Fatalf("restore set = %#v, want only the missing non-yuanbao cookie", got)
	}
}

func TestCookiesToRestoreFillsMissingYuanbaoToken(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	stored := []cookieSnap{{
		Name: "hy_token", Value: "saved-token", Domain: ".yuanbao.tencent.com",
		Expires: float64(now.Add(time.Hour).Unix()),
	}}
	got := cookiesToRestore(nil, stored, now, nil)
	if len(got) != 1 || got[0].Value != "saved-token" {
		t.Fatalf("restore set = %#v, want the stored token when the profile has none", got)
	}
}

func TestCookiesToRestoreReplacesExpiredProfileToken(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	profile := []cookieSnap{{
		Name: "hy_token", Value: "expired-token", Domain: ".yuanbao.tencent.com",
		Expires: float64(now.Add(-time.Hour).Unix()),
	}}
	stored := []cookieSnap{{
		Name: "hy_token", Value: "saved-token", Domain: ".yuanbao.tencent.com",
		Expires: float64(now.Add(time.Hour).Unix()),
	}}
	got := cookiesToRestore(profile, stored, now, nil)
	if len(got) != 1 || got[0].Value != "saved-token" {
		t.Fatalf("restore set = %#v, want the stored token when the profile token is expired", got)
	}
}

func TestCookiesToRestorePreferStoredHyTokenOnce(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	profile := []cookieSnap{{
		Name: "hy_token", Value: "stale-token", Domain: ".tencent.com",
		Expires: float64(now.Add(24 * time.Hour).Unix()),
	}}
	stored := []cookieSnap{{
		Name: "hy_token", Value: "login-token", Domain: ".tencent.com",
		Expires: float64(now.Add(time.Hour).Unix()),
	}}
	without := cookiesToRestore(profile, stored, now, nil)
	if len(without) != 0 {
		t.Fatalf("profile token should win without the one-shot override: %#v", without)
	}
	got := cookiesToRestore(profile, stored, now, map[string]bool{"hy_token": true})
	if len(got) != 1 || got[0].Value != "login-token" {
		t.Fatalf("prefer file should replace the profile token once: %#v", got)
	}
}

func TestForgetPreservedAuthDropsStaleYuanbaoSnapshot(t *testing.T) {
	keep := map[string]bool{"yuanbao.tencent.com": true, "tencent.com": true, "deepseek.com": true}
	fresh := []cookieSnap{{Name: "hy_token", Value: "new-token", Domain: "yuanbao.tencent.com"}}
	forgetPreservedAuth(keep, fresh)
	if keep["yuanbao.tencent.com"] || keep["tencent.com"] {
		t.Fatalf("yuanbao domains should no longer be preserved: %#v", keep)
	}
	if !keep["deepseek.com"] {
		t.Fatal("other sites must stay untouched")
	}
}

func TestForgetPreservedAuthKeepsSnapshotWithoutLiveToken(t *testing.T) {
	keep := map[string]bool{"yuanbao.tencent.com": true, "tencent.com": true}
	fresh := []cookieSnap{{Name: "hy_source", Value: "guest", Domain: "yuanbao.tencent.com"}}
	forgetPreservedAuth(keep, fresh)
	if !keep["yuanbao.tencent.com"] || !keep["tencent.com"] {
		t.Fatalf("snapshot should stay while login is unfinished: %#v", keep)
	}
}

func sha12(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])[:12]
}

// TestRehearseYuanbaoPreferFile runs the real one-shot restore against a
// fixture directory. GEO_REHEARSE_DIR is set by the cutover rehearsal script.
func TestRehearseYuanbaoPreferFile(t *testing.T) {
	dir := os.Getenv("GEO_REHEARSE_DIR")
	if dir == "" {
		t.Skip("GEO_REHEARSE_DIR is set by scripts/rehearse_yuanbao_cutover.py")
	}
	liveRoot := "/root/workspace/GUIAgent/GEO/.chrome-cn-ai-profiles/acct_836d3b506d1b"
	if filepath.Clean(dir) == filepath.Clean(liveRoot) || filepath.Base(dir) == "acct_836d3b506d1b" {
		t.Fatal("refusing to rehearse against the live 176 profile")
	}
	storePath := filepath.Join(dir, ".session-store.json")
	raw, err := os.ReadFile(storePath)
	if err != nil {
		t.Fatal(err)
	}
	var stored sessionStore
	if err := json.Unmarshal(raw, &stored); err != nil {
		t.Fatal(err)
	}
	profileRaw, err := os.ReadFile(filepath.Join(dir, "profile-cookies.json"))
	if err != nil {
		t.Fatal(err)
	}
	var profile []cookieSnap
	if err := json.Unmarshal(profileRaw, &profile); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	without := cookiesToRestore(profile, stored.Cookies, now, nil)
	for _, c := range without {
		if c.Name == "hy_token" {
			t.Fatalf("stale profile hy_token must win before the prefer file is consumed, sha12=%s", sha12(c.Value))
		}
	}
	prefer := consumePreferCookieNames(storePath + ".prefer")
	if !prefer["hy_token"] {
		t.Fatal("prefer file did not name hy_token")
	}
	if _, err := os.Stat(storePath + ".prefer"); !os.IsNotExist(err) {
		t.Fatal("prefer file was not consumed")
	}
	got := cookiesToRestore(profile, stored.Cookies, now, prefer)
	var selected string
	for _, c := range got {
		if c.Name == "hy_token" {
			selected = c.Value
		}
	}
	if selected == "" {
		t.Fatal("prefer restore did not select hy_token")
	}
	again := consumePreferCookieNames(storePath + ".prefer")
	if again != nil {
		t.Fatal("prefer file can be consumed only once")
	}
	out := map[string]any{
		"without_prefer_includes_hy_token": false,
		"prefer_consumed":                  true,
		"selected_hy_token_sha256_12":      sha12(selected),
		"second_consume_empty":             true,
	}
	encoded, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "rehearse-result.json"), encoded, 0o600); err != nil {
		t.Fatal(err)
	}
}
