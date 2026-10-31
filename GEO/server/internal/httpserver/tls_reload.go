package httpserver

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"sync"
)

// ReloadingCertificate keeps the last complete certificate/key pair available
// while a renewal replaces the two files. Certificates returned to a TLS
// handshake are immutable and remain usable after a later reload.
type ReloadingCertificate struct {
	mu                sync.Mutex
	certPath, keyPath string
	certificate       *tls.Certificate
	certInfo, keyInfo os.FileInfo
}

func NewReloadingCertificate(certPath, keyPath string) (*ReloadingCertificate, error) {
	loader := &ReloadingCertificate{certPath: certPath, keyPath: keyPath}
	if _, err := loader.GetCertificate(nil); err != nil {
		return nil, err
	}
	return loader, nil
}

func (c *ReloadingCertificate) GetCertificate(_ *tls.ClientHelloInfo) (*tls.Certificate, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	certInfo, certErr := os.Stat(c.certPath)
	keyInfo, keyErr := os.Stat(c.keyPath)
	if certErr != nil || keyErr != nil {
		if c.certificate != nil {
			return c.certificate, nil
		}
		if certErr != nil {
			return nil, fmt.Errorf("TLS certificate file: %w", certErr)
		}
		return nil, fmt.Errorf("TLS key file: %w", keyErr)
	}
	if c.certificate != nil && sameCertificateFile(c.certInfo, certInfo) && sameCertificateFile(c.keyInfo, keyInfo) {
		return c.certificate, nil
	}
	pair, err := tls.LoadX509KeyPair(c.certPath, c.keyPath)
	if err == nil {
		// A rename or symlink update during the read must be retried on the
		// next handshake instead of caching the wrong file fingerprints.
		afterCert, afterCertErr := os.Stat(c.certPath)
		afterKey, afterKeyErr := os.Stat(c.keyPath)
		if afterCertErr != nil || afterKeyErr != nil || !sameCertificateFile(certInfo, afterCert) || !sameCertificateFile(keyInfo, afterKey) {
			err = fmt.Errorf("TLS certificate files changed during reload")
		}
	}
	if err == nil {
		pair.Leaf, err = x509.ParseCertificate(pair.Certificate[0])
	}
	if err != nil {
		if c.certificate != nil {
			return c.certificate, nil
		}
		return nil, fmt.Errorf("load TLS certificate pair: %w", err)
	}
	c.certificate = &pair
	c.certInfo, c.keyInfo = certInfo, keyInfo
	return c.certificate, nil
}

func sameCertificateFile(before, after os.FileInfo) bool {
	return before != nil && after != nil && before.Size() == after.Size() && before.ModTime().Equal(after.ModTime()) && os.SameFile(before, after)
}
