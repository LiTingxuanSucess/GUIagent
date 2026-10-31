package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEnvAdminPasswordIsNotReturnedAsBootstrapSecret(t *testing.T) {
	root := t.TempDir()
	t.Setenv("GEO_ADMIN_PASSWORD", "stable-secret-password")
	t.Setenv("GEO_API_KEY", "test-api-key")
	cfg, bootstrap, err := Load(root, filepath.Join(root, "missing.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if bootstrap != "" {
		t.Fatalf("environment password leaked as bootstrap value: %q", bootstrap)
	}
	if !cfg.CheckPassword("stable-secret-password") {
		t.Fatal("environment password does not authenticate")
	}
}

func TestGeneratedPasswordIsReturnedOnceAndStoredSecurely(t *testing.T) {
	root := t.TempDir()
	t.Setenv("GEO_ADMIN_PASSWORD", "")
	t.Setenv("GEO_API_KEY", "test-api-key")
	cfg, bootstrap, err := Load(root, filepath.Join(root, "missing.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if bootstrap == "" || !cfg.CheckPassword(bootstrap) {
		t.Fatal("generated bootstrap password missing or invalid")
	}
	info, err := os.Stat(filepath.Join(root, ".admin-bootstrap"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("bootstrap file mode = %o, want 600", info.Mode().Perm())
	}
}
