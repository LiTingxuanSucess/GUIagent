package jobs

import (
	"context"
	"fmt"
	"sync/atomic"
	"testing"
	"time"
)

type changingCapacityPool struct {
	concurrentPool
	count atomic.Int32
}

func (p *changingCapacityPool) LoggedInAccountIDs(string) []string {
	var ids []string
	for i := int32(0); i < p.count.Load(); i++ {
		ids = append(ids, fmt.Sprint(i))
	}
	return ids
}

func TestPoolAdmissionWaitsOutsideBrowserGateAndUsesNewLogin(t *testing.T) {
	p := &changingCapacityPool{}
	p.count.Store(1)
	q := &Queue{Sess: p, siteSem: make(chan struct{}, 2)}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	first, ok := q.acquirePoolCapacity(ctx, "doubao")
	if !ok {
		t.Fatal("first account was not admitted")
	}
	defer first()
	next := make(chan func(), 1)
	go func() { release, _ := q.acquirePoolCapacity(ctx, "doubao"); next <- release }()
	select {
	case <-next:
		t.Fatal("one account admitted overlapping questions")
	case <-time.After(40 * time.Millisecond):
	}
	if len(q.siteSem) != 0 {
		t.Fatal("waiting for an account consumed a global browser slot")
	}
	p.count.Store(2)
	select {
	case second := <-next:
		if second == nil {
			t.Fatal("new login did not join the pool")
		}
		second()
		second() // Releasing twice must not create extra capacity.
	case <-time.After(time.Second):
		t.Fatal("capacity stayed fixed after a second account logged in")
	}
	short, stop := context.WithTimeout(ctx, 20*time.Millisecond)
	defer stop()
	p.count.Store(1)
	if release, ok := q.acquirePoolCapacity(short, "doubao"); ok || release != nil {
		t.Fatal("a wait outlived its deadline")
	}
	other, ok := q.acquirePoolCapacity(ctx, "yuanbao")
	if !ok {
		t.Fatal("busy Doubao blocked another platform")
	}
	other()
}
