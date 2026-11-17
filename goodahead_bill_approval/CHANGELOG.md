# Changelog

## 19.0.1.5.0

- A customer credit note that records a completed, provider-confirmed payment refund (e.g. a card
  refund) is posted without the two approval steps.
- Reached only through a private server-side path that RPC callers cannot trigger.

## 19.0.1.4.1

- The step 1 button is now labelled "Send for Review" on the form, in the dialog and in the list
  action menu. Labels only.

## 19.0.1.4.0

- "Send for Review" opens a dialog to choose an optional approver and gives step 1 in one action,
  also available from the list view for several documents at once.

## 19.0.1.3.1

- The approval exemption for "Reverse and create invoice" now applies only to a full reversal of
  the posted customer invoice; partial reversals stay gated.

## 19.0.1.3.0

- Optional chosen approver per document, and a per-user substitute who may approve on their behalf.
- Customer credit notes follow the same two-step flow, with their own queues under Customers.
- Step 2 queues and counters show only what the current user may approve; new "My Approvals" filter.
- New `_bill_approval_is_held()` for automated callers that leave unapproved documents in draft instead of failing.

## 19.0.1.2.0

- Activity for the reviewer when a bill is fully approved, cleared on posting and removed on refusal.
- New "Bills to Post" queue and a systray counter of documents waiting for the current user.

## 19.0.1.1.0

- Approval fields can no longer be written directly through the ORM; only the approval actions
  may set them.
- A bill held back from auto-posting is offered to auto-posting again once both approvals are given.

## 19.0.1.0.0

- First version: two-step approval of vendor bills, enforced in `_post()`, with per-user approval
  levels, refusal with a reason, and review/approve queues.
