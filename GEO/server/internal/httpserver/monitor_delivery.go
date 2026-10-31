package httpserver

import (
	"encoding/json"
	"time"
)

// Optional delivery metadata must not break historical index decoding or carry
// arbitrary state/policy strings into the public metadata contract.
type monitorRetryState string

func (state *monitorRetryState) UnmarshalJSON(raw []byte) error {
	var value string
	if json.Unmarshal(raw, &value) == nil {
		switch value {
		case "retry_wait", "operator_wait", "awaiting_confirmation":
			*state = monitorRetryState(value)
			return nil
		}
	}
	*state = ""
	return nil
}

type monitorDeliveryPolicy string

func (policy *monitorDeliveryPolicy) UnmarshalJSON(raw []byte) error {
	var value string
	if json.Unmarshal(raw, &value) == nil && value == "eventual" {
		*policy = "eventual"
	} else {
		*policy = ""
	}
	return nil
}

type monitorRetryAt string

func (at *monitorRetryAt) UnmarshalJSON(raw []byte) error {
	var value string
	*at = ""
	if json.Unmarshal(raw, &value) == nil {
		if parsed, err := time.Parse(time.RFC3339Nano, value); err == nil && parsed.Unix() > 0 {
			*at = monitorRetryAt(parsed.UTC().Format(time.RFC3339Nano))
		}
	}
	return nil
}
