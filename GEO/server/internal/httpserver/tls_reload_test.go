package httpserver

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func certificateFixture(t *testing.T, serial int64) ([]byte, []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(serial), NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
	}
	cert, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	private, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert}), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: private})
}

func writeCertificateFixture(t *testing.T, path string, data []byte, version int64) {
	t.Helper()
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	stamp := time.Unix(1700000000+version, 0)
	if err := os.Chtimes(path, stamp, stamp); err != nil {
		t.Fatal(err)
	}
}

func TestReloadingCertificateKeepsCompletePairDuringRenewal(t *testing.T) {
	dir := t.TempDir()
	certPath, keyPath := filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	firstCert, firstKey := certificateFixture(t, 1)
	secondCert, secondKey := certificateFixture(t, 2)
	writeCertificateFixture(t, certPath, firstCert, 1)
	writeCertificateFixture(t, keyPath, firstKey, 1)
	loader, err := NewReloadingCertificate(certPath, keyPath)
	if err != nil {
		t.Fatal(err)
	}
	first, err := loader.GetCertificate(nil)
	if err != nil || first.Leaf == nil || first.Leaf.SerialNumber.Int64() != 1 {
		t.Fatalf("initial pair was not loaded: %v %v", first, err)
	}
	cached, err := loader.GetCertificate(nil)
	if err != nil || cached != first {
		t.Fatal("unchanged certificate files were not cached")
	}
	// Certbot/deploy hooks need not replace both files in the same syscall.
	writeCertificateFixture(t, certPath, secondCert, 2)
	mismatched, err := loader.GetCertificate(nil)
	if err != nil || mismatched != first {
		t.Fatal("an incomplete renewal discarded the previous matching pair")
	}
	writeCertificateFixture(t, keyPath, secondKey, 2)
	second, err := loader.GetCertificate(nil)
	if err != nil || second == first || second.Leaf == nil || second.Leaf.SerialNumber.Int64() != 2 {
		t.Fatal("the completed renewal did not replace the certificate")
	}
	if first.Leaf.SerialNumber.Int64() != 1 {
		t.Fatal("reloading mutated a certificate used by an earlier handshake")
	}
	writeCertificateFixture(t, certPath, []byte("incomplete PEM"), 3)
	invalid, err := loader.GetCertificate(nil)
	if err != nil || invalid != second {
		t.Fatal("a transient invalid certificate interrupted existing TLS")
	}
	if err := os.Remove(keyPath); err != nil {
		t.Fatal(err)
	}
	missing, err := loader.GetCertificate(nil)
	if err != nil || missing != second {
		t.Fatal("a transient missing file interrupted existing TLS")
	}
	writeCertificateFixture(t, certPath, firstCert, 4)
	writeCertificateFixture(t, keyPath, firstKey, 4)
	restored, err := loader.GetCertificate(nil)
	if err != nil || restored.Leaf.SerialNumber.Int64() != 1 {
		t.Fatal("reload did not recover after an invalid/missing-file interval")
	}
}

func TestReloadingCertificateRejectsInvalidInitialPair(t *testing.T) {
	dir := t.TempDir()
	certPath, keyPath := filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	if _, err := NewReloadingCertificate(certPath, keyPath); err == nil {
		t.Fatal("a missing initial certificate was accepted")
	}
	cert, _ := certificateFixture(t, 1)
	_, mismatchedKey := certificateFixture(t, 2)
	writeCertificateFixture(t, certPath, cert, 1)
	writeCertificateFixture(t, keyPath, mismatchedKey, 1)
	if _, err := NewReloadingCertificate(certPath, keyPath); err == nil {
		t.Fatal("a mismatched initial certificate/key pair was accepted")
	}
}

func TestServeTLSReloadsCertificateWithoutRestart(t *testing.T) {
	dir := t.TempDir()
	certPath, keyPath := filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	firstCert, firstKey := certificateFixture(t, 11)
	secondCert, secondKey := certificateFixture(t, 22)
	writeCertificateFixture(t, certPath, firstCert, 1)
	writeCertificateFixture(t, keyPath, firstKey, 1)
	loader, err := NewReloadingCertificate(certPath, keyPath)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12, GetCertificate: loader.GetCertificate}}
	defer server.Close()
	go func() { _ = server.ServeTLS(listener, "", "") }()
	serial := func() int64 {
		t.Helper()
		conn, err := tls.DialWithDialer(&net.Dialer{Timeout: time.Second}, "tcp", listener.Addr().String(), &tls.Config{InsecureSkipVerify: true})
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		return conn.ConnectionState().PeerCertificates[0].SerialNumber.Int64()
	}
	if serial() != 11 {
		t.Fatal("TLS listener did not use its dynamic certificate callback")
	}
	writeCertificateFixture(t, certPath, secondCert, 2)
	writeCertificateFixture(t, keyPath, secondKey, 2)
	if serial() != 22 {
		t.Fatal("TLS renewal required restarting the listener")
	}
}
