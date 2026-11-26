# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The delivery ledger's own short transactions.

Every outcome of a send must survive whatever happens to the caller's
transaction, so the ledger writes the delivery history on a cursor of its
own, committed as soon as the step is done::

    with ledger_env(self.env) as env:
        env["goodahead.invoice.send.attempt"]._ledger().create(...)

🔴 ONLY ATTEMPT ROWS (and their attachments, and the send job a cancel
stops). Never ``account.move``: the caller's transaction may hold that row.
Inserting an attempt takes PostgreSQL's ``FOR KEY SHARE`` lock on the
invoice, which an ordinary update of the invoice (``FOR NO KEY UPDATE``) does
not conflict with, so a caller that has written the invoice cannot deadlock
the ledger. ``lock_timeout`` turns anything
else into an error instead of a hang.

The ledger's transactions are READ COMMITTED: every transition is
conditional on the row's current state, read under ``FOR UPDATE``, so two
connections racing a claim, or a job start and a cancel, resolve to exactly
one winner and the loser reads the winner's row.

Under the test runner the ledger writes through a savepoint of the test's
own cursor instead (tests stay isolated and see the rows); a test that needs
real connections sets ``FORCE_OWN_CURSOR``.
"""

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from odoo import api
from odoo.modules import module as odoo_module
from odoo.tools import format_datetime

_logger = logging.getLogger(__name__)

IN_PROGRESS = "send.in_progress"
RAN_TOO_LONG = "send.ran_too_long"
CANCELLED = "send.cancelled"
CANCELLED_BY_NEUTRALIZATION = "send.cancelled_by_neutralization"
JOB_LOST = "send.job_lost"

#: A send still running this long after it started is failed.
HUNG_AFTER = timedelta(minutes=10)
LOCK_TIMEOUT = "10s"

#: Tests set this to make the ledger open real cursors under the test runner.
FORCE_OWN_CURSOR = False

#: Plain-language reasons of the codes the ledger itself writes.
REASONS = {
    RAN_TOO_LONG: "The send was still running 10 minutes after it started; "
    "the invoice may already have reached APIX.",
    CANCELLED: "The queued send was cancelled.",
    CANCELLED_BY_NEUTRALIZATION: "The queued send was cancelled because this "
    "database copy was neutralized.",
    JOB_LOST: "The background job ended without sending — nothing was sent.",
}


def reason(env, code):
    """``[code] plain text``."""
    text = REASONS.get(code)
    return f"[{code}] {env._(text)}" if text else f"[{code}]"  # pylint: disable=translation-not-lazy


@dataclass(frozen=True)
class Holder:
    """Who holds an invoice's send in progress, read from the holding row
   : the loser of a claim, or a refused transmit-method change."""

    attempt_id: int
    user_id: int
    user_name: str
    since: datetime
    state: str
    code: str = IN_PROGRESS
    #: The invoice was held by another transaction past the lock timeout
    #: (review R8): in progress, holder unknown.
    busy: bool = False

    @classmethod
    def locked(cls, since):
        return cls(0, 0, "", since, "running", busy=True)

    def message(self, env=None):
        if self.busy:
            text = ("Another send of this invoice is in progress right now; try again in a "
                    "moment.")
            return env._(text) if env else text  # pylint: disable=translation-not-lazy
        since = format_datetime(env, self.since) if env else str(self.since)
        text = "A send of this invoice is already in progress, started by %(user)s on %(since)s."
        if env:
            return env._(text, user=self.user_name, since=since)
        return text % {"user": self.user_name, "since": since}


@dataclass(frozen=True)
class AttemptView:
    """An attempt as the ledger step that wrote it read it, on the ledger's
    own cursor, before committing.

    The caller's transaction is REPEATABLE READ: its snapshot was taken
    before the ledger committed, so a row the step just inserted does not
    exist for it (``MissingError``) and a row it changed still reads as
    before. Callers read the outcome from this view, never by browsing the
    attempt in their own environment."""

    id: int
    move_id: int
    channel: str
    state: str
    origin: str
    run_uuid: str = ""
    apix_batch_id: str = ""
    apix_document_id: str = ""
    apix_credits: float = 0.0
    reason: str = ""
    warning_text: str = ""

    @classmethod
    def of(cls, row):
        """The view of ``row`` (an attempt on the ledger's cursor)."""
        return cls(
            id=row.id,
            move_id=row.move_id.id,
            channel=row.channel,
            state=row.state,
            origin=row.origin,
            run_uuid=row.run_uuid or "",
            apix_batch_id=row.apix_batch_id or "",
            apix_document_id=row.apix_document_id or "",
            apix_credits=row.apix_credits or 0.0,
            reason=row.reason or "",
            warning_text=row.warning_text or "",
        )


#: ``cr.postcommit.data`` / ``cr.postrollback.data`` keys of the ledger
#: steps that wait for the caller's transaction to end (review R5).
AFTER_COMMIT = "goodahead_invoice_send_apix.after_commit"
AFTER_ROLLBACK = "goodahead_invoice_send_apix.after_rollback"


def after_transaction(cr, on_commit=None, on_rollback=None):
    """Run the ``Step`` ``on_commit`` once ``cr`` commits and ``on_rollback``
    once it rolls back (only one of them ever runs: the cursor drops the
    other's queue). For outcomes that are true only if the caller's
    transaction is: an email the ERP queued in that transaction is gone if it
    rolls back."""
    if on_commit:
        _queue(cr.postcommit, AFTER_COMMIT, on_commit)
    if on_rollback:
        _queue(cr.postrollback, AFTER_ROLLBACK, on_rollback)


def _queue(callbacks, key, step):
    if key not in callbacks.data:
        callbacks.data[key] = []
        callbacks.add(lambda: run_steps(callbacks.data.pop(key, [])))
    callbacks.data[key].append(step)


@dataclass(frozen=True)
class Step:
    """One delivery-history write waiting for the caller's transaction:
    ``func()`` writes it (each call on a fresh ledger cursor)."""

    func: object
    env: object
    move_id: int
    state: str


#: The chatter note when a step's history line cannot be written (review F2).
NOTES = {
    "sent": "Sent by email, but its delivery history line could not be written.",
    "failed": "The email could not be sent, and its delivery history line could not be "
    "written.",
    "downloaded": "Downloaded, but its delivery history line could not be written.",
}
NOTE_OTHER = "A send ended, but its delivery history line could not be written."


def run_steps(steps):
    """Run each step; the caller's transaction is over, so nobody would see
    an error: a step that fails is logged and retried once on a fresh
    cursor, and if that fails too the invoice says so in its chatter. The
    next step still runs."""
    for step in steps:
        _run_step(step)


def _run_step(step):
    try:
        step.func()
        return
    except Exception:
        _logger.warning("Delivery history line (%s) of invoice %s could not be written after "
                        "the transaction; retrying on a fresh cursor",
                        step.state, step.move_id, exc_info=True)
    try:
        step.func()
        return
    except Exception:
        _logger.exception("Delivery history line (%s) of invoice %s could not be written "
                          "twice; noting it on the invoice", step.state, step.move_id)
    try:
        _note_on_invoice(step)
    except Exception:
        _logger.exception("Could not note the missing history line on invoice %s",
                          step.move_id)


def _note_on_invoice(step):
    with ledger_env(step.env) as env:
        move = env["account.move"].browse(step.move_id).exists()
        if move:
            text = NOTES.get(step.state, NOTE_OTHER)
            move.message_post(body=env._(text))  # pylint: disable=translation-not-lazy


def own_cursor():
    """Whether the ledger opens a cursor of its own (always, outside tests)."""
    return FORCE_OWN_CURSOR or not odoo_module.current_test


@contextlib.contextmanager
def ledger_env(env):
    """A superuser environment for one ledger step, committed when the block
    ends (rolled back when it raises)."""
    if not own_cursor():
        with env.cr.savepoint():
            yield env(su=True)
        return
    with env.registry.cursor() as cr:
        cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cr.execute("SET LOCAL lock_timeout = %s", [LOCK_TIMEOUT])
        yield api.Environment(cr, env.uid, dict(env.context), su=True)
