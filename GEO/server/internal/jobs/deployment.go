package jobs

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

func (q *Queue) deploymentHolds(id string) bool {
	if q.DataDir == "" {
		return false
	}
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(q.DataDir), ".deployment-drain.json"))
	if err != nil {
		return false
	}
	var drain struct {
		ExpiresAt float64  `json:"expires_at"`
		Allow     []string `json:"allow"`
	}
	if json.Unmarshal(raw, &drain) != nil || drain.ExpiresAt <= float64(time.Now().UnixNano())/1e9 {
		return false
	}
	for _, allowed := range drain.Allow {
		if allowed == "job_"+id {
			return false
		}
	}
	return true
}

// Hold new arrivals before changing them to running, including jobs that
// would otherwise take the API-only path. They remain restartable queued
// records even before a Python process can write its deployment marker.
func (q *Queue) waitForDeployment(id string) bool {
	for q.deploymentHolds(id) {
		job, ok := q.Get(id)
		if !ok || job.Status != "queued" {
			return false
		}
		// Deployment and worker waiting do not consume webpage execution time.
		time.Sleep(100 * time.Millisecond)
	}
	return true
}
