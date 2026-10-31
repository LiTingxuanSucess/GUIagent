package sessions

import (
	"testing"
	"time"
)

func TestRecoverySMSWaitsForBrowserAdmissionAndCloseDrainsCallback(t *testing.T) {
	p := recoveryLeaseFixture(t)
	m := p.runtimes["captcha"]
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")
	lease := &Lease{Manager: m, siteID: "doubao"}
	guard, err := lease.BeginWebRecovery()
	if err != nil {
		t.Fatal(err)
	}
	defer guard()
	requested, permit := make(chan struct{}), make(chan struct{})
	closeWindow, err := lease.AllowRecoverySMSWithAdmission(func() bool {
		close(requested)
		<-permit
		return true
	})
	if err != nil {
		t.Fatal(err)
	}
	defer closeWindow()
	callback := make(chan func(), 1)
	go func() { release, _ := beginRecoverySMSAction(m, "doubao"); callback <- release }()
	select {
	case <-requested:
	case <-time.After(time.Second):
		t.Fatal("SMS did not request admission")
	}
	select {
	case <-callback:
		t.Fatal("SMS touched the page without admission")
	default:
	}
	close(permit)
	var release func()
	select {
	case release = <-callback:
	case <-time.After(time.Second):
		t.Fatal("admitted SMS did not proceed")
	}
	if release == nil {
		t.Fatal("SMS callback was rejected after admission")
	}
	closed := make(chan struct{})
	go func() { closeWindow(); close(closed) }()
	select {
	case <-closed:
		t.Fatal("close did not wait for SMS browser action")
	default:
	}
	release()
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("SMS close remained blocked")
	}
}

func TestRecoverySMSRejectedAdmissionDoesNotLeaveCallbackLocked(t *testing.T) {
	p := recoveryLeaseFixture(t)
	m := p.runtimes["captcha"]
	m.AcquireSite("doubao")
	defer m.ReleaseSite("doubao")
	lease := &Lease{Manager: m, siteID: "doubao"}
	guard, err := lease.BeginWebRecovery()
	if err != nil {
		t.Fatal(err)
	}
	defer guard()
	closeWindow, err := lease.AllowRecoverySMSWithAdmission(func() bool { return false })
	if err != nil {
		t.Fatal(err)
	}
	if release, recovering := beginRecoverySMSAction(m, "doubao"); !recovering || release != nil {
		t.Fatal("rejected admission performed SMS action")
	}
	closeWindow()
	closeWindow, err = lease.AllowRecoverySMS()
	if err != nil {
		t.Fatal(err)
	}
	defer closeWindow()
	release, _ := beginRecoverySMSAction(m, "doubao")
	if release == nil {
		t.Fatal("previous rejection kept callback locked")
	}
	release()
}
