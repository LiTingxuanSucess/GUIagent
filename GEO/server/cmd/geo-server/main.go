package main

import (
	"context"
	"crypto/tls"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"geo-server/internal/auth"
	"geo-server/internal/config"
	"geo-server/internal/httpserver"
	"geo-server/internal/jobs"
	"geo-server/internal/results"
	"geo-server/internal/sessions"
	"geo-server/internal/sms"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("geo-server ")

	root := findRoot()
	cfgPath := os.Getenv("GEO_CONFIG")
	if cfgPath == "" {
		cfgPath = filepath.Join(root, "config.local.yaml")
	}
	cfg, bootstrapPW, err := config.Load(root, cfgPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	if err := os.MkdirAll(cfg.ExportsDir, 0o755); err != nil {
		log.Fatalf("exports: %v", err)
	}
	if err := httpserver.EnsureCert(cfg.PublicIP, cfg.TLS.Cert, cfg.TLS.Key); err != nil {
		log.Fatalf("tls: %v", err)
	}
	certificates, err := httpserver.NewReloadingCertificate(cfg.TLS.Cert, cfg.TLS.Key)
	if err != nil {
		log.Fatalf("tls: %v", err)
	}

	log.Printf("start %s", cfg.String())
	if bootstrapPW != "" {
		log.Printf("admin user=%s bootstrap credential saved in %s", cfg.Admin.User, filepath.Join(root, ".admin-bootstrap"))
	} else {
		log.Printf("admin user=%s (password from config/env)", cfg.Admin.User)
	}
	log.Printf("legacy api key ready (header X-API-Key, 只读结果接口)")

	authStore := auth.New(cfg)
	keys, created, err := auth.LoadKeys(filepath.Join(root, ".aksk.json"))
	if err != nil {
		log.Fatalf("aksk: %v", err)
	}
	if created != nil {
		log.Printf("created AK/SK pair ak=%s  (sk 只写在 %s ，不会再打印)", created.AK, filepath.Join(root, ".aksk.json"))
	} else {
		log.Printf("aksk loaded ak=%s", keys.DefaultAK())
	}
	dataDir := filepath.Join(root, "data")
	if v := strings.TrimSpace(os.Getenv("GEO_DATA_DIR")); v != "" {
		dataDir = v
	}
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		log.Fatalf("data dir: %v", err)
	}
	resStore := &results.Store{ExportsDir: cfg.ExportsDir, QuestionsPath: cfg.QuestionsPath}
	sess := sessions.NewPool(
		cfg.ChromePath,
		cfg.ProfileDir,
		cfg.Phone,
		filepath.Join(dataDir, "accounts.json"),
	)
	geoRoot := filepath.Clean(filepath.Join(root, ".."))
	queue := jobs.New(geoRoot, sess, keys, filepath.Join(dataDir, "jobs"))
	sess.SetLoginChallengeAssist(queue.AssistLoginChallenge)
	inbox := sms.New(filepath.Join(dataDir, "sms.jsonl"))
	if skipBrowserProcess() {
		log.Printf("GEO_SKIP_BROWSER=1, browser start skipped")
	} else {
		go func() {
			if err := sess.Start(); err != nil {
				log.Printf("browser start failed: %v", err)
			} else {
				log.Printf("account pool ready")
			}
		}()
	}

	webDir := filepath.Join(root, "web")
	if _, err := os.Stat(webDir); err != nil {
		log.Fatalf("web dir: %v", err)
	}
	log.Printf("sms inbound http://%s:8080/api/sms/inbound (token configured, not logged)", cfg.PublicIP)
	srv := httpserver.New(cfg, authStore, keys, resStore, sess, queue, inbox, os.DirFS(webDir))
	handler := srv.Handler()
	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12, GetCertificate: certificates.GetCertificate}
	var servers []*http.Server
	if addr := strings.TrimSpace(os.Getenv("GEO_HTTP_ADDR")); addr != "" {
		s := &http.Server{Addr: addr, Handler: handler, ReadHeaderTimeout: 5 * time.Second}
		servers = append(servers, s)
		go func(srv *http.Server) {
			log.Printf("test http://%s", srv.Addr)
			if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				log.Printf("http %s: %v", srv.Addr, err)
			}
		}(s)
	} else {
		for _, addr := range []string{cfg.Listen, ":8443"} {
			s := &http.Server{
				Addr:              addr,
				Handler:           handler,
				ReadHeaderTimeout: 10 * time.Second,
				ReadTimeout:       60 * time.Second,
				WriteTimeout:      120 * time.Second,
				IdleTimeout:       90 * time.Second,
				TLSNextProto:      map[string]func(*http.Server, *tls.Conn, http.Handler){},
				TLSConfig:         tlsCfg,
			}
			servers = append(servers, s)
			go func(srv *http.Server) {
				if err := srv.ListenAndServeTLS("", ""); err != nil && err != http.ErrServerClosed {
					log.Printf("https %s: %v", srv.Addr, err)
				}
			}(s)
		}
		for _, addr := range []string{":80", ":8080"} {
			s := &http.Server{Addr: addr, Handler: handler, ReadHeaderTimeout: 5 * time.Second}
			servers = append(servers, s)
			go func(srv *http.Server) {
				if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
					log.Printf("http %s: %v", srv.Addr, err)
				}
			}(s)
		}
		log.Printf("public http://%s:8080/admin  https://%s:8443/admin", cfg.PublicIP, cfg.PublicIP)
	}
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	sig := <-ch
	log.Printf("signal %s, flush session and stop chrome", sig)
	sess.Stop()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	for _, s := range servers {
		_ = s.Shutdown(ctx)
	}
}

func findRoot() string {
	if v := os.Getenv("GEO_SERVER_ROOT"); v != "" {
		return v
	}
	exe, err := os.Executable()
	if err == nil {
		dir := filepath.Dir(exe)
		if fileExists(filepath.Join(dir, "config.example.yaml")) {
			return dir
		}
		if fileExists(filepath.Join(dir, "..", "..", "config.example.yaml")) {
			return filepath.Clean(filepath.Join(dir, "..", ".."))
		}
	}
	wd, _ := os.Getwd()
	if fileExists(filepath.Join(wd, "config.example.yaml")) {
		return wd
	}
	if fileExists(filepath.Join(wd, "server", "config.example.yaml")) {
		return filepath.Join(wd, "server")
	}
	return wd
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}

func skipBrowserProcess() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("GEO_SKIP_BROWSER"))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}
