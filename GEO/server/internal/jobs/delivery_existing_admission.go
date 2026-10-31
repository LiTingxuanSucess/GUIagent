package jobs

import (
	"context"
	"errors"

	"geo-server/internal/sessions"
)

// Pools implementing this optional interface can check a recovery target
// before consuming execution capacity and claim it without an account wait.
// Legacy pools retain the existing bounded waiting behavior.
type existingRecoveryAdmissionPool interface {
	InspectExistingForRecovery(string, string) sessions.ExistingRecoveryAvailability
	TryAcquireExistingForRecoveryContext(context.Context, string, string) (*sessions.Lease, error)
}

func (q *Queue) acquireExistingDeliveryLease(parent context.Context, pool existingRecoveryPool, fast existingRecoveryAdmissionPool, site, account string) (*sessions.Lease, error) {
	if err := parent.Err(); err != nil {
		return nil, err
	}
	if fast != nil {
		return fast.TryAcquireExistingForRecoveryContext(parent, internalSiteID(site), account)
	}
	leaseCtx, stopLease := sessionWaitContext(parent, internalSiteID(site))
	defer stopLease()
	return pool.AcquireExistingForRecoveryContext(leaseCtx, internalSiteID(site), account)
}

func existingDeliveryLeaseBusy(err error) bool {
	return errors.Is(err, sessions.ErrExistingRecoveryBusy) || errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled)
}
