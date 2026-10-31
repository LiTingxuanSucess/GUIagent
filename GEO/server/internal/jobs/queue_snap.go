package jobs

import (
	"encoding/json"
	"sort"
)

// jobSnap is the poll response, built when the job changes. A status read
// only copies these bytes, so it does not wait on the job lock or walk the
// answer while browsers are running.
type jobSnap struct {
	ak         string
	publicBody []byte
	adminBody  []byte
}

func (q *Queue) VisibleBody(id, ak string, admin bool) ([]byte, bool) {
	q.snapMu.RLock()
	snap := q.snaps[id]
	q.snapMu.RUnlock()
	if snap == nil {
		return nil, false
	}
	if !admin && (ak == "" || snap.ak != ak) {
		return nil, false
	}
	if admin {
		return snap.adminBody, true
	}
	return snap.publicBody, true
}

func (q *Queue) publish(id string) {
	q.publishIDs([]string{id}, false)
}

func (q *Queue) publishAll() {
	q.mu.Lock()
	ids := make([]string, 0, len(q.jobs))
	for id := range q.jobs {
		ids = append(ids, id)
	}
	q.mu.Unlock()
	q.publishIDs(ids, true)
}

func (q *Queue) publishIDs(ids []string, onlyListed bool) {
	// Serialize capture through publication. Marshaling happens without mu,
	// and a slower, older capture must never overwrite a newer job snapshot.
	q.snapPublishMu.Lock()
	defer q.snapPublishMu.Unlock()
	q.mu.Lock()
	heads := q.snapshotHeadsLocked(ids, onlyListed)
	q.mu.Unlock()
	snaps := make(map[string]*jobSnap, len(heads))
	var gone []string
	for _, head := range heads {
		if head.missing {
			gone = append(gone, head.id)
			continue
		}
		snap, ok := marshalSnap(head.job)
		if ok {
			snaps[head.id] = snap
		}
	}
	q.snapMu.Lock()
	if q.snaps == nil {
		q.snaps = map[string]*jobSnap{}
	}
	for id, snap := range snaps {
		q.snaps[id] = snap
	}
	for _, id := range gone {
		delete(q.snaps, id)
	}
	q.snapMu.Unlock()
}

type snapHead struct {
	id      string
	missing bool
	job     Job
}

func (q *Queue) snapshotHeadsLocked(ids []string, onlyListed bool) []snapHead {
	// A publication uses one coherent queue view. Scanning and sorting the
	// active jobs per historical snapshot makes startup quadratic in history.
	active := make([]*Job, 0)
	for _, candidate := range q.jobs {
		if candidate.Status == "queued" || candidate.Status == "running" {
			active = append(active, candidate)
		}
	}
	sortJobsByCreated(active)
	positions := make(map[string]int, len(active))
	for i, candidate := range active {
		positions[candidate.ID] = i
	}
	seen := map[string]bool{}
	var heads []snapHead
	consider := append([]string(nil), ids...)
	if !onlyListed {
		for _, candidate := range active {
			consider = append(consider, candidate.ID)
		}
	}
	for _, id := range consider {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		latest := q.jobs[id]
		if latest == nil {
			heads = append(heads, snapHead{id: id, missing: true})
			continue
		}
		head := *latest
		if latest.ManualStates != nil {
			head.ManualStates = copyJob(latest).ManualStates
		}
		head.Results = append([]map[string]any(nil), latest.Results...)
		head.Sites = append([]string(nil), latest.Sites...)
		head.QueueLen = len(active)
		head.QueuePos = 0
		if head.Status == "queued" || head.Status == "running" {
			head.QueuePos = positions[head.ID]
		}
		heads = append(heads, snapHead{id: id, job: head})
	}
	return heads
}

func sortJobsByCreated(active []*Job) {
	sort.SliceStable(active, func(i, k int) bool {
		return active[i].CreatedAt < active[k].CreatedAt
	})
}

func marshalSnap(head Job) (*jobSnap, bool) {
	adminJob := copyJob(&head)
	publicJob := copyJob(&head)
	publicJob.AK = ""
	publicJob.DispatchTracked, publicJob.WebDispatched = false, nil
	stripInternalResults(publicJob)
	adminBody, err := json.Marshal(adminJob)
	if err != nil {
		return nil, false
	}
	publicBody, err := json.Marshal(publicJob)
	if err != nil {
		return nil, false
	}
	adminBody = append(adminBody, '\n')
	publicBody = append(publicBody, '\n')
	return &jobSnap{ak: head.AK, publicBody: publicBody, adminBody: adminBody}, true
}
