# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Background sends: one prioritised job per invoice (contract §1.1
``_apix_queue`` / ``_apix_send_queued``; events.md
``apix.send.requested.v1``; sad.md Flow C, Flow D; ADR-0005, ADR-0007).

``_apix_queue`` inserts a ``queued`` attempt and its job **in the caller's
transaction** (both or neither), one invoice under one savepoint, so an
invoice already in progress never spoils the others. Each job runs in its
invoice's company whoever queued it (``with_company(move.company_id)``).

The job acts only when the ledger moves its attempt ``queued → running``
itself, so a redelivered, duplicated or cancelled job does nothing. It is
registered without a retry pattern and never raises after the claim: once
the APIX call may have started, a second call could deliver the invoice
twice. Anything raised is recorded as ``send.unexpected``.

🔴 No commit of the job's own transaction here: the outcome is written by the
ledger in its own short transaction — for an email once the job's transaction
commits (the email exists only then), failed if it rolls back.
"""

import logging
from functools import partial

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..lib import readiness
from .account_move import APIX_CHANNELS, NOT_POSTED
from .account_move_send import CHANNEL_UNSUPPORTED, UNEXPECTED, _Verdict
from .goodahead_invoice_send_attempt import CANNOT_CANCEL  # noqa: F401  (re-exported)
from .ledger import Holder

_logger = logging.getLogger(__name__)

COMPANY_PAUSED = "send.company_paused"
SENDING_OFF_SINCE_QUEUED = "send.sending_off_since_queued"
NOT_READY_SINCE_QUEUED = "send.not_ready_since_queued"
EMAIL_FAILED = "send.email_failed"
#: Channels a background job can send.
JOB_CHANNELS = ("einvoice", "letter", "email")
#: Job priority by origin: send now jumps ahead of the background.
PRIORITY = {"mass_now": 10, "mass_background": 20}


def identity_key(attempt_id):
    return f"apix-send:{attempt_id}"


class AccountMove(models.Model):
    _inherit = "account.move"

    # ------------------------------------------------------------------
    # Queue (Flow C)
    # ------------------------------------------------------------------

    def _apix_queue(self, run_uuid, origin, priority):
        """Queue every invoice of ``self``: ``{queued: attempts, in_progress:
        {move_id: Holder}}``. Callers check ``_apix_check_sendable`` first;
        only the in-progress rule is checked again here."""
        Attempt = self.env["goodahead.invoice.send.attempt"]
        queued = Attempt.browse()
        in_progress = {}
        for move in self:
            attempt = move._apix_queue_one(run_uuid, origin, priority)
            if isinstance(attempt, Holder):
                in_progress[move.id] = attempt
                continue
            queued |= attempt
        return {"queued": queued, "in_progress": in_progress}

    def _apix_queue_one(self, run_uuid, origin, priority):
        """The queued attempt with its job, or the ``Holder`` of the send
        already in progress."""
        self.ensure_one()
        Attempt = self.env["goodahead.invoice.send.attempt"]
        Ledger = Attempt._ledger()
        with self.env.cr.savepoint():
            for _try in range(3):
                attempt_id = Ledger._ledger_insert(
                    "queued", self, self._apix_channel(), origin, self.env.user, run_uuid,
                    fields.Datetime.now(),
                )
                if attempt_id:
                    break
                # read fresh: a send claimed meanwhile is not in this snapshot
                holder = Attempt._ledger_holder_now(self)
                if holder:
                    return holder
            else:
                raise UserError(_("The invoice %s could not be queued; try again.", self.name))
            job = self.with_company(self.company_id).with_delay(
                priority=priority,
                identity_key=identity_key(attempt_id),
                description=_("Send invoice %s through APIX", self.name),
            )._apix_send_queued(Attempt.browse(attempt_id))
            Ledger.browse(attempt_id).write({"queue_job_id": job.db_record().id})
        # inserted by SQL: the invoice's delivery history must be read again
        self.invalidate_recordset(["send_attempt_ids"])
        return Attempt.browse(attempt_id)

    # ------------------------------------------------------------------
    # The job (Flow D)
    # ------------------------------------------------------------------

    def _apix_send_queued(self, attempt):
        """Send one queued attempt; see the module docstring. Returns a short
        text for the job's result."""
        self.ensure_one()
        move = self.with_company(self.company_id)
        Attempt = self.env["goodahead.invoice.send.attempt"]
        if not Attempt._ledger_start(attempt):
            return "Not queued any more: nothing done."
        try:
            verdict = move._apix_queued_verdict(attempt)
        except Exception as exc:  # recorded, never raised past the start
            _logger.exception("Background APIX send of invoice %s failed unexpectedly", self.id)
            verdict = _Verdict("failed", UNEXPECTED, _(
                "The send failed unexpectedly (%s).", type(exc).__name__
            ))
        if attempt.channel == "email" and verdict.state == "sent":
            return move._apix_record_email_after_transaction(attempt)
        result = move._apix_record_verdict(attempt, verdict)
        return f"{result.outcome} {result.code}".strip()

    def _apix_record_email_after_transaction(self, attempt):
        """The email exists only once the job's transaction commits: the
        attempt ends ``sent`` then, ``failed`` if it rolls back (R5)."""
        Attempt = self.env["goodahead.invoice.send.attempt"]
        undone = _(
            "The background job was undone after the email step; no email went out."
        )
        Attempt._ledger_after_transaction(
            self,
            on_commit=("sent", partial(Attempt._ledger_finish, attempt, "sent", {})),
            on_rollback=("failed", partial(Attempt._ledger_finish, attempt, "failed",
                                           {"reason": f"[{EMAIL_FAILED}] {undone}"})),
        )
        return "sent"

    def _apix_queued_verdict(self, attempt):
        """Re-check the switch, the pause and readiness, then send by the
        attempt's channel; the ``_Verdict``."""
        channel = attempt.channel
        if channel not in JOB_CHANNELS:
            return _Verdict("failed", CHANNEL_UNSUPPORTED, _(
                "This transmit method cannot be sent in the background; nothing was sent."
            ))
        refused = self._apix_queued_refusal(channel)
        if refused:
            return refused
        if channel == "email":
            return self._apix_send_email()
        return self._apix_send_claimed(attempt, channel, None)

    def _apix_queued_refusal(self, channel):
        """A ``_Verdict`` when the invoice can no longer be sent, else None."""
        if self.state != "posted":
            return _Verdict("failed", NOT_POSTED, _(
                "Not sent: the invoice is no longer posted."
            ))
        Connection = self.env["goodahead.apix.connection"]
        connection = Connection._apix_for_company(self.company_id)
        if channel in APIX_CHANNELS and not connection.sending_enabled:
            return _Verdict("failed", SENDING_OFF_SINCE_QUEUED, _(
                "Not sent: sending through APIX was switched off for %s after the invoice "
                "was queued.", self.company_id.name,
            ))
        if channel in APIX_CHANNELS and connection._apix_send_circuit_open():
            return _Verdict("failed", COMPANY_PAUSED, _(
                "Not attempted — the company's APIX connection is not working."
            ))
        blocking = [
            item for item in self._apix_readiness(channel)["items"]
            if item["severity"] == readiness.BLOCK
        ]
        if blocking:
            return _Verdict("failed", NOT_READY_SINCE_QUEUED, _(
                "Not sent: the invoice is no longer ready: %s",
                " ".join(self.env._(item["message"]) for item in blocking),  # pylint: disable=translation-not-lazy
            ))
        return None

    def _apix_send_email(self):
        """The ERP's own email sending of this invoice."""
        try:
            with self.env.cr.savepoint():
                self.env["account.move.send"].with_context(
                    goodahead_send_recorded=True
                )._generate_and_send_invoices(self, sending_methods=["email"])
        except Exception as exc:  # recorded as the email's failure
            _logger.warning("Email sending of invoice %s failed", self.id, exc_info=True)
            return _Verdict("failed", EMAIL_FAILED, _(
                "The email could not be sent: %s", exc
            ))
        return _Verdict("sent")
