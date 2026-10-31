package config

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"golang.org/x/crypto/bcrypt"
	"gopkg.in/yaml.v3"
)

type Config struct {
	Listen        string `yaml:"listen"`
	PublicIP      string `yaml:"public_ip"`
	Phone         string `yaml:"phone"`
	APIKey        string `yaml:"api_key"`
	ChromePath    string `yaml:"chrome_path"`
	ProfileDir    string `yaml:"profile_dir"`
	ExportsDir    string `yaml:"exports_dir"`
	QuestionsPath string `yaml:"questions_path"`
	TLS           struct {
		Cert string `yaml:"cert"`
		Key  string `yaml:"key"`
	} `yaml:"tls"`
	Admin struct {
		User           string `yaml:"user"`
		PasswordBcrypt string `yaml:"password_bcrypt"`
	} `yaml:"admin"`
	SMSToken string `yaml:"sms_token"`

	Root string `yaml:"-"`
}

func Default(root string) *Config {
	c := &Config{Root: root}
	c.Listen = "0.0.0.0:443"
	c.PublicIP = "127.0.0.1"
	c.Phone = ""
	c.Admin.User = "admin"
	c.TLS.Cert = filepath.Join(root, "certs", "cert.pem")
	c.TLS.Key = filepath.Join(root, "certs", "key.pem")
	c.ProfileDir = filepath.Clean(filepath.Join(root, "..", ".chrome-cn-ai-profile"))
	c.ExportsDir = filepath.Clean(filepath.Join(root, "..", "exports"))
	c.QuestionsPath = filepath.Clean(filepath.Join(root, "..", "questions_cn.json"))
	c.ChromePath = defaultChrome()
	return c
}

func defaultChrome() string {
	candidates := []string{
		os.Getenv("CHROME_PATH"),
		"/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
		"/usr/bin/google-chrome",
		"/usr/bin/chromium",
		"/usr/bin/chromium-browser",
	}
	if runtime.GOOS == "darwin" {
		candidates = append([]string{os.Getenv("CHROME_PATH"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}, candidates...)
	}
	for _, p := range candidates {
		if p == "" {
			continue
		}
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return ""
}

func Load(root, path string) (*Config, string, error) {
	c := Default(root)
	if path != "" {
		raw, err := os.ReadFile(path)
		if err != nil && !os.IsNotExist(err) {
			return nil, "", err
		}
		if err == nil {
			if err := yaml.Unmarshal(raw, c); err != nil {
				return nil, "", err
			}
		}
	}
	c.Root = root
	if v := os.Getenv("GEO_LISTEN"); v != "" {
		c.Listen = v
	}
	if v := os.Getenv("GEO_PUBLIC_IP"); v != "" {
		c.PublicIP = v
	}
	if v := os.Getenv("CN_AI_PHONE"); v != "" {
		c.Phone = v
	}
	if v := os.Getenv("GEO_API_KEY"); v != "" {
		c.APIKey = v
	}
	if v := os.Getenv("CHROME_PATH"); v != "" {
		c.ChromePath = v
	}
	if v := os.Getenv("GEO_ADMIN_USER"); v != "" {
		c.Admin.User = v
	}
	if v := os.Getenv("GEO_SMS_TOKEN"); v != "" {
		c.SMSToken = v
	}
	c.resolveRel()
	plain, err := c.ensureSecrets()
	if err != nil {
		return nil, "", err
	}
	return c, plain, nil
}

func (c *Config) resolveRel() {
	abs := func(p string) string {
		if p == "" || filepath.IsAbs(p) {
			return p
		}
		return filepath.Clean(filepath.Join(c.Root, p))
	}
	c.TLS.Cert = abs(c.TLS.Cert)
	c.TLS.Key = abs(c.TLS.Key)
	c.ProfileDir = abs(c.ProfileDir)
	c.ExportsDir = abs(c.ExportsDir)
	c.QuestionsPath = abs(c.QuestionsPath)
}

func (c *Config) ensureSecrets() (string, error) {
	plain := os.Getenv("GEO_ADMIN_PASSWORD")
	bootstrapPlain := ""
	if c.Admin.PasswordBcrypt == "" {
		if plain == "" {
			b := make([]byte, 9)
			if _, err := rand.Read(b); err != nil {
				return "", err
			}
			plain = hex.EncodeToString(b)
			bootstrapPlain = plain
			boot := filepath.Join(c.Root, ".admin-bootstrap")
			_ = os.WriteFile(boot, []byte("user="+c.Admin.User+"\npassword="+plain+"\n"), 0o600)
		}
		hash, err := bcrypt.GenerateFromPassword([]byte(plain), bcrypt.DefaultCost)
		if err != nil {
			return "", err
		}
		c.Admin.PasswordBcrypt = string(hash)
	}
	if c.APIKey == "" {
		if v := os.Getenv("GEO_API_KEY"); v != "" {
			c.APIKey = v
		} else {
			b := make([]byte, 16)
			if _, err := rand.Read(b); err != nil {
				return "", err
			}
			c.APIKey = hex.EncodeToString(b)
			_ = os.WriteFile(filepath.Join(c.Root, ".api-key"), []byte(c.APIKey+"\n"), 0o600)
		}
	}
	if c.Admin.User == "" {
		c.Admin.User = "admin"
	}
	if c.SMSToken == "" {
		p := filepath.Join(c.Root, ".sms-token")
		if raw, err := os.ReadFile(p); err == nil {
			c.SMSToken = strings.TrimSpace(string(raw))
		}
		if c.SMSToken == "" {
			b := make([]byte, 16)
			if _, err := rand.Read(b); err != nil {
				return "", err
			}
			c.SMSToken = hex.EncodeToString(b)
			_ = os.WriteFile(p, []byte(c.SMSToken+"\n"), 0o600)
		}
	}
	return bootstrapPlain, nil
}

func (c *Config) CheckPassword(pw string) bool {
	if c.Admin.PasswordBcrypt == "" {
		return false
	}
	return bcrypt.CompareHashAndPassword([]byte(c.Admin.PasswordBcrypt), []byte(pw)) == nil
}

func RandomToken() (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func (c *Config) String() string {
	return fmt.Sprintf("listen=%s public_ip=%s chrome=%s profile=%s", c.Listen, c.PublicIP, c.ChromePath, c.ProfileDir)
}

func NormalizePhone(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}
