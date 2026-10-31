package sessions

import (
	"testing"
	"time"
)

func TestSMSFormSurvivesCountdownOnlyForOriginalFreshGeneration(t *testing.T) {
	at := time.Unix(1000, 0)
	for _, tt := range []struct {
		name            string
		sent            float64
		target, current string
		form, want      bool
	}{
		{"countdown_finished", 900, "original", "original", true, true},
		{"initial_form", 0, "", "original", true, false},
		{"replacement_page", 900, "original", "replacement", true, false},
		{"missing_binding", 900, "", "original", true, false},
		{"expired_generation", 700, "original", "original", true, false},
		{"future_generation", 1001, "original", "original", true, false},
		{"no_code_input", 900, "original", "original", false, false},
	} {
		t.Run(tt.name, func(t *testing.T) {
			if got := smsFormForGeneration(tt.sent, tt.target, tt.current, at, tt.form); got != tt.want {
				t.Fatalf("SMS readiness = %t, want %t", got, tt.want)
			}
		})
	}
}
