package sessions

import (
	"errors"
	"testing"
)

func TestCallbackContextIsReadOnlyAndRejectsReplacementTargetOrGeneration(t *testing.T) {
	for _, cause := range []string{"generation", "target", "missing_generation"} {
		t.Run(cause, func(t *testing.T) {
			p, m, tab := attachedExistingRecoveryFixture(t)
			m.states["doubao"].LastSendAt = 123.5
			m.states["doubao"].LastSendTargetID = "original-target"
			current := p.SMSContext("healthy", "doubao")
			if current.SentAt != 123.5 || current.TargetID != "original-target" || m.Busy() {
				t.Fatalf("context=%+v", current)
			}
			switch cause {
			case "generation":
				current.SentAt = 122.5
			case "target":
				current.TargetID = "replacement-target"
			case "missing_generation":
				current.SentAt = 0
			}
			_, err := p.SubmitCallbackCode("healthy", "doubao", "123456", current)
			if !errors.Is(err, ErrSMSGenerationChanged) {
				t.Fatalf("unsafe callback allowed: %v", err)
			}
			if m.tabs["doubao"] != tab || m.opened["doubao"] || m.Busy() {
				t.Fatal("rejected callback mutated or retained page")
			}
		})
	}
}

func TestCallbackWithoutExistingBrowserNeverStartsOne(t *testing.T) {
	p := recoveryLeaseFixture(t)
	delete(p.runtimes, "healthy")
	_, err := p.SubmitCallbackCode("healthy", "doubao", "123456", SMSCallbackContext{SentAt: 123, TargetID: "original-target"})
	if !errors.Is(err, ErrExistingRecoveryMissing) || p.runtimes["healthy"] != nil {
		t.Fatalf("browser created: %v", err)
	}
}
