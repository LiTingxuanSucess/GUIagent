package jobs

import (
	"context"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type browserPermitKey struct{}

// Only the global execution permit is yielded during an idle wait. The site
// gate, pool reservation, account lease and original page remain owned by the
// caller. Every subsequent browser operation must reacquire this permit.
type browserPermit struct {
	mu       sync.Mutex
	resumeMu sync.Mutex
	q        *Queue
	timings  *stageTimings
	held     bool
	closed   bool
	done     chan struct{}
}

func browserPermitFrom(ctx context.Context) *browserPermit {
	p, _ := ctx.Value(browserPermitKey{}).(*browserPermit)
	return p
}

func (p *browserPermit) grant() {
	p.mu.Lock()
	p.held = true
	p.done = make(chan struct{})
	p.mu.Unlock()
}

func (p *browserPermit) release() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return
	}
	p.closed = true
	close(p.done)
	if p.held {
		<-p.q.siteSem
		p.held = false
	}
}

func (p *browserPermit) pause() bool {
	// A retained login page still consumes memory while idle. Do not admit
	// extra browser work when this host is already short of physical memory.
	minimum := envInt("GEO_BROWSER_IDLE_MIN_AVAILABLE_MB", 512, 0, 4096)
	if minimum > 0 {
		raw, err := os.ReadFile("/proc/meminfo")
		if err != nil {
			return false
		}
		available := int64(0)
		for _, line := range strings.Split(string(raw), "\n") {
			fields := strings.Fields(line)
			if len(fields) >= 2 && fields[0] == "MemAvailable:" {
				available, _ = strconv.ParseInt(fields[1], 10, 64)
				break
			}
		}
		if available < int64(minimum)*1024 {
			return false
		}
	}
	p.mu.Lock()
	if !p.held || p.closed {
		p.mu.Unlock()
		return false
	}
	<-p.q.siteSem
	p.held = false
	p.mu.Unlock()
	p.q.browserMu.Lock()
	p.q.dispatchBrowsersLocked()
	p.q.browserMu.Unlock()
	p.q.wakeFairResourceWaiters()
	return true
}

func (p *browserPermit) resume(ctx context.Context) bool {
	p.resumeMu.Lock()
	defer p.resumeMu.Unlock()
	p.mu.Lock()
	if p.closed || p.held {
		ok := p.held && !p.closed && ctx.Err() == nil
		p.mu.Unlock()
		return ok
	}
	done := p.done
	p.mu.Unlock()
	started := time.Now()
	defer func() { p.timings.add("recovery_resource_wait_ms", time.Since(started)) }()
	if ctx.Err() != nil {
		return false
	}
	select {
	case p.q.siteSem <- struct{}{}:
		p.mu.Lock()
		if p.closed || ctx.Err() != nil {
			<-p.q.siteSem
			p.mu.Unlock()
			return false
		}
		p.held = true
		p.mu.Unlock()
		return true
	case <-ctx.Done():
		return false
	case <-done:
		return false
	}
}

func withIdleBrowser(ctx context.Context, wait func() error) error {
	p := browserPermitFrom(ctx)
	if p == nil || !p.pause() {
		return wait()
	}
	started := time.Now()
	err := wait()
	p.timings.add("recovery_idle_ms", time.Since(started))
	if !p.resume(ctx) {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return context.Canceled
	}
	return err
}
