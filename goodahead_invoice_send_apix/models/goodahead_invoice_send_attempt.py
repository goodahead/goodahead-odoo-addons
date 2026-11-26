# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The delivery history: one immutable row per try to deliver one invoice on
one channel.

🔴 THE HISTORY IS THE RECORD. Users read it - the attempts of their
allowed companies only (record rule) - and change nothing: ``create``,
``write`` and ``unlink`` refuse with ``send.history_readonly`` unless called
through the ledger (``_ledger()``), whose marker is a Python object no RPC
call can put in a context. The access rights grant read only; the ledger
writes through sudo.

Developer fields (message, package, masked exchange, schema errors, job link)
are for developer mode only.
"""

import logging

import psycopg2.errors

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from . import ledger
from .account_move import DELIVERY_TAB_KEY
from .ledger import AttemptView, Holder, ledger_env

_logger = logging.getLogger(__name__)

HISTORY_READONLY = "send.history_readonly"
CANNOT_CANCEL = "send.cannot_cancel"
HANDLED_REASON_REQUIRED = "send.handled_reason_required"
NOT_LATEST = "send.not_latest"
#: Items of a section in the attention bubble.
ATTENTION_ITEMS = 10
DEVELOPER_GROUP = "base.group_no_one"

#: Context key and marker of `_ledger()`.
CTX_LEDGER = "goodahead_send_ledger"
_LEDGER = object()

STATES = [
    ("queued", "Queued"),
    ("running", "Sending"),
    ("accepted", "Accepted by APIX"),
    ("sent", "Sent"),
    ("refused", "Refused"),
    ("failed", "Failed"),
    ("cancelled", "Cancelled"),
    ("downloaded", "Downloaded"),
]
IN_PROGRESS = ("queued", "running")
#: Origins of sends run by a background job.
BACKGROUND_ORIGINS = ("mass_now", "mass_background")
#: The outcomes a send ends in (`_ledger_finish`, `_ledger_record`).
OUTCOMES = ("accepted", "sent", "refused", "failed", "downloaded")
#: Glossary "Sent": APIX took it, or it went out by email.
SENT_STATES = ("accepted", "sent")
ATTENTION_STATES = ("refused", "failed")
#: Outcomes that are not a send: never the invoice's latest send.
NOT_LATEST_STATES = ("downloaded",)
#: First key of the advisory lock that serialises an invoice's latest send.
LATEST_LOCK = 73101
#: What a finish or a record may set besides the outcome.
OUTCOME_FIELDS = frozenset({
    "reason", "warning_text", "apix_status_code", "apix_batch_id", "apix_document_id",
    "apix_credits", "message_attachment_id", "package_attachment_id", "exchange_log",
    "schema_result", "schema_errors", "units_without_code", "queue_job_id", "end_date",
})
RECORD_FIELDS = OUTCOME_FIELDS | {"migration_source", "run_uuid", "start_date"}
#: Fields a finish fills from files it attaches (the developer view, AC-35).
FILE_FIELDS = frozenset({"message_attachment_id", "package_attachment_id"})


def split_reason(reason):
    """``("send.code", "plain text")`` of a ``[code] plain text`` reason."""
    if not reason.startswith("["):
        return "", reason
    code, _sep, text = reason[1:].partition("]")
    return code, text.strip()


class HandledReasonRequired(UserError):
    """Mark handled without a reason."""

    code = HANDLED_REASON_REQUIRED


class NotLatest(UserError):
    """Mark handled on an attempt that is no longer the invoice's latest
    refused or failed send."""

    code = NOT_LATEST


class SendHistoryReadonly(AccessError):
    """A user tried to create, change or remove a delivery history entry."""

    code = HISTORY_READONLY


class CannotCancel(UserError):
    """Cancel of a send that is no longer queued (Flow E)."""

    code = CANNOT_CANCEL


class GoodaheadInvoiceSendAttempt(models.Model):
    _name = "goodahead.invoice.send.attempt"
    _description = "Invoice send attempt"
    _order = "id desc"
    _rec_name = "move_id"

    move_id = fields.Many2one(
        "account.move", string="Invoice", required=True, index=True, ondelete="cascade", readonly=True
    )
    company_id = fields.Many2one(
        related="move_id.company_id", store=True, index=True, readonly=True
    )
    channel = fields.Selection(
        [
            ("einvoice", "E-invoice"),
            ("letter", "Printed letter"),
            ("email", "Email"),
            ("download", "Download"),
            ("unknown", "Unknown"),
        ],
        required=True,
        readonly=True,
    )
    state = fields.Selection(STATES, string="Outcome", required=True, default="queued", readonly=True)
    origin = fields.Selection(
        [
            ("button", "Send via APIX"),
            ("send_print", "Send & Print"),
            ("mass_now", "Mass sending, now"),
            ("mass_background", "Mass sending, background"),
            ("erp_send", "The ERP's own sending"),
            ("migration", "Earlier setup"),
            ("late_answer", "Late APIX answer"),
        ],
        required=True,
        readonly=True,
    )
    run_uuid = fields.Char(string="Run", readonly=True, copy=False)
    requested_by_id = fields.Many2one("res.users", string="Requested by", required=True, readonly=True)
    queued_date = fields.Datetime(readonly=True)
    start_date = fields.Datetime(readonly=True)
    end_date = fields.Datetime(readonly=True)
    cancelled_by_id = fields.Many2one("res.users", ondelete="set null", readonly=True)
    counts_as_sent = fields.Boolean(readonly=True, default=False)
    is_latest = fields.Boolean(readonly=True, default=False)
    needs_attention = fields.Boolean(readonly=True, default=False)
    handled_by_id = fields.Many2one("res.users", ondelete="set null", readonly=True)
    handled_date = fields.Datetime(readonly=True)
    handled_reason = fields.Text(readonly=True)
    reason = fields.Text(readonly=True)
    warning_text = fields.Text(string="Warnings", readonly=True)
    apix_status_code = fields.Char(string="APIX status code", readonly=True)
    apix_batch_id = fields.Char(string="APIX batch id", readonly=True)
    apix_document_id = fields.Char(string="APIX document id", readonly=True)
    apix_credits = fields.Float(string="Credits", readonly=True)
    message_attachment_id = fields.Many2one(
        "ir.attachment", string="Finvoice message", ondelete="set null", readonly=True,
        groups=DEVELOPER_GROUP,
    )
    package_attachment_id = fields.Many2one(
        "ir.attachment", string="Package", ondelete="set null", readonly=True,
        groups=DEVELOPER_GROUP,
    )
    # SCR-13 downloads of the files above (not stored)
    message_file = fields.Binary(related="message_attachment_id.datas", string="Finvoice message file",
                                 groups=DEVELOPER_GROUP)
    message_filename = fields.Char(related="message_attachment_id.name", string="Finvoice message file name",
                                   groups=DEVELOPER_GROUP)
    package_file = fields.Binary(related="package_attachment_id.datas", string="Package file",
                                 groups=DEVELOPER_GROUP)
    package_filename = fields.Char(related="package_attachment_id.name", string="Package file name",
                                   groups=DEVELOPER_GROUP)
    exchange_log = fields.Text(string="APIX exchange", readonly=True, groups=DEVELOPER_GROUP)
    schema_result = fields.Selection(
        [("not_checked", "Not checked"), ("valid", "Valid"), ("invalid", "Invalid")],
        default="not_checked",
        readonly=True,
    )
    schema_errors = fields.Text(readonly=True, groups=DEVELOPER_GROUP)
    units_without_code = fields.Char(string="Units without a code", readonly=True)
    queue_job_id = fields.Many2one(
        "queue.job", string="Background job", ondelete="set null", readonly=True,
        groups=DEVELOPER_GROUP,
    )
    # SCR-13: the job as text for developers without job rights
    queue_job_label = fields.Char(string="Background job id and state", compute="_compute_queue_job_label",
                                  groups=DEVELOPER_GROUP)
    migration_source = fields.Char(readonly=True)
    late_for_attempt_id = fields.Many2one(
        "goodahead.invoice.send.attempt", string="Late answer for", ondelete="set null", readonly=True
    )
    # SCR-02 (the delivery tab): when, and who started it or queued it.
    partner_id = fields.Many2one(related="move_id.partner_id", string="Customer")
    display_date = fields.Datetime(string="When", compute="_compute_display")
    started_by_label = fields.Char(string="Started by", compute="_compute_display")

    # 🔴 `models.UniqueIndex` / `models.Index`, not `_sql_constraints` (ignored in 19.0).
    _one_in_progress = models.UniqueIndex(
        "(move_id) WHERE state IN ('queued', 'running')",
        "A send of this invoice is already in progress.",
    )
    _migration_once = models.UniqueIndex(
        "(move_id, migration_source) WHERE origin = 'migration'",
        "This invoice was already carried over from that setup.",
    )
    _running_since_idx = models.Index("(start_date) WHERE state = 'running'")
    _queued_idx = models.Index("(queued_date) WHERE state = 'queued'")
    _attention_idx = models.Index("(company_id) WHERE needs_attention")
    _sent_idx = models.Index("(move_id) WHERE counts_as_sent")
    _run_idx = models.Index("(run_uuid) WHERE run_uuid IS NOT NULL")

    @api.depends("end_date", "start_date", "queued_date", "origin", "requested_by_id")
    def _compute_display(self):
        for attempt in self:
            attempt.display_date = attempt.end_date or attempt.start_date or attempt.queued_date
            name = attempt.requested_by_id.name or ""
            if attempt.origin in BACKGROUND_ORIGINS:
                name = _("background, queued by %s", name)
            attempt.started_by_label = name

    def _compute_queue_job_label(self):
        for attempt in self:
            job = attempt.sudo().queue_job_id
            attempt.queue_job_label = job and f"{job.uuid} ({job.state})"

    @api.constrains("handled_by_id", "handled_reason")
    def _check_handled_reason(self):
        for attempt in self:
            if attempt.handled_by_id and not (attempt.handled_reason or "").strip():
                raise ValidationError(_("Marking a send as handled needs a reason."))

    # ------------------------------------------------------------------
    # The history is the record
    # ------------------------------------------------------------------

    def _ledger(self):
        """These attempts (or the model) as the ledger writes them: sudo, with
        the marker that lets `create`, `write` and `unlink` through."""
        return self.sudo().with_context(**{CTX_LEDGER: _LEDGER})

    def _is_ledger(self):
        return self.env.context.get(CTX_LEDGER) is _LEDGER

    def _refuse_change(self):
        raise SendHistoryReadonly(
            _("The delivery history cannot be changed or removed: it is the record of every send.")
        )

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_ledger():
            self._refuse_change()
        return super().create(vals_list)

    def write(self, vals):
        if not self._is_ledger():
            self._refuse_change()
        return super().write(vals)

    def unlink(self):
        if not self._is_ledger():
            self._refuse_change()
        return super().unlink()

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------

    def action_cancel(self):
        """Cancel this queued send: the send does not take place, the
        invoice is free at once. ``send.cannot_cancel`` once it started."""
        self.ensure_one()
        self.check_access("read")
        if self._ledger_cancel(self, self.env.user):
            return {"type": "ir.actions.client", "tag": "soft_reload"}
        # the state as committed now (a job may have started it since this
        # transaction began), read on the ledger's cursor
        with ledger_env(self.env) as env:
            now_state = env[self._name]._ledger_view(self.id).state
        state = dict(STATES).get(now_state, now_state)
        raise CannotCancel(_("Cannot cancel — the send is no longer queued (now: %s).",
                             self.env._(state)))  # pylint: disable=translation-not-lazy

    # ------------------------------------------------------------------
    # Failed sends (AC-33, AC-34; SCR-10 – SCR-12, Flow K)
    # ------------------------------------------------------------------

    def action_open_technical(self):
        """SCR-13, developer mode only: this attempt in the developer view
        (files, masked exchange, schema result, job)."""
        self.ensure_one()
        if not self.env.user.has_group(DEVELOPER_GROUP):
            raise AccessError(_("The developer view needs developer mode."))
        view = self.env.ref(
            "goodahead_invoice_send_apix.goodahead_invoice_send_attempt_view_form_technical")
        return {
            "type": "ir.actions.act_window",
            "name": _("Send attempt — technical"),
            "res_model": self._name,
            "res_id": self.id,
            "views": [[view.id, "form"]],
            "target": "new",
        }

    def action_open_invoice(self):
        """The attempt's invoice on its Delivery tab."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": self.move_id.id,
            "views": [[False, "form"]],
            "context": {DELIVERY_TAB_KEY: True},
        }

    def action_open_mark_handled(self):
        """SCR-12 for the invoice's latest send that needs attention."""
        self.ensure_one()
        latest = self.search(
            [("move_id", "=", self.move_id.id), ("needs_attention", "=", True)], limit=1
        )
        if not latest:
            raise NotLatest(_("This send no longer needs attention — reopen the list."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Mark handled"),
            "res_model": "goodahead.invoice.send.mark.handled",
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
            "context": {"default_attempt_id": latest.id},
        }

    def _mark_handled(self, reason):
        """Who handled this failed send, when and why; it leaves the
        failed-sends list. The only change an ended attempt accepts, made
        through the ledger, only while it is still the invoice's latest send
        needing attention (``send.not_latest``)."""
        self.ensure_one()
        self.check_access("read")
        reason = (reason or "").strip()
        if not reason:
            raise HandledReasonRequired(_("A reason is required."))
        with ledger_env(self.env) as env:
            Attempt = env[self._name]._ledger()
            Attempt._ledger_lock(self.id)
            row = Attempt.browse(self.id)
            if not (row.is_latest and row.needs_attention):
                raise NotLatest(_("This send is no longer the latest — reopen the list."))
            row.write({
                "handled_by_id": self.env.uid,
                "handled_date": fields.Datetime.now(),
                "handled_reason": reason,
                "needs_attention": False,
            })
        self._ledger_result(self.id)
        return True

    @api.model
    def _attention_failed_sends_section(self):
        """The ``failed_sends`` section of the shared bubble: invoices whose
        latest send was refused or failed and is not handled, in the user's
        active companies (``_attention_idx``); an item opens the invoice on
        its Delivery tab."""
        domain = [("needs_attention", "=", True), ("company_id", "in", self.env.companies.ids)]
        states = dict(STATES)
        attempts = self.search(domain, order="end_date desc, id desc", limit=ATTENTION_ITEMS)
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "goodahead_invoice_send_apix.action_failed_sends"
        )
        return {
            "label": _("Failed sends"),
            "count": self.search_count(domain),
            "items": [
                {
                    "id": attempt.move_id.id,
                    "title": attempt.move_id.name,
                    "subtitle": " · ".join(filter(None, [
                        self.env._(states[attempt.state]),  # pylint: disable=translation-not-lazy
                        split_reason(attempt.reason or "")[1],
                    ])),
                    "badge": attempt.state,
                    "date": fields.Datetime.to_string(attempt.end_date),
                }
                for attempt in attempts
            ],
            "action": action,
            "item_action": {
                "type": "ir.actions.act_window",
                "res_model": "account.move",
                "views": [[False, "form"]],
                "context": {DELIVERY_TAB_KEY: True},
            },
        }

    # ------------------------------------------------------------------
    # The ledger: short transactions of its own
    # ------------------------------------------------------------------
    #
    # Each method below is one transaction on the ledger's own cursor
    # (`ledger.ledger_env`), committed when it returns. The caller's
    # transaction is REPEATABLE READ and its snapshot predates that commit:
    # a row the ledger has just inserted does not exist for it and a changed
    # row reads as before. So the steps hand back an `AttemptView` read on
    # the ledger's cursor, never a record to be read in the caller's
    # transaction; read fresh rows with `_ledger_run_status`, never by
    # re-searching.

    @api.model
    def _ledger_claim(self, move, channel, origin, user, run_uuid=False):
        """Claim ``move`` for one send: an attempt ``running`` from now, or the
        ``Holder`` of the send already in progress.

        A send still running 10 minutes after it started is failed first
        (``send.ran_too_long``, AC-59). The partial unique index
        ``_one_in_progress`` decides between two connections claiming at once.
        """
        now = fields.Datetime.now()
        try:
            with ledger_env(self.env) as env:
                Attempt = env[self._name]._ledger()
                Attempt._ledger_fail_hung(move.id, now)
                for _try in range(3):
                    attempt_id = Attempt._ledger_insert_running(
                        move, channel, origin, user, run_uuid, now)
                    if attempt_id:
                        return Attempt._ledger_view(attempt_id)
                    holder = Attempt._ledger_holder(move.id)
                    if holder:
                        return holder
        except psycopg2.errors.LockNotAvailable:
            # another transaction holds the invoice (the ERP's batch cron, an
            # email being sent) past the lock timeout: it is being sent (R8)
            _logger.info("Claim of invoice %s waited past the lock timeout", move.id)
            return Holder.locked(now)
        raise ValidationError(_("The invoice could not be claimed for sending; try again."))

    @api.model
    def _ledger_holder_now(self, move):
        """The ``Holder`` of ``move``'s send in progress as committed right
        now (a fresh read on the ledger's cursor; nothing locked), or None."""
        with ledger_env(self.env) as env:
            return env[self._name]._ledger()._ledger_holder(move.id)

    @api.model
    def _ledger_after_transaction(self, move, on_commit=None, on_rollback=None):
        """Ledger steps of ``move`` that wait for the caller's transaction to
        end (``ledger.after_transaction``); each is ``(state, func)``."""
        def step(pair):
            return pair and ledger.Step(pair[1], self.env, move.id, pair[0])

        ledger.after_transaction(self.env.cr, step(on_commit), step(on_rollback))

    @api.model
    def _ledger_start(self, attempt):
        """``queued → running`` where still queued (a job starting its send).
        False: cancelled, already running or ended - do nothing."""
        with ledger_env(self.env) as env:
            Attempt = env[self._name]._ledger()
            if Attempt._ledger_lock(attempt.id) != "queued":
                return False
            Attempt.browse(attempt.id).write(
                {"state": "running", "start_date": fields.Datetime.now()}
            )
        self._ledger_result(attempt.id)
        return True

    @api.model
    def _ledger_finish(self, attempt, state, values, files=None):
        """Record the outcome of a running send. An attempt that is no longer
        running (the sweeper or the next claim failed it) is never
        overwritten: the late verdict becomes a new attempt, ``origin =
        late_answer`` (Flow E). ``files`` (``{field: (name, bytes,
        mimetype)}``, the message and package fields) become attachments of
        the attempt in the same transaction."""
        self._ledger_check(state, values, OUTCOME_FIELDS)
        if set(files or {}) - FILE_FIELDS:
            raise ValueError(f"The ledger keeps no file in {sorted(set(files) - FILE_FIELDS)}")
        now = fields.Datetime.now()
        with ledger_env(self.env) as env:
            Attempt = env[self._name]._ledger()
            row = Attempt.browse(attempt.id)
            if Attempt._ledger_lock(attempt.id) != "running":
                row = Attempt.create({
                    "move_id": row.move_id.id,
                    "channel": row.channel,
                    "state": state,
                    "origin": "late_answer",
                    "requested_by_id": row.requested_by_id.id,
                    "run_uuid": row.run_uuid,
                    "start_date": row.start_date,
                    "late_for_attempt_id": row.id,
                })
            values = dict(values, **Attempt._ledger_files(row, files))
            Attempt._ledger_end(row, state, values, now)
            view = Attempt._ledger_view(row.id)
        self._ledger_result(view.id, attempt.id)
        return view

    @api.model
    def _ledger_record(self, move, channel, state, origin, user, values):
        """One-step attempt that is already over: an email or download from
        the ERP's sending, or a send carried over from an earlier setup
        (``origin = migration``, once per invoice and ``migration_source``)."""
        self._ledger_check(state, values, RECORD_FIELDS)
        now = fields.Datetime.now()
        with ledger_env(self.env) as env:
            Attempt = env[self._name]._ledger()
            if origin == "migration":
                existing = Attempt.search([
                    ("move_id", "=", move.id),
                    ("origin", "=", "migration"),
                    ("migration_source", "=", values.get("migration_source")),
                ], limit=1)
                if existing:
                    return Attempt._ledger_view(existing.id)
            row = Attempt.create({
                "move_id": move.id,
                "channel": channel,
                "state": state,
                "origin": origin,
                "requested_by_id": user.id,
                "start_date": values.get("start_date") or values.get("end_date") or now,
                "migration_source": values.get("migration_source"),
                "run_uuid": values.get("run_uuid"),
            })
            Attempt._ledger_end(row, state, values, now)
            view = Attempt._ledger_view(row.id)
        self._ledger_result(view.id)
        return view

    @api.model
    def _ledger_cancel(self, attempt, user, code=ledger.CANCELLED):
        """``queued → cancelled`` where still queued, then cancel its job.
       False: no longer queued (the caller shows
        ``send.cannot_cancel``)."""
        with ledger_env(self.env) as env:
            Attempt = env[self._name]._ledger()
            if Attempt._ledger_lock(attempt.id) != "queued":
                return False
            row = Attempt.browse(attempt.id)
            row.write({
                "state": "cancelled",
                "end_date": fields.Datetime.now(),
                "cancelled_by_id": user.id,
                "reason": ledger.reason(env, code),
                "needs_attention": False,
            })
            job = row.queue_job_id
            if job and job.state in ("wait_dependencies", "pending", "enqueued"):
                job.button_cancelled()
        self._ledger_result(attempt.id)
        return True

    @api.model
    def _ledger_run_status(self, run_uuid):
        """Fresh read of a run's attempts (the mass wizard polls it, §3.4)."""
        with ledger_env(self.env) as env:
            attempts = env[self._name].search([("run_uuid", "=", run_uuid)], order="id")
            return [
                {
                    "id": attempt.id,
                    "move_id": attempt.move_id.id,
                    "move_name": attempt.move_id.display_name,
                    "company_name": attempt.company_id.name,
                    "state": attempt.state,
                    "channel": attempt.channel,
                    "reason": attempt.reason or "",
                    "batch_id": attempt.apix_batch_id or "",
                    "warnings": attempt.warning_text or "",
                    "queued_date": attempt.queued_date,
                    "start_date": attempt.start_date,
                    "end_date": attempt.end_date,
                }
                for attempt in attempts
            ]

    @api.model
    def _ledger_sweep(self, now=None):
        """Release every invoice stuck in progress (Flow E, AC-59): fail each
        send still ``running`` 10 minutes after it started
        (``send.ran_too_long``) and each ``queued`` send whose job ended
        without moving it — failed, cancelled, done or gone
        (``send.job_lost``). A job still waiting is left alone. Conditional
        and row-locked, so overlapping runs fail each attempt once. Returns
        how many were failed."""
        now = now or fields.Datetime.now()
        with ledger_env(self.env) as env:
            Attempt = env[self._name]._ledger()
            swept = {
                ledger.RAN_TOO_LONG: Attempt._ledger_hung_ids(now),
                ledger.JOB_LOST: Attempt._ledger_lost_ids(),
            }
            for code, attempt_ids in swept.items():
                reason = ledger.reason(env, code)
                for attempt in Attempt.browse(attempt_ids):
                    Attempt._ledger_end(attempt, "failed", {"reason": reason}, now)
        return sum(len(ids) for ids in swept.values())

    # -- ledger internals (called inside `ledger_env`, on `_ledger()`) --------

    @api.model
    def _ledger_hung_ids(self, now):
        self.flush_model()
        self.env.cr.execute(SQL(
            "SELECT id FROM %s WHERE state = 'running' AND start_date < %s"
            " ORDER BY id FOR UPDATE SKIP LOCKED",
            SQL.identifier(self._table),
            now - ledger.HUNG_AFTER,
        ))
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _ledger_lost_ids(self):
        self.flush_model()
        self.env["queue.job"].flush_model(["state"])
        self.env.cr.execute(SQL(
            """
            SELECT a.id
              FROM %s a
              LEFT JOIN queue_job j ON j.id = a.queue_job_id
             WHERE a.state = 'queued'
               AND (j.id IS NULL OR j.state IN ('failed', 'cancelled', 'done'))
             ORDER BY a.id
               FOR UPDATE OF a SKIP LOCKED
            """,
            SQL.identifier(self._table),
        ))
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _ledger_check(self, state, values, allowed):
        if state not in OUTCOMES:
            raise ValueError(f"{state!r} is not the outcome of a send")
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"The ledger does not set {sorted(unknown)} here")

    @api.model
    def _ledger_files(self, attempt, files):
        """``{field: attachment id}`` for ``files`` attached to ``attempt``."""
        Attachment = self.env["ir.attachment"].sudo()
        return {
            field: Attachment.create({
                "name": name,
                "raw": raw,
                "mimetype": mimetype,
                "res_model": self._name,
                "res_id": attempt.id,
            }).id
            for field, (name, raw, mimetype) in (files or {}).items()
            if raw
        }

    def _ledger_result(self, *attempt_ids):
        """Drop the caller's cache of these attempts after a ledger step."""
        self.browse(attempt_ids).invalidate_recordset()

    def _ledger_view(self, attempt_id):
        """The ``AttemptView`` of an attempt, read on the ledger's cursor
        (inside ``ledger_env``) before the step commits."""
        self.flush_model()
        row = self.browse(attempt_id)
        row.invalidate_recordset()
        return AttemptView.of(row)

    @api.model
    def _ledger_lock(self, attempt_id):
        """The attempt's current state, its row locked until the step ends."""
        self.flush_model()
        self.env.cr.execute(
            SQL("SELECT state FROM %s WHERE id = %s FOR UPDATE", SQL.identifier(self._table), attempt_id)
        )
        row = self.env.cr.fetchone()
        self.browse(attempt_id).invalidate_recordset()
        return row[0] if row else None

    @api.model
    def _ledger_insert_running(self, move, channel, origin, user, run_uuid, now):
        """Insert the running attempt unless one is in progress; its id or None."""
        return self._ledger_insert("running", move, channel, origin, user, run_uuid, now)

    @api.model
    def _ledger_insert(self, state, move, channel, origin, user, run_uuid, now):
        """Insert a ``running`` (start date now) or ``queued`` (queued date
        now) attempt unless one is in progress; its id or None. In a
        REPEATABLE READ transaction (the caller's, when queueing) a send
        claimed and committed meanwhile is a serialization failure, not a
        conflict PostgreSQL can skip: when a fresh read finds that send, it is
        "in progress" too (None); any other concurrent update is raised again
        so the ERP retries the whole request (review F1)."""
        # the ORM's pending writes (a hung send just failed) first
        self.flush_model()
        try:
            with self.env.cr.savepoint(flush=False):
                return self._ledger_insert_row(state, move, channel, origin, user, run_uuid, now)
        except psycopg2.errors.SerializationFailure:
            if self._ledger_holder_now(move):
                return None
            raise

    @api.model
    def _ledger_insert_row(self, state, move, channel, origin, user, run_uuid, now):
        self.env.cr.execute(SQL(
            """
            INSERT INTO %(table)s
                (move_id, company_id, channel, state, origin, run_uuid, requested_by_id,
                 start_date, queued_date, counts_as_sent, is_latest, needs_attention,
                 schema_result, create_uid, create_date, write_uid, write_date)
            SELECT m.id, m.company_id, %(channel)s, %(state)s, %(origin)s, %(run)s, %(user)s,
                   %(start)s, %(queued)s, false, false, false, 'not_checked',
                   %(uid)s, %(now)s, %(uid)s, %(now)s
              FROM account_move m
             WHERE m.id = %(move)s
            ON CONFLICT (move_id) WHERE state IN ('queued', 'running') DO NOTHING
            RETURNING id
            """,
            table=SQL.identifier(self._table),
            channel=channel,
            state=state,
            origin=origin,
            run=run_uuid or None,
            user=user.id,
            start=now if state == "running" else None,
            queued=now if state == "queued" else None,
            now=now,
            uid=self.env.uid,
            move=move.id,
        ))
        row = self.env.cr.fetchone()
        return row[0] if row else None

    @api.model
    def _ledger_holder(self, move_id):
        """The send in progress of ``move_id`` as a ``Holder``, or None."""
        self.flush_model()
        self.env.cr.execute(SQL(
            """
            SELECT a.id, a.requested_by_id, p.name,
                   COALESCE(a.start_date, a.queued_date, a.create_date), a.state
              FROM %s a
              JOIN res_users u ON u.id = a.requested_by_id
              JOIN res_partner p ON p.id = u.partner_id
             WHERE a.move_id = %s AND a.state IN ('queued', 'running')
             ORDER BY a.id
             LIMIT 1
            """,
            SQL.identifier(self._table),
            move_id,
        ))
        row = self.env.cr.fetchone()
        return Holder(*row) if row else None

    @api.model
    def _ledger_fail_hung(self, move_id, now):
        """Fail every send of ``move_id`` running for more than 10 minutes."""
        self.env.cr.execute(SQL(
            "SELECT id FROM %s WHERE move_id = %s AND state = 'running' AND start_date < %s"
            " FOR UPDATE",
            SQL.identifier(self._table),
            move_id,
            now - ledger.HUNG_AFTER,
        ))
        hung = self.browse([row[0] for row in self.env.cr.fetchall()])
        hung.invalidate_recordset()
        for attempt in hung:
            self._ledger_end(attempt, "failed", {"reason": ledger.reason(self.env, ledger.RAN_TOO_LONG)}, now)

    @api.model
    def _ledger_end(self, attempt, state, values, now):
        """Write an outcome with its flags. A send's outcome becomes the
        invoice's latest and the previous latest loses that and its
        attention; a download never does — it neither counts nor clears a
        failed send. The latest is decided under the invoice's own
        lock, so two outcomes ending at once leave one latest."""
        latest = state not in NOT_LATEST_STATES
        vals = dict(values)
        vals.update(
            state=state,
            end_date=values.get("end_date") or now,
            counts_as_sent=state in SENT_STATES,
            is_latest=latest,
            needs_attention=latest and state in ATTENTION_STATES,
        )
        if latest:
            self._ledger_lock_latest(attempt.move_id.id)
            previous = self.search([
                ("move_id", "=", attempt.move_id.id),
                ("is_latest", "=", True),
                ("id", "!=", attempt.id),
            ])
            if previous:
                previous.write({"is_latest": False, "needs_attention": False})
        attempt.write(vals)

    @api.model
    def _ledger_lock_latest(self, move_id):
        """Wait for any other ledger step deciding ``move_id``'s latest send
        (a transaction-level advisory lock; the invoice row itself is never
        locked by the ledger)."""
        self.flush_model()
        self.env.cr.execute(SQL("SELECT pg_advisory_xact_lock(%s, %s)", LATEST_LOCK, move_id))
