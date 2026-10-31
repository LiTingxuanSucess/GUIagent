package jobs

import "log"

func apiFallbackLog(site, webCode, apiCode string) {
	log.Printf("api fallback failed site=%s web_error_code=%s api_error_code=%s", site, webCode, apiCode)
}

func apiFallbackSuccessLog(site, webCode, model string, elapsedMS int64) {
	log.Printf("api fallback succeeded site=%s web_error_code=%s model=%s api_elapsed_ms=%d", site, webCode, model, elapsedMS)
}
