# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Two-step approval of vendor bills and customer credit notes, enforced before posting.

The gate is `_post()`, not a button: core posts some bills by itself through
`_autopost_bill()` (attachment upload, Peppol, OCR). `_post()` raises on every route, while
`_autopost_bill()` returns quietly so automated imports leave the bill in draft instead of
raising at the user. `env.su` is deliberately not a bypass.

Moves posted before installation read 'to_review' (the column default). That is harmless while
they stay posted, but one reset to draft needs both approvals to be posted again.
"""

import logging

from markupsafe import Markup

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.mail import plaintext2html
from odoo.tools.sql import column_exists

_logger = logging.getLogger(__name__)

# The two approval levels, named by step rather than by job title.
GROUP_1 = 'goodahead_bill_approval.group_bill_approval_1'
GROUP_2 = 'goodahead_bill_approval.group_bill_approval_2'

# Move types in scope. Vendor documents and customer credit notes are separate families
# because each has its own menus, list and search views.
VENDOR_TYPES = ('in_invoice', 'in_refund')
CUSTOMER_TYPES = ('out_refund',)
APPROVED_TYPES = VENDOR_TYPES + CUSTOMER_TYPES

# The user chosen to give step 2 on one document.
APPROVER_FIELD = 'bill_approval_approver_id'

# Activity raised on the chosen approver while the document waits for them.
APPROVE_ACTIVITY_TYPE_XMLID = 'goodahead_bill_approval.mail_activity_type_bill_to_approve'

# Fields whose change can start or end "waiting for the chosen approver".
TASK_TRIGGER_FIELDS = frozenset({
    APPROVER_FIELD, 'approval_1_uid', 'approval_2_uid', 'state', 'move_type',
})

# `_reverse_moves(cancel=True)` creates and posts the credit note in one call, so that route is
# exempt from the gate. The token is an instance of a private class rather than a plain context
# flag: an RPC context is decoded from JSON and can never carry it, so it cannot be forged.
CANCEL_REVERSAL_KEY = 'goodahead_bill_approval_cancel_reversal'


class _CancelReversal(tuple):
    """The ids of the posted documents a `cancel=True` reversal is cancelling."""
    __slots__ = ()


# Token for posting the credit note of a completed payment refund without approval. Set only by
# the private `_bill_approval_post_payment_refunds`; the move's data is checked as well
# (`_bill_approval_is_payment_refund`), since transaction data alone could be forged.
PAYMENT_REFUND_KEY = 'goodahead_bill_approval_payment_refund'


class _PaymentRefund(tuple):
    """The ids of the credit notes a done payment refund may post without approval."""
    __slots__ = ()


# Fields only this module's actions may write. `readonly=True` is a client-side modifier, so
# without `_bill_approval_guard_fields` anyone able to edit a bill could forge an approval.
APPROVAL_WRITE_FIELDS = (
    'approval_1_uid', 'approval_1_date',
    'approval_2_uid', 'approval_2_date',
    'approval_2_on_behalf_uid',
)

# Activity raised on the reviewer once step 2 is given.
ACTIVITY_TYPE_XMLID = 'goodahead_bill_approval.mail_activity_type_bill_to_post'

# ---------------------------------------------------------------------------- the queues
#
# Each queue domain is complete on its own (approval state, draft state, move type) because it
# is read both by a search view filter and by the systray count, and the two must agree.
# `tests/test_systray.py` compares these domains with the XML filters.
def _draft_scope(move_types):
    return [
        ('state', '=', 'draft'),
        ('move_type', 'in', list(move_types)),
    ]


BILL_DRAFT_SCOPE = _draft_scope(VENDOR_TYPES)
CREDIT_NOTE_DRAFT_SCOPE = _draft_scope(CUSTOMER_TYPES)

# The state half of each queue; each family adds its own `_draft_scope`.
TO_REVIEW_CLAUSES = [('bill_approval_state', '=', 'to_review')]
TO_APPROVE_CLAUSES = [('bill_approval_state', '=', 'reviewed')]

QUEUE_TO_REVIEW_DOMAIN = TO_REVIEW_CLAUSES + BILL_DRAFT_SCOPE


def mine_to_approve_clauses(uid):
    """The step 2 documents user `uid` may approve: no chosen approver, or `uid` is the
    chosen approver or their substitute (one hop only)."""
    return [
        '|', '|',
        (APPROVER_FIELD, '=', False),
        (APPROVER_FIELD, '=', uid),
        (APPROVER_FIELD + '.bill_approval_substitute_id', '=', uid),
    ]


def queue_to_approve_domain(uid):
    return TO_APPROVE_CLAUSES + BILL_DRAFT_SCOPE + mine_to_approve_clauses(uid)


def resolve_queue_domain(domain, uid):
    """A queue domain as a list, for the reader `uid`. The step 2 domains are functions."""
    return domain(uid) if callable(domain) else domain


# Bills set to post themselves on their accounting date (by the user, or by `_post(soft=True)`
# for future-dated moves) are not waiting for anyone, so they are left out of the to-post queue.
TO_POST_CLAUSES = [
    ('bill_approval_state', '=', 'approved'),
    ('auto_post', '=', 'no'),
]
BILL_QUEUE_TO_POST_DOMAIN = TO_POST_CLAUSES + BILL_DRAFT_SCOPE

# All families together: the SQL twin of `_bill_approval_is_waiting_to_post()`.
QUEUE_TO_POST_DOMAIN = TO_POST_CLAUSES + _draft_scope(APPROVED_TYPES)

CREDIT_NOTE_QUEUE_TO_REVIEW_DOMAIN = TO_REVIEW_CLAUSES + CREDIT_NOTE_DRAFT_SCOPE


def credit_note_queue_to_approve_domain(uid):
    return TO_APPROVE_CLAUSES + CREDIT_NOTE_DRAFT_SCOPE + mine_to_approve_clauses(uid)


CREDIT_NOTE_QUEUE_TO_POST_DOMAIN = TO_POST_CLAUSES + CREDIT_NOTE_DRAFT_SCOPE

# (key, group xmlid, search-view filter name, action xmlid, domain), in workflow order.
# One row per family, so each badge count equals the list it opens. Filter names must exist in
# the search view: the web client silently drops unknown `search_default_*` keys.
BILL_APPROVAL_QUEUES = (
    ('to_review', GROUP_1, 'bill_awaiting_review',
     'goodahead_bill_approval.action_bills_awaiting_review', QUEUE_TO_REVIEW_DOMAIN),
    ('to_approve', GROUP_2, 'bill_awaiting_approval',
     'goodahead_bill_approval.action_bills_awaiting_approval', queue_to_approve_domain),
    ('to_post', GROUP_1, 'bill_approved_to_post',
     'goodahead_bill_approval.action_bills_to_post', BILL_QUEUE_TO_POST_DOMAIN),
    ('cn_to_review', GROUP_1, 'credit_note_awaiting_review',
     'goodahead_bill_approval.action_credit_notes_awaiting_review',
     CREDIT_NOTE_QUEUE_TO_REVIEW_DOMAIN),
    ('cn_to_approve', GROUP_2, 'credit_note_awaiting_approval',
     'goodahead_bill_approval.action_credit_notes_awaiting_approval',
     credit_note_queue_to_approve_domain),
    ('cn_to_post', GROUP_1, 'credit_note_approved_to_post',
     'goodahead_bill_approval.action_credit_notes_to_post',
     CREDIT_NOTE_QUEUE_TO_POST_DOMAIN),
)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _auto_init(self):
        # Create the column with its default in SQL so the ORM does not recompute the stored
        # field on every existing move (same approach as enterprise `account_3way_match`).
        if not column_exists(self.env.cr, 'account_move', 'bill_approval_state'):
            self.env.cr.execute(
                "ALTER TABLE account_move "
                "ADD COLUMN bill_approval_state VARCHAR DEFAULT 'to_review'"
            )
        return super()._auto_init()

    # ------------------------------------------------------------------ fields

    approval_1_uid = fields.Many2one(
        'res.users',
        string='Reviewed by',
        readonly=True,
        copy=False,
        tracking=True,
        index='btree_not_null',
        help="The user who gave approval step 1: the figures on this bill are right.",
    )
    approval_1_date = fields.Datetime(
        string='Reviewed on',
        readonly=True,
        copy=False,
        tracking=True,
    )
    approval_2_uid = fields.Many2one(
        'res.users',
        string='Approved by',
        readonly=True,
        copy=False,
        tracking=True,
        index='btree_not_null',
        help="The user who gave approval step 2: we agree to pay this bill.",
    )
    approval_2_date = fields.Datetime(
        string='Approved on',
        readonly=True,
        copy=False,
        tracking=True,
    )

    # Optional: when set, only this user or their substitute may give step 2. Not readonly, as
    # it is a choice made on the form; guarded by `_bill_approval_guard_approver` and validated
    # by `_check_bill_approval_approver`.
    bill_approval_approver_id = fields.Many2one(
        'res.users',
        string='Approver (step 2)',
        copy=False,
        tracking=True,
        index='btree_not_null',
        domain=lambda self: [
            ('groups_id', 'in', self.env.ref(GROUP_2).ids),
            ('share', '=', False),
        ],
        help="The one person who may give approval step 2 on this document — or their "
             "substitute, set on their own user preferences. Leave empty to let anyone "
             "holding \"Bills: may approve (step 2)\" approve it.",
    )
    # Set only when step 2 was given by the chosen approver's substitute; names the approver.
    approval_2_on_behalf_uid = fields.Many2one(
        'res.users',
        string='Approved on behalf of',
        readonly=True,
        copy=False,
        tracking=True,
        help="Step 2 was given by the chosen approver's substitute, on behalf of this user.",
    )
    # Whether the current user may change the chosen approver; drives the form's readonly.
    bill_approval_user_may_set_approver = fields.Boolean(
        compute='_compute_bill_approval_user_may_set_approver',
    )

    # Stored and indexed: the queues filter and count on it.
    bill_approval_state = fields.Selection(
        selection=[
            ('to_review', 'Waiting for Review'),
            ('reviewed', 'Waiting for Approval'),
            ('approved', 'Approved'),
        ],
        string='Approval',
        compute='_compute_bill_approval_state',
        store=True,
        index=True,
        readonly=True,
        help="Where this bill stands in the two-step approval: waiting for a review "
             "(step 1), waiting for an approval (step 2), or approved and postable.",
    )

    @api.depends('approval_1_uid', 'approval_2_uid')
    def _compute_bill_approval_state(self):
        # Not scoped by move type: this states which approvals the record carries. Whether
        # they are required is `_bill_approval_required()`.
        for move in self:
            if move.approval_1_uid and move.approval_2_uid:
                move.bill_approval_state = 'approved'
            elif move.approval_1_uid:
                move.bill_approval_state = 'reviewed'
            else:
                move.bill_approval_state = 'to_review'

    @api.depends_context('uid')
    def _compute_bill_approval_user_may_set_approver(self):
        may = self.env.user._bill_approval_holds_a_level()
        for move in self:
            move.bill_approval_user_may_set_approver = may

    @api.constrains(APPROVER_FIELD)
    def _check_bill_approval_approver(self):
        """The chosen approver must be an active internal user holding step 2.

        A constraint rather than only the field domain, which RPC and imports bypass.
        """
        for move in self:
            approver = move.bill_approval_approver_id.sudo()
            if not approver:
                continue
            if approver.active and approver._is_internal() and approver._has_group(GROUP_2):
                continue
            raise ValidationError(self.env._(
                "%(user)s cannot be chosen as the approver of %(move)s: the chosen approver "
                "must be an active internal user holding \"Bills: may approve (step 2)\".",
                user=approver.display_name,
                move=move.display_name,
            ))

    # ------------------------------------------------------------------- scope

    def _bill_approval_required(self):
        """True when this move may not be posted without both approvals."""
        self.ensure_one()
        return self.move_type in APPROVED_TYPES

    def _bill_approval_is_approved(self):
        """Both approvals present, read from the guarded uid fields rather than the derived
        `bill_approval_state`."""
        self.ensure_one()
        return bool(self.approval_1_uid and self.approval_2_uid)

    def _bill_approval_is_held(self):
        """True when posting this move now would be refused by the gate.

        For automated callers that would rather leave unapproved moves in draft than fail.
        """
        self.ensure_one()
        return self._bill_approval_required() and not self._bill_approval_is_approved()

    def _bill_approval_is_waiting_to_post(self):
        """True when this document is approved and still draft, waiting to be posted.

        Python twin of `QUEUE_TO_POST_DOMAIN`; the tests check that both agree.
        """
        self.ensure_one()
        return bool(
            self._bill_approval_required()
            and self.state == 'draft'
            and self.auto_post == 'no'
            and self._bill_approval_is_approved()
        )

    # ------------------------------------------------- the ORM-level guard

    def _bill_approval_guard_fields(self, vals):
        """Refuse any write of an approval field outside this module's own actions.

        The legitimate writers check the user's level and then write with `sudo()`. `env.su` is
        the authorisation because, unlike a context key, it cannot be set over RPC. This is not
        a separation-of-duties rule: one user may hold both levels. Tracking rows for these
        fields are therefore authored by OdooBot, so each action also posts a note as the user.
        """
        if self.env.su:
            return
        touched = [name for name in APPROVAL_WRITE_FIELDS if name in vals]
        if not touched:
            return
        raise AccessError(self.env._(
            "An approval cannot be written directly — it is recorded by pressing "
            "Send for Review or Approve on the document, which is what makes it a "
            "record of who agreed and when.\n\n"
            "Refused fields: %(fields)s\n\n"
            "If an approval needs clearing, use Refuse: it clears both, asks for a "
            "reason and returns the document to the review queue.",
            fields=", ".join(touched),
        ))

    def _bill_approval_guard_approver(self, vals):
        """Only holders of either level may choose the step 2 approver, and only while the
        document is a draft without step 2. `env.su` passes (scripts, migrations).
        """
        if self.env.su or APPROVER_FIELD not in vals:
            return
        if not self.env.user._bill_approval_holds_a_level():
            raise AccessError(self.env._(
                "You cannot choose who approves this document. The approver is chosen "
                "by the users who hold an approval level — \"Bills: may review (step 1)\" "
                "or \"Bills: may approve (step 2)\", set on their user record under "
                "Settings > Users > Access Rights > Accounting."
            ))
        for move in self:
            if not move._bill_approval_required():
                raise UserError(self.env._(
                    "%(move)s does not go through approval, so it has no approver to "
                    "choose. Vendor bills, vendor credit notes and customer credit notes "
                    "do.",
                    move=move.display_name,
                ))
            if move.state != 'draft' or move.approval_2_uid:
                raise UserError(self.env._(
                    "The approver of %(move)s can no longer be changed: approval step 2 "
                    "has already been given or the document is no longer a draft. The "
                    "approver is part of the record of who agreed. If the document has "
                    "to change, use Refuse — it clears both approvals and lets you "
                    "choose again.",
                    move=move.display_name,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._bill_approval_guard_fields(vals)
            self._bill_approval_guard_approver(vals)
        moves = super().create(vals_list)
        moves.filtered(APPROVER_FIELD)._bill_approval_sync_approve_task()
        return moves

    def write(self, vals):
        self._bill_approval_guard_fields(vals)
        self._bill_approval_guard_approver(vals)
        result = super().write(vals)
        # Sync the approve task only when a relevant field changed: `write` runs on every move
        # edit, and most documents never have a chosen approver.
        if not TASK_TRIGGER_FIELDS.intersection(vals):
            return result
        concerned = self if APPROVER_FIELD in vals else self.filtered(APPROVER_FIELD)
        concerned._bill_approval_sync_approve_task()
        return result

    # -------------------------------------------------------------- the gate

    def _post(self, soft=True):
        """Refuse to post a document in scope that lacks either approval.

        Exempt: the full-reversal credit note of `_reverse_moves(cancel=True)` and the credit
        note of a completed payment refund. Also closes the reviewer's post task.
        """
        blocked = []
        candidates = self._bill_approval_cancel_reversals()
        # Only a full mirror of the original is exempt: `default_values_list` lets a server
        # caller post a partial credit note with `cancel=True`, which is a new amount.
        cancelling = candidates.filtered(lambda move: move._bill_approval_is_full_reversal())
        partial = candidates - cancelling
        refunds = self._bill_approval_payment_refunds()
        for move in self:
            if not move._bill_approval_required():
                continue
            if move in cancelling or move in refunds:
                continue
            missing = None
            if not move.approval_1_uid:
                missing = self.env._(
                    "step 1, Review — a user holding \"Bills: may review (step 1)\"")
            elif not move.approval_2_uid:
                missing = move._bill_approval_step_2_holders_label()
            if not missing:
                continue
            if move in partial:
                missing = self.env._(
                    "%(missing)s. It reverses %(origin)s only in part, and a partial "
                    "reversal is a new amount, so it needs both approvals",
                    missing=missing, origin=move.reversed_entry_id.display_name)
            blocked.append((move, missing))

        if blocked:
            raise UserError(self.env._(
                "These documents cannot be posted yet, because vendor bills, vendor "
                "credit notes and customer credit notes are reviewed and then approved "
                "BEFORE they reach the ledger:\n\n"
                "%(bills)s\n\n"
                "Use the Send for Review and Approve buttons on the document. If one is "
                "wrong, use Refuse — it clears both approvals, asks for a reason and "
                "returns it to the review queue.",
                bills="\n".join(
                    "  - %s: waiting for %s" % (move.display_name, missing)
                    for move, missing in blocked
                ),
            ))

        posted = super()._post(soft=soft)

        # Said in words on the credit note itself, because its approval fields are empty
        # and a reader would otherwise conclude the gate had been walked round.
        for move in cancelling.filtered(lambda m: m.state == 'posted'):
            move.message_post(body=Markup("<p>%s</p>") % self.env._(
                "Posted without the two-step approval because it is the reversal that "
                "cancels %(origin)s in full. The document it cancels was already posted, "
                "so no new amount was agreed.",
                origin=move.reversed_entry_id.display_name,
            ))
        for move in refunds.filtered(lambda m: m.state == 'posted'):
            move.message_post(body=Markup("<p>%s</p>") % self.env._(
                "Posted without the two-step approval because it records a payment refund "
                "that is already done: the money has gone back, so no new amount was agreed.",
            ))

        # Driven by each move's final condition, not by `posted`: with `soft=True` core does not
        # post future-dated moves but sets `auto_post = 'at_date'` and leaves them out of the
        # result. Those no longer wait for anyone either.
        done = self.filtered(
            lambda move: move._bill_approval_required()
            and not move._bill_approval_is_waiting_to_post()
        )
        if done:
            done._bill_approval_close_post_task()

        return posted

    def _bill_approval_cancel_reversals(self):
        """The moves in `self` that are a `cancel=True` reversal of a posted document named by
        the token set in `_reverse_moves`."""
        token = self.env.context.get(CANCEL_REVERSAL_KEY)
        if not isinstance(token, _CancelReversal):
            return self.browse()
        origins = set(token)
        # Customer credit notes only; a vendor credit note made this way stays gated.
        return self.filtered(
            lambda move: move.move_type in CUSTOMER_TYPES
            and move.reversed_entry_id.id in origins
        )

    def _bill_approval_post_payment_refunds(self):
        """Post customer credit notes that record a completed payment refund, without approval.

        Each move must still pass `_bill_approval_is_payment_refund`, otherwise posting raises.
        """
        token = _PaymentRefund(self.ids)
        self.with_context(**{PAYMENT_REFUND_KEY: token}).action_post()
        return self

    def _bill_approval_payment_refunds(self):
        """The moves in `self` that the payment-refund token names and that pass the checks."""
        token = self.env.context.get(PAYMENT_REFUND_KEY)
        if not isinstance(token, _PaymentRefund):
            return self.browse()
        ids = set(token)
        return self.filtered(
            lambda move: move.id in ids and move._bill_approval_is_payment_refund())

    def _bill_approval_is_payment_refund(self):
        """True when this customer credit note records exactly one completed payment refund.

        It must reverse a posted customer invoice and match, in currency and total, one done
        refund transaction (provider-confirmed where `payment_refund_confirmed` exists) that no
        other posted credit note records. Without `account_payment` there is no exemption.
        """
        self.ensure_one()
        if self.move_type not in CUSTOMER_TYPES or 'transaction_ids' not in self._fields:
            return False
        origin = self.reversed_entry_id
        if origin.move_type != 'out_invoice' or origin.state != 'posted':
            return False
        currency = self.currency_id
        for tx in self.sudo().transaction_ids:
            if tx.operation != 'refund' or tx.state != 'done' or tx.currency_id != currency:
                continue
            # A refund created over RPC looks the same; where the provider confirmation flag
            # exists (only the superuser can set it), require it.
            if 'payment_refund_confirmed' in tx._fields and not tx.payment_refund_confirmed:
                continue
            if currency.compare_amounts(abs(tx.amount), self.amount_total):
                continue
            others = tx.invoice_ids.filtered(
                lambda move: move != self and move.move_type in CUSTOMER_TYPES
                and move.state == 'posted')
            if others:
                continue
            return True
        return False

    def _bill_approval_is_full_reversal(self):
        """True when this credit note exactly mirrors its original: same currency, same total,
        and per account the opposite amounts, compared with the currency's rounding."""
        self.ensure_one()
        origin = self.reversed_entry_id
        if not origin or origin.currency_id != self.currency_id:
            return False
        currency = self.currency_id
        if currency.compare_amounts(origin.amount_total, self.amount_total):
            return False
        totals = {}
        for line in origin.line_ids:
            totals[line.account_id] = totals.get(line.account_id, 0.0) + line.amount_currency
        for line in self.line_ids:
            totals[line.account_id] = totals.get(line.account_id, 0.0) + line.amount_currency
        return all(currency.is_zero(total) for total in totals.values())

    def _reverse_moves(self, default_values_list=None, cancel=False):
        """Let a full cancellation of a posted customer invoice through the gate.

        With `cancel=True` core creates and posts the credit note in one call and reconciles it
        with the original, so nobody could approve it; it carries no new amount. Vendor bills get
        no such exemption: reverse them without cancelling and approve the credit note.
        """
        if not cancel:
            return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)
        token = _CancelReversal(self.filtered(
            lambda move: move.state == 'posted' and move.move_type == 'out_invoice').ids)
        reversals = super(AccountMove, self.with_context(
            **{CANCEL_REVERSAL_KEY: token}
        ))._reverse_moves(default_values_list=default_values_list, cancel=cancel)
        # Hand back records without the token, so nothing the caller does next with them -
        # posting a later edit, say - inherits the exemption.
        return reversals.with_env(self.env)

    def _bill_approval_step_2_holders_label(self):
        """Who can give step 2 on this document, in words, for a refusal message."""
        self.ensure_one()
        approver = self.bill_approval_approver_id.sudo()
        if not approver:
            return self.env._(
                "step 2, Approve — a user holding \"Bills: may approve (step 2)\"")
        substitute = approver.bill_approval_substitute_id
        if not substitute:
            return self.env._(
                "step 2, Approve — by %(approver)s, the chosen approver",
                approver=approver.display_name)
        return self.env._(
            "step 2, Approve — by %(approver)s, the chosen approver, or their "
            "substitute %(substitute)s",
            approver=approver.display_name, substitute=substitute.display_name)

    def _autopost_bill(self):
        """Skip auto-posting an unapproved bill instead of raising.

        The callers are automated imports, where `_post`'s error would surface as a traceback.
        `res.company.autopost_bills` is left alone: it also governs out-of-scope documents.
        """
        self.ensure_one()
        if self._bill_approval_required() and not self._bill_approval_is_approved():
            return
        return super()._autopost_bill()

    def _bill_approval_try_autopost(self):
        """Re-offer the bill to core's auto-posting now that it is approved.

        Core's `_autopost_bill` keeps all its own conditions. Runs in a savepoint with a broad
        `except`, so any posting failure, database errors included, keeps the approval and is
        reported in the chatter. `sudo()` because the approver need not be an accountant; the
        gate itself has no superuser bypass.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.sudo()._autopost_bill()
        except Exception as error:  # noqa: BLE001 — deliberate; see the docstring
            if isinstance(error, UserError):
                # An ordinary business refusal from core: no traceback needed.
                _logger.info(
                    "Bill approval: auto-posting bill %s after its second approval was "
                    "refused: %s", self.id, error)
            else:
                # Unexpected: log the traceback before writing to the chatter.
                _logger.exception(
                    "Bill approval: auto-posting bill %s after its second approval raised "
                    "an unexpected error; the approval is kept", self.id)
            # The record cache may still hold values the rollback discarded.
            self.invalidate_recordset()
            # The same text the user would get from core when posting by hand.
            self.message_post(body=Markup("<p>%s</p>") % self.env._(
                "This vendor is set to post bills automatically and this bill is "
                "now fully approved, but posting it did not succeed: %(error)s\n"
                "The approvals are kept. Post it by hand once the cause is fixed.",
                error=str(error) or type(error).__name__,
            ))

    # ------------------------------------------------ telling the bookkeeper
    #
    # When step 2 is given the reviewer gets an activity. The to-post queue and the systray
    # counter cover every case where no activity could be raised.

    def _bill_approval_post_task_assignee(self):
        """The user who gets the post task: the step 1 approver when active, internal and not
        OdooBot; otherwise an empty recordset.

        There is deliberately no fallback to the step 2 approver; the to-post queue covers it.
        """
        self.ensure_one()
        user = self.approval_1_uid.sudo()
        if not user or not user.active or user.id == SUPERUSER_ID:
            return self.env['res.users']
        if not user._is_internal():
            return self.env['res.users']
        return user

    def _bill_approval_raise_post_task(self):
        """Raise one "post it, then pay it" activity on this document, when appropriate.

        At most one task per document. Returns an empty recordset when skipped, e.g. when the
        activity type is missing (which would otherwise fall back to core's generic To-Do).
        """
        self.ensure_one()
        activity_type = self.env.ref(ACTIVITY_TYPE_XMLID, raise_if_not_found=False)
        if not activity_type:
            _logger.warning(
                "Bill approval: the bill-approval activity type is absent, so no task was raised "
                "on bill %s. The approvals are recorded and the bill is in the "
                "approved-and-draft queue; only the task is missing.", self.id)
            return self.env['mail.activity']

        existing = self.activity_search([ACTIVITY_TYPE_XMLID])
        if existing:
            _logger.info(
                "Bill approval: bill %s already carries an open post-this-bill task, so a second "
                "one was not raised.", self.id)
            return existing

        # A user who gave both approvals is not told to post what they just approved; the queue
        # and the systray badge still show it.
        if self.approval_1_uid == self.approval_2_uid:
            _logger.info(
                "Bill approval: both approvals on bill %s were given by the same user (%s), so no "
                "post-this-bill task was raised. The bill is in the approved-and-draft "
                "queue.", self.id, self.approval_1_uid.display_name)
            return self.env['mail.activity']

        assignee = self._bill_approval_post_task_assignee()
        if not assignee:
            # Tell the next reader on the document, not only the log.
            _logger.warning(
                "Bill approval: bill %s names nobody who can be given the post-this-bill task "
                "(step 1 approver %r is unset, archived, external or OdooBot), so none was "
                "raised. The approvals are recorded and the bill is in the "
                "approved-and-draft queue.",
                self.id, self.approval_1_uid.sudo().login or None)
            self.message_post(body=Markup("<p>%s</p>") % self.env._(
                "This document is fully approved and ready to be posted, but no task could "
                "be assigned: the user who gave approval step 1 is no longer available. It "
                "is waiting in %(queue)s, which every user holding the review level can "
                "see.",
                queue=self._bill_approval_to_post_queue_label(),
            ))
            return self.env['mail.activity']

        # A customer credit note is settled rather than paid.
        if self._bill_approval_is_credit_note():
            summary = self.env._("Post this credit note, then settle it")
            next_step = self.env._("Post it, then settle it with the customer.")
        else:
            summary = self.env._("Post this bill, then pay it")
            next_step = self.env._("Post it, then take it to payment.")
        return self.activity_schedule(
            activity_type_id=activity_type.id,
            summary=summary,
            # Html field: escape the text, which includes user-typed names.
            note=plaintext2html(self.env._(
                "%(partner)s — approval step 2 was given by %(approver)s, so this document "
                "is ready. %(next_step)s\n"
                "It stays in %(queue)s until it is posted.",
                partner=self.partner_id.display_name or self.env._("No partner"),
                approver=self.approval_2_uid.display_name,
                next_step=next_step,
                queue=self._bill_approval_to_post_queue_label(),
            )),
            user_id=assignee.id,
        )

    def _bill_approval_is_credit_note(self):
        """True for the customer family (`out_refund`), whose menus are under Customers."""
        self.ensure_one()
        return self.move_type in CUSTOMER_TYPES

    def _bill_approval_to_post_queue_label(self):
        """Where this document waits once approved, as the menu path a person would click."""
        self.ensure_one()
        if self._bill_approval_is_credit_note():
            return self.env._("Customers > Credit Notes to Post")
        return self.env._("Vendors > Bills to Post")

    # ----------------------------------------------- telling the chosen approver

    def _bill_approval_sync_approve_task(self):
        """Keep one "approve this" task on the chosen approver while the document waits for them.

        Waiting means: in scope, draft, step 1 given, step 2 not given, approver chosen. The task
        goes to the approver, never the substitute. `sudo()` avoids activity record rules for
        tasks assigned to someone else; the acting uid is kept.
        """
        activity_type = self.env.ref(APPROVE_ACTIVITY_TYPE_XMLID, raise_if_not_found=False)
        if not activity_type:
            _logger.warning(
                "Bill approval: the approve-task activity type is absent, so no approve task was "
                "kept in step on %s.", self.ids)
            return
        for move in self:
            existing = move.sudo().activity_search([APPROVE_ACTIVITY_TYPE_XMLID])
            approver = move.bill_approval_approver_id
            waiting = bool(
                move._bill_approval_required()
                and move.state == 'draft'
                and move.approval_1_uid
                and not move.approval_2_uid
                and approver
            )
            if not waiting:
                existing.unlink()
                continue
            if existing and existing.user_id == approver:
                continue
            existing.unlink()
            move.sudo().activity_schedule(
                activity_type_id=activity_type.id,
                summary=self.env._("Approve this document (step 2)"),
                note=plaintext2html(self.env._(
                    "%(partner)s — you were chosen to give approval step 2 on this "
                    "document. Step 1 was given by %(reviewer)s.",
                    partner=move.partner_id.display_name or self.env._("No partner"),
                    reviewer=move.approval_1_uid.display_name,
                )),
                user_id=approver.id,
            )

    def _bill_approval_close_approve_task(self, feedback):
        """Mark the approve task done - step 2 has just been given."""
        self.sudo().activity_feedback([APPROVE_ACTIVITY_TYPE_XMLID], feedback=feedback)

    def _bill_approval_close_post_task(self):
        """Mark the post task done; posting is what the task asked for.

        Not `sudo()`: activity access also allows users with write access to the document.
        Core only closes automated activities here, so hand-made ones of this type are left
        alone.
        """
        self.activity_feedback([ACTIVITY_TYPE_XMLID], feedback=self.env._(
            "The bill was posted."))

    def _bill_approval_drop_post_task(self):
        """Delete the post task on refusal: the task became void rather than done."""
        self.activity_unlink([ACTIVITY_TYPE_XMLID])

    # ----------------------------------------------------------- the systray

    @api.model
    def bill_approval_systray_data(self):
        """What the systray menu shows, fetched by it once per page load.

        `{}` for external users, for users who cannot read journal entries and for users
        holding neither level; the menu hides itself on an empty answer. Checked here and not
        only by the group test, because a level can be given to someone without accounting
        rights, and this runs on every page load.
        """
        if not self.env.user._is_internal():
            return {}
        if not self.browse().has_access('read'):
            return {}
        return self._bill_approval_systray_counts()

    @api.model
    def _bill_approval_systray_counts(self):
        """Systray rows for the current user, or `{}` when they hold neither level.

        One row per queue the user can act on. Not `sudo()`, so each count equals the list it
        opens. The result is a snapshot taken when the page loads.
        """
        rows = []
        for key, group, _filter_name, action_xmlid, domain in BILL_APPROVAL_QUEUES:
            if not self.env.user._has_group(group):
                continue
            rows.append({
                'id': key,
                'count': self.search_count(resolve_queue_domain(domain, self.env.uid)),
                'label': self._bill_approval_systray_label(key),
                'action_xml_id': action_xmlid,
            })
        if not rows:
            return {}
        return {'rows': rows}

    @api.model
    def _bill_approval_systray_label(self, key):
        """The one-line wording of a queue, in the reader's own language."""
        return {
            'to_review': self.env._("Bills to review"),
            'to_approve': self.env._("Bills to approve"),
            'to_post': self.env._("Approved bills to post"),
            'cn_to_review': self.env._("Credit notes to review"),
            'cn_to_approve': self.env._("Credit notes to approve"),
            'cn_to_post': self.env._("Approved credit notes to post"),
        }.get(key, key)

    # ------------------------------------------------------------- the actions

    def _bill_approval_check_level(self, group, level_label):
        """Refuse a user who does not hold the level; a button's `groups=` only hides it."""
        if self.env.su or self.env.user._has_group(group):
            return
        raise AccessError(self.env._(
            "You cannot give approval %(level)s.\n\n"
            "That level is set on a user's record, under Settings > Users > Access "
            "Rights > Accounting (\"Bill Review (step 1)\" and \"Bill Approval "
            "(step 2)\"). Ask an "
            "administrator to grant it, or ask someone who holds it to approve "
            "this document.",
            level=level_label,
        ))

    def _bill_approval_assert_open(self):
        """Every move in `self` must be a draft document in scope."""
        for move in self:
            if not move._bill_approval_required():
                raise UserError(self.env._(
                    "%(move)s does not go through approval. Vendor bills, vendor "
                    "credit notes and customer credit notes do; customer invoices "
                    "do not.",
                    move=move.display_name,
                ))
            if move.state != 'draft':
                raise UserError(self.env._(
                    "%(move)s is already %(state)s. Approval happens while the "
                    "document is a draft, before it reaches the ledger — reset it to "
                    "draft first if it has to change.",
                    move=move.display_name,
                    state=move.state,
                ))

    def action_bill_approval_1_dialog(self):
        """The Send for Review button: open the dialog to choose an approver, then give step 1.

        Server callers use `action_bill_approval_1`, which has no dialog.
        """
        self._bill_approval_check_level(GROUP_1, self.env._("step 1, Review"))
        self._bill_approval_assert_open()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Who should approve this?"),
            'res_model': 'bill.approval.review',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': 'account.move',
                'active_ids': self.ids,
            },
        }

    def action_bill_approval_1(self):
        """Step 1: the figures on this document are right. No dialog.

        Called by the review dialog after it has saved the chosen approver, and by any
        server code that gives step 1 directly.
        """
        self._bill_approval_check_level(
            GROUP_1, self.env._("step 1, Review"))
        self._bill_approval_assert_open()
        # Captured before the sudo, or the recorded approver is OdooBot.
        approver = self.env.user
        for move in self:
            if move.approval_1_uid:
                continue
            # `sudo()` only after the level check; see `_bill_approval_guard_fields`.
            move.sudo().write({
                'approval_1_uid': approver.id,
                'approval_1_date': fields.Datetime.now(),
            })
            text = self.env._(
                "Approval step 1 (Review) given by %(user)s: the partner, the dates, "
                "the lines and the accounts on this document are right.",
                user=approver.display_name,
            )
            if move.bill_approval_approver_id:
                text = self.env._(
                    "%(text)s Chosen approver for step 2: %(chosen)s.",
                    text=text, chosen=move.bill_approval_approver_id.display_name)
            # Posted as the real user; the sudo write's tracking row is authored by OdooBot.
            move.message_post(body=Markup("<p>%s</p>") % text)
        return True

    def _bill_approval_check_chosen_approver(self, user):
        """Refuse step 2 by anyone but the chosen approver or their substitute (one hop).

        Returns `{move: on_behalf_of}`, where `on_behalf_of` is the chosen approver when `user`
        is their substitute. The whole batch is refused before anything is written.
        """
        allowed = {}
        refused = []
        for move in self:
            approver = move.bill_approval_approver_id.sudo()
            if self.env.su or not approver or approver == user:
                allowed[move] = self.env['res.users']
                continue
            if approver.bill_approval_substitute_id == user:
                allowed[move] = approver
                continue
            refused.append(move)
        if refused:
            raise AccessError(self.env._(
                "You cannot give approval step 2 on these documents, because somebody "
                "else was chosen to approve them:\n\n%(moves)s\n\n"
                "Ask them, or their substitute, to approve — or, if they should not be "
                "the approver, change the Approver (step 2) field on the document.",
                moves="\n".join(
                    "  - %s: %s" % (
                        move.display_name,
                        move._bill_approval_step_2_holders_label())
                    for move in refused
                ),
            ))
        return allowed

    def action_bill_approval_2(self):
        """Step 2: agree to pay this document. Requires step 1 first.

        A user holding both levels may give both steps; both approvers show on the form.
        """
        self._bill_approval_check_level(
            GROUP_2, self.env._("step 2, Approve"))
        self._bill_approval_assert_open()
        missing = self.filtered(lambda m: not m.approval_1_uid)
        if missing:
            raise UserError(self.env._(
                "These bills have not been reviewed yet, and step 2 follows step 1:\n\n"
                "%(bills)s\n\n"
                "A user holding \"Bills: may review (step 1)\" presses Send for Review first.",
                bills="\n".join("  - %s" % move.display_name for move in missing),
            ))
        approver = self.env.user
        pending = self.filtered(lambda m: not m.approval_2_uid)
        on_behalf = pending._bill_approval_check_chosen_approver(approver)
        newly_approved = self.browse()
        for move in pending:
            principal = on_behalf[move]
            if principal:
                text = self.env._(
                    "Approval step 2 (Approve) given by %(user)s on behalf of "
                    "%(principal)s, the chosen approver, as their substitute: we agree "
                    "to this document. It can now be posted.",
                    user=approver.display_name, principal=principal.display_name)
            else:
                text = self.env._(
                    "Approval step 2 (Approve) given by %(user)s: we agree to this "
                    "document. It can now be posted.",
                    user=approver.display_name)
            # Before the write: the write's task sync would otherwise delete the task as void.
            move._bill_approval_close_approve_task(text)
            move.sudo().write({
                'approval_2_uid': approver.id,
                'approval_2_date': fields.Datetime.now(),
                'approval_2_on_behalf_uid': principal.id,
            })
            move.message_post(body=Markup("<p>%s</p>") % text)
            newly_approved |= move

        # After the write, so core's own auto-post conditions see an approved bill.
        for move in newly_approved:
            move._bill_approval_try_autopost()
            # Raise the post task only if the bill still waits (not posted or scheduled).
            if move._bill_approval_is_waiting_to_post():
                move._bill_approval_raise_post_task()
        return True

    def _bill_approval_check_refuser(self):
        """Either level may refuse — refusing clears what either level gave.

        A bookkeeper who spots their own mistake should not have to find the
        approver to undo it, and the approver refusing is the ordinary case.
        """
        if self.env.su or self.env.user._bill_approval_holds_a_level():
            return
        raise AccessError(self.env._(
            "You cannot refuse this document. Refusing clears the approvals already "
            "given, so it is held by the users who can give them: either bill "
            "approval level, set on their user record under "
            "Settings > Users > Access Rights > Accounting."
        ))

    def action_bill_approval_refuse(self):
        """Open the refusal dialog; a reason is required."""
        self.ensure_one()
        self._bill_approval_assert_open()
        self._bill_approval_check_refuser()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Refuse this document"),
            'res_model': 'bill.approval.refusal',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_move_id': self.id},
        }

    # ------------------------------------------------- the automated producer

    def _bill_approval_grant_step_1(self, note=None):
        """Give step 1 from an automated producer (e.g. an import hook), without a level check.

        The recorded reviewer is the user the job runs as, and the chatter states that the step
        was raised automatically. Never posts the bill.
        """
        self.ensure_one()
        if not self._bill_approval_required() or self.approval_1_uid:
            return False
        # `sudo()`: the job user may hold no level; the recorded user is still the job's.
        self.sudo().write({
            'approval_1_uid': self.env.user.id,
            'approval_1_date': fields.Datetime.now(),
        })
        # `Markup % args` escapes every operand; never build the body with an f-string.
        self.message_post(body=Markup("<p>%s</p>") % self.env._(
            "Approval step 1 (Review) was raised automatically by %(origin)s, as the "
            "user running it (%(user)s). This records that the document was read into "
            "the bill in full — it is not a person's judgement that the figures are "
            "right. Step 2 (Approve) is still outstanding, so the bill is not posted.",
            origin=note or self.env._("an automated import"),
            user=self.env.user.display_name,
        ))
        return True

    def _bill_approval_clear(self, reason, refuser):
        """Clear both approvals and record who refused and why.

        Reached only through the refusal wizard, which checks access first.
        """
        self.ensure_one()
        # The chosen approver is kept: they still approve the corrected document.
        self.sudo().write({
            'approval_1_uid': False,
            'approval_1_date': False,
            'approval_2_uid': False,
            'approval_2_date': False,
            'approval_2_on_behalf_uid': False,
        })
        # The reason is user input, so it is passed as a `Markup` operand.
        self.message_post(body=Markup("<p>%s</p><p><b>%s</b> %s</p>") % (
            self.env._(
                "Refused by %(user)s. Both approvals were cleared and the document "
                "is back in the review queue.",
                user=refuser.display_name,
            ),
            self.env._("Reason:"),
            reason,
        ))
        # The post task is a stored row, so it must be removed explicitly.
        self._bill_approval_drop_post_task()
