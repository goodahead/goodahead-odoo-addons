# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import dataclasses
import json
import logging

from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression
from odoo.tools import SQL
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.goodahead_einvoice_base.lib.finvoice import rows as finvoice_rows
from odoo.addons.goodahead_einvoice_base.models.account_move import BUYER_REFERENCE_MAX

from ..lib import import_record as lifecycle
from ..lib.import_source import (
    DEFAULT_POLICY,
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_IMPORTED,
    ImportResult,
    readers_in_order,
)
from ..lib.invoice_data import (
    InvoiceDataError,
    InvoiceTotalsError,
    SourceDataValidator,
    stated_document_for_the_bill,
    stored_price,
    undiscounted_row,
)
from .account_edi_ubl import EINVOICE_UNIT_CODES_CONTEXT_KEY
from .res_partner import MATCHING_POLICY_CONTEXT_KEY

_logger = logging.getLogger(__name__)


class _AiImportDecodeError(Exception):
    """Internal signal to roll back the create-and-decode savepoint on a UBL
    decode error (distinct from a raw DB exception, for a clearer message)."""


# Dedicated queue_job channel for the (slow, ~60s) Claude extraction jobs. Give it
# a small capacity in the deploy config (e.g. `channels = root:N,root.ai_import:1`)
# so these long jobs can't occupy the whole worker pool and starve the UI. queue_job
# stores the channel as a plain string and auto-creates the channel node, so no
# queue.job.channel record is required here; the CAPACITY is a deploy-config setting.
AI_IMPORT_CHANNEL = "root.ai_import"

#: The stored lifecycle; `display_state` adds "processing".
STATE_SELECTION = [
    (lifecycle.STATE_WAITING, "Waiting"),
    (lifecycle.STATE_IMPORTED, "Imported"),
    (lifecycle.STATE_DUPLICATE, "Duplicate"),
    (lifecycle.STATE_FAILED, "Failed"),
    (lifecycle.STATE_POSSIBLY_LOST, "Possibly lost"),
    (lifecycle.STATE_CLOSED, "Closed without bill"),
]

#: The statuses that need an Accountant: counted by the systray.
ATTENTION_STATES = (
    lifecycle.STATE_FAILED,
    lifecycle.STATE_DUPLICATE,
    lifecycle.STATE_POSSIBLY_LOST,
)

#: Not settled yet: the inbox's "Unsettled" filter.
UNSETTLED_STATES = (
    lifecycle.STATE_WAITING,
    lifecycle.STATE_FAILED,
    lifecycle.STATE_DUPLICATE,
    lifecycle.STATE_POSSIBLY_LOST,
)


def _summary_text(value):
    """A short text column value from invoice data; False when empty."""
    if value is None or value is False or isinstance(value, (dict, list)):
        return False
    return str(value).strip() or False


class GoodaheadInvoiceImport(models.Model):
    _name = "goodahead.invoice.import"
    _inherit = ["mail.thread"]
    _description = "AI Invoice Import"
    _order = "create_date desc"

    name = fields.Char(string="Filename", readonly=True)
    # 🔴 THE STORED LIFECYCLE HAS NO "PROCESSING". A job writes
    # inside one transaction that commits only at its end, so a stored
    # "processing" was never seen by anybody else. What users see is
    # `display_state`, derived from the record's job, which the job runner
    # commits in its own transaction.
    state = fields.Selection(
        STATE_SELECTION,
        default=lifecycle.STATE_WAITING,
        required=True,
        readonly=True,
        index=True,
    )
    display_state = fields.Selection(
        STATE_SELECTION + [(lifecycle.STATE_PROCESSING, "Processing")],
        string="Status",
        compute="_compute_display_state",
        search="_search_display_state",
        help="The stored status, or Processing while the record's background "
        "job is queued or running.",
    )
    processing_stale = fields.Boolean(
        string="Processing too long",
        compute="_compute_display_state",
        search="_search_processing_stale",
        help="Shown as processing for more than %s minutes: the job may have "
        "been lost by a worker and wait for the job runner to requeue it."
        % lifecycle.STALE_PROCESSING_MINUTES,
    )
    source = fields.Selection(
        [("upload", "Upload"), ("email", "Email")],
        required=True,
        default="upload",
        readonly=True,
        help="Where the document came from. Source add-ons add their own.",
    )
    # 🔴 WHAT READS THE RECORD IS STORED, NOT WORKED OUT FROM WHICH ADD-ON
    # LOADED LAST. Each source add-on adds its key with `selection_add`
    # and registers a `SourceReader` in `_import_readers`; the job and the
    # re-import dispatch on this value. Empty on a record nobody claimed yet:
    # `_import_reader` then asks the installed readers in priority order.
    reader = fields.Selection(
        [],
        readonly=True,
        index=True,
        copy=False,
        help="What reads this record's document: set by the source that "
        "created it.",
    )
    failure_kind = fields.Selection(
        [("data", "Needs an Accountant"), ("interrupted", "Interrupted")],
        readonly=True,
        help="Data: the document or its data has to be fixed by hand. "
        "Interrupted: the import stopped on a temporary problem and is retried.",
    )
    queue_job_id = fields.Many2one(
        "queue.job",
        string="Current job",
        ondelete="set null",
        index=True,
        readonly=True,
        copy=False,
    )
    duplicate_of_move_id = fields.Many2one(
        "account.move",
        string="Duplicate of",
        ondelete="set null",
        readonly=True,
        copy=False,
    )
    duplicate_override = fields.Boolean(
        readonly=True,
        copy=False,
        help="Set by an Accountant to import this invoice although it looks "
        "like a duplicate.",
    )
    close_reason = fields.Text(readonly=True, copy=False)
    closed_by_id = fields.Many2one("res.users", string="Closed by", readonly=True, copy=False)
    closed_date = fields.Datetime(readonly=True, copy=False)
    invoice_number_normalized = fields.Char(
        readonly=True,
        index=True,
        help="The invoice number with case, spaces and separators removed, as "
        "the duplicate check compares it.",
    )
    seller_vat = fields.Char(string="Seller VAT", readonly=True)
    seller_business_id = fields.Char(string="Seller business ID", readonly=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        readonly=True,
    )
    source_email = fields.Char(readonly=True)
    journal_id = fields.Many2one(
        "account.journal",
        string="Journal",
        readonly=True,
        help="The purchase journal the manual upload targeted. Empty for the "
        "email path, which falls back to the company's first purchase journal.",
    )
    upload_move_type = fields.Char(
        readonly=True,
        help="The move type requested by the manual upload (e.g. in_invoice / "
        "in_refund). Empty for the email path (defaults to in_invoice).",
    )
    pdf_attachment_id = fields.Many2one("ir.attachment", string="PDF", readonly=True)
    xml_attachment_id = fields.Many2one(
        "ir.attachment", string="Generated XML", readonly=True
    )
    extraction_data = fields.Text(
        string="Extraction JSON",
        help="The JSON Claude extracted. Stored even on a failed extraction so "
        "you can see what was parsed, fix it (e.g. add a missing date) and "
        "re-import without calling the AI again. Editable while failed.",
    )
    move_id = fields.Many2one("account.move", string="Vendor Bill", readonly=True)
    error_message = fields.Text(readonly=True)

    # 🔴 THE INBOX COLUMNS, STORED SO THE LIST CAN SORT,
    # FILTER AND GROUP ON THEM. Derived from `extraction_data`, the one copy
    # of the invoice data every source writes (and an Accountant edits while
    # failed), so they follow it: on an import, on a hand edit and — the ORM
    # computes a new stored field for every row when the add-on is upgraded —
    # on the records that existed before them. A source that knows the
    # invoice before it has read it (APIX's listing) extends the compute.
    seller_name = fields.Char(
        string="Seller",
        compute="_compute_invoice_summary",
        store=True,
        readonly=True,
    )
    invoice_number = fields.Char(
        compute="_compute_invoice_summary",
        store=True,
        readonly=True,
    )
    # finvoice content AC-21: the buyer's order number and the buyer reference
    # the vendor sent, written on import (never back-filled).
    einvoice_order_reference = fields.Char(string="Order number", readonly=True, copy=False)
    einvoice_buyer_reference = fields.Char(string="Buyer reference", readonly=True,
                                           copy=False)
    invoice_date = fields.Date(
        string="Document date",
        compute="_compute_invoice_summary",
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        "res.currency",
        compute="_compute_invoice_summary",
        store=True,
        readonly=True,
    )
    amount_total = fields.Monetary(
        string="Amount",
        currency_field="currency_id",
        compute="_compute_invoice_summary",
        store=True,
        readonly=True,
    )

    @api.depends("extraction_data")
    def _compute_invoice_summary(self):
        for record in self:
            record.update(record._invoice_summary_values(
                lifecycle.parse_extraction(record.extraction_data)
            ))

    def _invoice_summary_values(self, data):
        """The inbox columns from invoice data (a dict, possibly empty)."""
        data = data if isinstance(data, dict) else {}
        seller = data.get("seller") if isinstance(data.get("seller"), dict) else {}
        totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
        amount = totals.get("payable")
        if amount is None:
            amount = totals.get("tax_inclusive")
        try:
            amount = float(amount) if amount not in (None, "") else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        try:
            date = fields.Date.to_date(data.get("issue_date") or None)
        except (TypeError, ValueError):
            date = False
        return {
            "seller_name": _summary_text(seller.get("name")),
            "invoice_number": _summary_text(data.get("invoice_number")),
            "invoice_date": date or False,
            "currency_id": self._summary_currency(data.get("currency")).id,
            "amount_total": amount,
        }

    def _summary_currency(self, code):
        code = (code or "").strip().upper() if isinstance(code, str) else ""
        if not code:
            return self.company_id.currency_id or self.env.company.currency_id
        currency = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "=", code)], limit=1)
        )
        return currency or self.company_id.currency_id or self.env.company.currency_id

    @api.model
    def _enqueue_from_attachment(self, attachment, company, journal=None,
                                 move_type=None, reader=None):
        """Create an import record from a manually-uploaded PDF and enqueue it on
        the same queue the email path uses. Returns the created record.

        ``journal``/``move_type`` capture the journal (and move type) the upload
        targeted, so finalization builds the bill on the SAME journal the user
        chose instead of the company's first purchase journal. The uploaded
        ``ir.attachment`` (created client-side with no parent) is reparented onto
        the new import record so it is the record's PDF. ``reader`` is the
        key of the source that reads it.
        """
        record = self.create(
            {
                "name": attachment.name or "upload.pdf",
                "company_id": company.id,
                "journal_id": journal.id if journal else False,
                "upload_move_type": move_type or False,
                "reader": reader or False,
            }
        )
        attachment.write({"res_model": self._name, "res_id": record.id})
        record.pdf_attachment_id = attachment
        record._enqueue_import(f"AI Invoice Import: {record.name}")
        return record

    # ------------------------------------------------------------------
    # The background job and the status derived from it
    # ------------------------------------------------------------------

    def _enqueue_import(self, description):
        """Enqueue `_process_import` for this record and remember the job.

        The identity key keeps one queued import per record: queue_job
        returns the job already queued under it instead of a second one, and
        that job is the one linked. Written in the caller's transaction, at
        enqueue time — never from inside the job (ADR-0008: no write from a
        second cursor to a row the job transaction also writes)."""
        self.ensure_one()
        # The job runs in the record's company, whatever company the user who
        # queued it had active (queue_job keeps `allowed_company_ids`).
        job = self.with_company(self.company_id).with_delay(
            description=description,
            identity_key=lifecycle.identity_key(self.id),
            channel=AI_IMPORT_CHANNEL,
            **self._import_job_options(),
        )._process_import()
        self._link_job(job)
        return job

    def _import_job_options(self):
        """Extra `with_delay` options of this record's import job. A source
        whose reading is billed limits its retries here."""
        return {}

    def _link_job(self, job):
        """Point `queue_job_id` at ``job`` — a queue_job `Job`, or the
        `queue.job` row queue_job hands back when an identical job is queued
        already. Anything else (a job run at once under
        `queue_job__no_delay`, a test double) links nothing."""
        record = job
        if not isinstance(record, models.BaseModel):
            db_record = getattr(job, "db_record", None)
            record = db_record() if callable(db_record) else None
        if not isinstance(record, models.BaseModel) or record._name != "queue.job":
            return
        if record:
            self.queue_job_id = record[:1].id

    @api.model
    def _active_job_query(self):
        """Jobs of this model that still have work to do, as a subquery.
        Through `sudo`: queue jobs are readable by job managers only, and
        the status of an import is everybody's who may read the import."""
        return self.env["queue.job"].sudo()._search(
            [
                ("model_name", "=", self._name),
                ("state", "in", list(lifecycle.ACTIVE_JOB_STATES)),
            ]
        )

    @api.depends("state", "queue_job_id")
    def _compute_display_state(self):
        now = fields.Datetime.now()
        limit = timedelta(minutes=lifecycle.STALE_PROCESSING_MINUTES)
        for record in self:
            job = record.sudo().queue_job_id
            if not job or job.state not in lifecycle.ACTIVE_JOB_STATES:
                record.display_state = record.state
                record.processing_stale = False
                continue
            record.display_state = lifecycle.STATE_PROCESSING
            since = job.date_started or job.date_enqueued or job.date_created
            record.processing_stale = bool(since and now - since > limit)

    @staticmethod
    def _positive_search(operator, value):
        """``(positive, values)`` for a search method's operator and value.

        Odoo 18.0 hands a field's search method the operator as written, where
        19.0 normalises it to ``in`` first and negates the result itself. This
        does the same normalisation here; ``positive`` is False for ``!=`` and
        ``not in``, whose domain is the negation of the ``in`` one.
        """
        if operator in ("=", "!="):
            return operator == "=", [value]
        if operator in ("in", "not in"):
            return operator == "in", list(value)
        raise UserError(_("Unsupported search operator %s", operator))

    def _search_display_state(self, operator, value):
        positive, value = self._positive_search(operator, value)
        if not positive:
            # Every record has a display state, so "not X" is "one of the
            # others". Negating the SQL instead would drop the rows without
            # a job (NULL), which 19.0's domain negation keeps.
            states = [key for key, _label in self._fields["display_state"].selection]
            value = [state for state in states if state not in value]
        active = self._active_job_query()
        domains = []
        if lifecycle.STATE_PROCESSING in value:
            domains.append([("queue_job_id", "in", active)])
        stored = [v for v in value if v != lifecycle.STATE_PROCESSING]
        if stored:
            domains.append(
                expression.AND(
                    [
                        [("state", "in", stored)],
                        expression.OR(
                            [
                                [("queue_job_id", "=", False)],
                                [("queue_job_id", "not in", active)],
                            ]
                        ),
                    ]
                )
            )
        return expression.OR(domains) if domains else expression.FALSE_DOMAIN

    def _search_processing_stale(self, operator, value):
        positive, value = self._positive_search(operator, value)
        value = {bool(v) for v in value}
        if not positive:
            value = {True, False} - value
        since = fields.Datetime.now() - timedelta(
            minutes=lifecycle.STALE_PROCESSING_MINUTES
        )
        self.env.cr.execute(
            "SELECT id FROM queue_job WHERE model_name = %s AND state IN %s "
            "AND COALESCE(date_started, date_enqueued, date_created) < %s",
            (self._name, lifecycle.ACTIVE_JOB_STATES, since),
        )
        stale_jobs = [row[0] for row in self.env.cr.fetchall()]
        if True in value and False in value:
            return expression.TRUE_DOMAIN
        if True in value:
            return [("queue_job_id", "in", stale_jobs)]
        if False in value:
            return expression.OR(
                [[("queue_job_id", "=", False)], [("queue_job_id", "not in", stale_jobs)]]
            )
        return expression.FALSE_DOMAIN

    def _action_view_imports(self):
        """Return a window action showing this import set (form for one, list for
        many) — used as the manual-upload button's result."""
        action = {
            "type": "ir.actions.act_window",
            "name": _("Incoming invoices"),
            "res_model": self._name,
            "context": dict(self.env.context),
        }
        if len(self) == 1:
            action.update(
                {
                    "view_mode": "form",
                    "views": [(False, "form")],
                    "res_id": self.id,
                }
            )
        else:
            action.update(
                {
                    "view_mode": "list,form",
                    "views": [(False, "list"), (False, "form")],
                    "domain": [("id", "in", self.ids)],
                }
            )
        return action

    def action_retry(self):
        """Manual retry button — re-run the import of a failed record, or of a
        waiting one that has no job (a record reopened after a close, AC-11c).

        Refused while the record's job is queued or running (it shows
        processing): a second job would double-bill Claude.
        """
        self.ensure_one()
        if self.move_id:
            raise UserError(
                _(
                    "A vendor bill (%s) is already linked to this import; retry "
                    "is blocked to avoid creating a duplicate bill."
                )
                % self.move_id.display_name
            )
        if self.display_state == lifecycle.STATE_PROCESSING:
            raise UserError(
                _("This import is being processed right now; retry it once it has finished.")
            )
        if self.state not in (lifecycle.STATE_FAILED, lifecycle.STATE_WAITING):
            raise UserError(
                _("Only failed or waiting imports can be retried (this one is '%s').")
                % self.state
            )
        self.write(
            {
                "state": lifecycle.STATE_WAITING,
                "error_message": False,
                "failure_kind": False,
            }
        )
        self._enqueue_import(f"AI Invoice Import (retry): {self.name}")

    # ------------------------------------------------------------------
    # Recovery actions
    # ------------------------------------------------------------------

    def _check_not_processing(self, action_label):
        for record in self:
            if record.display_state == lifecycle.STATE_PROCESSING:
                raise UserError(
                    _(
                        "%(name)s is being imported right now; %(action)s it once "
                        "the import has finished.",
                        name=record.display_name,
                        action=action_label,
                    )
                )

    def action_open_close_dialog(self):
        """The "Close without bill" button: the dialog that asks why."""
        return {
            "type": "ir.actions.act_window",
            "name": _("Close without bill"),
            "res_model": "goodahead.invoice.import.close",
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {"default_import_ids": [(6, 0, self.ids)]},
        }

    def action_close_without_bill(self, reason):
        """Settle unsettled records without a bill: a confirmed
        duplicate, a test message, an invoice handled by hand. The reason is
        required; who and when are recorded; the source hears of it through
        `_on_settled`."""
        reason = (reason or "").strip()
        if not reason:
            raise UserError(_("Say why the invoice is closed without a bill."))
        settled = self.filtered(lambda r: r.state in lifecycle.SETTLED_STATES)
        if settled:
            raise UserError(
                _(
                    "Only unsettled imports can be closed; %(names)s already "
                    "is imported or closed.",
                    names=", ".join(settled.mapped("display_name")),
                )
            )
        self._check_not_processing(_("close"))
        self.write(
            {
                "state": lifecycle.STATE_CLOSED,
                "close_reason": reason,
                "closed_by_id": self.env.user.id,
                "closed_date": fields.Datetime.now(),
            }
        )
        for record in self:
            record.message_post(
                body=_("Closed without bill: %(reason)s", reason=reason)
            )
        self._on_settled()
        return True

    def action_reopen(self):
        """Undo a close: the record waits again, and
        is imported from what it kept on request — Retry. Nothing is enqueued
        here: for an AI record an import is a paid reading."""
        not_closed = self.filtered(lambda r: r.state != lifecycle.STATE_CLOSED)
        if not_closed:
            raise UserError(
                _(
                    "Only imports closed without bill can be reopened (%(names)s).",
                    names=", ".join(not_closed.mapped("display_name")),
                )
            )
        with_bill = self.filtered("move_id")
        if with_bill:
            raise UserError(
                _(
                    "%(names)s already has a vendor bill linked; reopening would "
                    "import it a second time.",
                    names=", ".join(with_bill.mapped("display_name")),
                )
            )
        for record in self:
            record.message_post(
                body=_(
                    "Reopened; it had been closed without bill: %(reason)s",
                    reason=record.close_reason or "",
                )
            )
            record.write(
                {
                    "state": lifecycle.STATE_WAITING,
                    "close_reason": False,
                    "closed_by_id": False,
                    "closed_date": False,
                    "error_message": False,
                    "failure_kind": False,
                }
            )
        return True

    def action_override_duplicate(self):
        """Import a duplicate-flagged record on purpose: the duplicate
        stop is skipped on the run this starts.

        🔴 A PAID READING IS NOT BOUGHT TWICE. A record whose reader is not a
        structured one (the AI) and that kept its invoice data is imported
        from that data, here and now, exactly as "Re-import from JSON" does —
        the duplicate rule stopped it AFTER the reading was paid for and
        stored. A structured reader's record, or one without stored data, is
        enqueued and read again (reading a structured document costs
        nothing, and its source may have work of its own to do in the job)."""
        not_duplicate = self.filtered(lambda r: r.state != lifecycle.STATE_DUPLICATE)
        if not_duplicate:
            raise UserError(
                _(
                    "Only imports flagged as duplicates can be imported anyway "
                    "(%(names)s).",
                    names=", ".join(not_duplicate.mapped("display_name")),
                )
            )
        self._check_not_processing(_("import"))
        for record in self:
            record.write(
                {
                    "state": lifecycle.STATE_WAITING,
                    "duplicate_override": True,
                    "error_message": False,
                    "failure_kind": False,
                }
            )
            record.message_post(
                body=_("Marked to import although it looks like a duplicate.")
            )
            stored = record._import_override_stored_data()
            if stored is not None:
                record._reimport_from_extraction(stored)
                continue
            record._enqueue_import(f"Invoice Import (duplicate override): {record.name}")
        return True

    def _import_override_stored_data(self):
        """The stored invoice data a duplicate override imports from, or None
        when the record is to be read again (see `action_override_duplicate`)."""
        self.ensure_one()
        spec = self._import_reader_spec()
        if spec and spec.structured:
            return None
        data = lifecycle.parse_extraction(self.extraction_data)
        return data or None

    def action_import_possibly_lost(self):
        """The "Import" button of a possibly-lost record:
        an Accountant decided the invoice is to be billed. Here it waits
        again and its normal import is enqueued; the migration tool
        overrides this with what a possibly-lost package needs."""
        not_lost = self.filtered(lambda r: r.state != lifecycle.STATE_POSSIBLY_LOST)
        if not_lost:
            raise UserError(
                _(
                    "Only imports shown as possibly lost can be imported this way "
                    "(%(names)s).",
                    names=", ".join(not_lost.mapped("display_name")),
                )
            )
        self._check_not_processing(_("import"))
        for record in self:
            record.write(
                {"state": lifecycle.STATE_WAITING, "error_message": False, "failure_kind": False}
            )
            record.message_post(
                body=_(
                    "%(user)s chose to import this possibly lost invoice.",
                    user=self.env.user.display_name,
                )
            )
            record._enqueue_import(f"Invoice Import (possibly lost): {record.name}")
        return True

    def _action_open_move(self, move):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
            "views": [(False, "form")],
        }

    def action_open_bill(self):
        """Smart button of an imported record: its bill."""
        return self._action_open_move(self.move_id)

    def action_open_duplicate_bill(self):
        """Smart button of a duplicate record: the bill it duplicates."""
        return self._action_open_move(self.duplicate_of_move_id)

    def _on_settled(self):
        """Called when records become settled without the import doing it
        (closed without bill). No-op here; APIX receiving overrides it to tell
        APIX the package was received."""
        return None

    @api.constrains("state", "close_reason")
    def _check_close_reason(self):
        for record in self:
            if record.state == lifecycle.STATE_CLOSED and not (record.close_reason or "").strip():
                raise ValidationError(
                    _("An import closed without bill needs a reason.")
                )

    def _process_import(self):
        """Main import pipeline — called via queue_job.

        Runs in a SINGLE queue_job transaction: read the record, call Claude
        once, write the result, return. It must NOT ``self.env.cr.commit()`` — a
        mid-job commit splits the transaction queue_job manages, so the job's
        final-state write then collides with the jobrunner on the ``queue_job``
        row ('could not serialize access'). queue_job *postpones* serialization
        errors (they don't count against ``max_retries``), so the job reschedules
        unbounded and re-runs this paid call every time — the $100 retry storm.
        With no mid-job commit, an extraction failure just ``return``s "FAILED"
        (the job is marked ``done``, not retried) → exactly one Claude call.
        """
        self.ensure_one()

        # Never reprocess a finished record, or one that already produced a bill
        # (a retry must not re-call Claude or create a duplicate bill).
        #
        # 🔴 THIS GUARD COMES FIRST, AND THAT ORDER IS LOAD-BEARING SINCE
        # 19.0.3.21.0. The core-profile refusal below now RECORDS itself with
        # `_fail`; in front of this guard it would rewrite the state of a record
        # that has already produced a vendor bill, turning a finished import
        # into a failed one over a core profile, for a run this method is about
        # to decline anyway. Nothing is lost by refusing after it: a finished
        # record makes no paid call and creates no bill, so there is no vendor
        # for an unmeasured core to invent.
        if self.state in lifecycle.SETTLED_STATES or self.move_id:
            return "OK: already processed"

        # 🔴 BEFORE the paid call, not after it. On a core this module has not
        # been measured against the vendor-matching hooks are silent no-ops, so
        # the extraction would be billed and then produce a duplicate vendor.
        # Inert on both measured builds; see models/core_profile.py.
        #
        # 🔴 AND THE REFUSAL IS RECORDED ON THE RECORD, NOT ONLY RAISED (the dual-core design
        # review, finding F3). It used to raise straight out of here with
        # nothing around it, so queue_job rolled the transaction back and left
        # the import in `draft` with an empty `error_message`: a person opening
        # it saw a draft with no reason, and `action_retry` — which requires
        # `failed` — could not be used once the core was fixed. The SAME
        # refusal raised from `_ai_decode_ubl_into_move` has always been caught
        # by `_finalize_import` and recorded through `_fail`, so one refusal had
        # two outcomes depending on which seam it fired at.
        #
        # ⚠️ THIS IS NOT THE DEGRADED MODE `core_profile.py:49-52` FORBIDS, and
        # that is the whole of the judgement. A degraded mode would IMPORT on an
        # unmeasured core. `_fail` imports nothing — it writes `failed` plus the
        # refusal text and returns, exactly as the three terminal refusals below
        # it already do (no API key, no PDF, a failed extraction). The method's
        # own docstring names this as the reason the check belongs here at all:
        # *"there is a human present, A RECORD TO WRITE THE ERROR ON, and a bill
        # that has not yet been created"* — and until now nothing on this path
        # wrote it.
        #
        # ⚠️ WHAT MOVES, SAID RATHER THAN LEFT TO BE NOTICED: the failure stops
        # being a failed `queue.job` row and becomes a failed `ai.invoice.import`
        # row. That is the same trade every other terminal failure in this
        # method already makes, and it is the direction that puts the reason in
        # front of the person who uploaded the PDF.
        try:
            self.env["account.move"]._ai_assert_core_profile()
        except UserError as e:
            self._fail("import.core_unknown", str(e))
            return f"FAILED: {e}"

        # 🔴 WHAT READS THE DOCUMENT IS THE SOURCE'S BUSINESS (core/source split).
        # The guards above are every source's; the reading, and the checks
        # that only make sense for one kind of reading (an API key, a PDF),
        # live in the add-on that reads, behind `_source_read`.
        return self._import_read_and_finalize()

    def _import_read_and_finalize(self):
        """Read the document with the record's reader, then finalize.

        🔴 DISPATCHED ON THE STORED `reader`, NOT OVERRIDDEN BY EACH
        SOURCE. Two add-ons overriding this one method, one of them without
        ``super()``, made the module load order decide which reader a record
        got. Source add-ons implement `_source_read` for their own key and
        pass every other key to ``super()``; nothing here depends on the MRO.
        The core alone has no reader, so a record that reaches it fails with
        a reason rather than sitting in waiting."""
        self.ensure_one()
        reader = self._import_reader()
        if not reader:
            self._fail("import.unsupported_format", "No reader is installed for this import")
            return "FAILED: No reader"
        if self.reader != reader:
            # A record created before readers were stored, or by hand: the
            # reader that claimed it is written so the next run asks nobody.
            self.reader = reader
        return self._source_read(reader)

    # ------------------------------------------------------------------
    # Source dispatch: readers registered by the source add-ons
    # ------------------------------------------------------------------

    @api.model
    def _import_readers(self):
        """The installed readers, ``{key: SourceReader}``. Each source add-on
        adds its own and calls ``super()``; the core has none."""
        return {}

    @api.model
    def _import_sources_in_order(self):
        """The reader keys in the order the core asks them (priority, then
        key) — the same whatever order the add-ons were loaded in."""
        return readers_in_order(self._import_readers())

    @api.model
    def _import_source_priority(self, source_key):
        """The priority of ``source_key`` (lower is asked first), or None for
        a key no installed add-on registers."""
        reader = self._import_readers().get(source_key)
        return reader.priority if reader else None

    @api.model
    def _source_accepts_attachment(self, source_key, attachment):
        """Whether ``source_key`` reads ``attachment``, a mail file
        ``(filename, bytes, mimetype)``, as a document of its own. Source
        add-ons answer for their key and pass every other key to
        ``super()``."""
        return False

    @api.model
    def _source_claim_companions(self, source_key, documents, pool):
        """The files of ``pool`` that go WITH ``documents`` (the files
        ``source_key`` accepted) rather than being read on their own — a
        Finvoice file's PDF image. Asked in priority order, so a reader asked
        earlier claims a file before a later reader could read it. Returns a
        list of items of ``pool`` (the same tuples)."""
        return []

    @api.model
    def _source_receive_mail(self, source_key, files, companions, company,
                             values, msg_dict):
        """Create the import records of the ``files`` ``source_key``
        accepted from one mail, with the ``companions`` it claimed, and
        enqueue them. ``values`` are the record values of the mail
        (``source_email`` and an alias's own defaults); ``company`` is the
        alias's company. Returns the records (the first one carries the
        mail). Source add-ons implement it for their key."""
        return self.browse()

    def _source_read(self, source_key):
        """Read this record with ``source_key`` and import it; the job's
        result text. Source add-ons implement it for their key."""
        self.ensure_one()
        self._fail("import.unsupported_format", "No reader is installed for this import")
        return "FAILED: No reader"

    def _source_claims_record(self, source_key):
        """Whether ``source_key`` reads this record although its ``reader``
        is not set — a record from before T9b, or made by hand."""
        return False

    def _source_bill_attachments(self, source_key):
        """``(main_attachment, extra_attachments)`` to put on this record's
        bill besides its PDF (see `_import_build_bill`). Nothing in the
        core."""
        empty = self.env["ir.attachment"]
        return empty, empty

    def _import_reader(self):
        """The key of this record's reader: the stored one, else the first
        installed reader, in priority order, that claims the record. False
        when nobody reads it."""
        self.ensure_one()
        readers = self._import_readers()
        if self.reader and self.reader in readers:
            return self.reader
        for key in readers_in_order(readers):
            if self._source_claims_record(key):
                return key
        return False

    def _import_reader_spec(self):
        """The `SourceReader` of this record, or None."""
        return self._import_readers().get(self._import_reader())

    def _import_decode_context(self):
        """Context for decoding this record's bill. A structured reader's
        lines read their units through the shared unit codes
        (``uom.uom._einvoice_unit_for_code``, send ADR-0013, AC-51); an AI
        reading keeps the ERP's own unit lookup."""
        spec = self._import_reader_spec()
        if spec and spec.structured:
            return {EINVOICE_UNIT_CODES_CONTEXT_KEY: True}
        return {}

    def _import_reader_policy(self):
        """The matching policy of this record's reader — the policy it is
        imported AND re-imported under. The default policy for a
        record no reader claims."""
        spec = self._import_reader_spec()
        policy = spec.policy if spec else DEFAULT_POLICY
        if self.source == "email" and not policy.contact_details_need_review:
            # Anybody can send mail: the contact details of a mailed document
            # wait for a person whatever its format (security fix).
            policy = dataclasses.replace(policy, contact_details_need_review=True)
        return policy

    # ------------------------------------------------------------------
    # Mail intake: one door, the readers asked in priority order
    # ------------------------------------------------------------------

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """A mail reaching an alias of this model.

        🔴 THE CORE OWNS THE MAIL DOOR. Each attachment goes to the
        first installed reader, in priority order, that accepts it
        (`_source_accepts_attachment`); each reader may then claim the
        companions of its documents (`_source_claim_companions` — a
        Finvoice file's PDF image); each reader creates its records
        (`_source_receive_mail`). Files nobody takes are named in the
        chatter of the mail's first record. When no reader takes anything,
        one failed record says the format is not supported
        (``import.unsupported_format``) and keeps the files. Inline parts
        (a signature's logo) are not attachments.

        🔴 THE ALIAS DECIDES THE COMPANY, NOT THE INCOMING MAIL SERVER.
        `fetchmail.server` has no company, and ``env.company`` here is the
        scheduler user's, the same for every mailbox. One alias per company,
        each with ``{'company_id': <id>}`` in its defaults, which core passes
        here as ``custom_values``. A company-less alias files into the
        current company and the record says it was a fallback, because a
        bill in the wrong chart of accounts resolves, posts and complains
        about nothing. Returns the mail's first record."""
        company, values, alias_named_company = self._mail_intake_values(
            msg_dict, custom_values
        )
        if not alias_named_company:
            _logger.warning(
                "Invoice import: the alias that received mail from %s names "
                "no company, so the bill will be filed in %s. With more than "
                "one company this is how bills reach the wrong chart of "
                "accounts — give each company its own alias and set "
                "`alias_defaults` to {'company_id': <id>}. See docs/SETUP.md.",
                msg_dict.get("email_from", "?"),
                company.display_name,
            )
        files = self._mail_intake_files(msg_dict)
        routes, unaccepted = self._mail_route_files(files)
        records = self.browse()
        for source_key, route in routes.items():
            records |= self._source_receive_mail(
                source_key,
                route["files"],
                route["companions"],
                company,
                dict(values),
                msg_dict,
            )
        if not records:
            return self._mail_refuse(msg_dict, company, values, files)

        first = records[:1]
        self._mail_post_company_note(first, alias_named_company)
        if unaccepted:
            first.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "This email also carried files that no installed import "
                    "reads; they were not imported: %s",
                    ", ".join(name for name, _content, _mimetype in unaccepted),
                )
            )
        return first

    @api.model
    def _mail_intake_values(self, msg_dict, custom_values):
        """``(company, values, alias_named_company)`` of a mail: the alias's
        ``company_id`` default, else the current company; the record values
        an alias may preset (fields of this model only, never the source,
        name, state or reader, which the intake decides)."""
        values = {"source_email": msg_dict.get("email_from") or ""}
        company = self.env.company
        alias_named_company = False
        for key, value in (custom_values or {}).items():
            if key == "company_id":
                named = self.env["res.company"].browse(value).exists() if value else None
                if named:
                    company = named
                    alias_named_company = True
                continue
            if key in ("source", "name", "state", "reader") or key not in self._fields:
                continue
            values[key] = value
        return company, values, alias_named_company

    @api.model
    def _mail_intake_files(self, msg_dict):
        """The mail's attachments as ``(filename, bytes, mimetype)``, inline
        parts left out."""
        files = []
        for attachment in msg_dict.get("attachments") or []:
            name = attachment[0] if len(attachment) > 0 else ""
            content = attachment[1] if len(attachment) > 1 else b""
            info = (
                attachment[2]
                if len(attachment) > 2 and isinstance(attachment[2], dict)
                else {}
            )
            if info.get("cid"):
                continue
            if isinstance(content, str):
                # The mail parser hands text parts (text/xml) over decoded;
                # encoding them back with the part's own charset restores the
                # bytes the sender wrote, declaration and all.
                content = content.encode(info.get("encoding") or "utf-8", "replace")
            content = content or b""
            files.append((name or "attachment", content, guess_mimetype(content)))
        return files

    @api.model
    def _mail_route_files(self, files):
        """Route a mail's ``files`` to the readers.

        Returns ``(routes, unaccepted)``: ``routes`` maps each reader key
        that got something, in priority order, to ``{"files": [...],
        "companions": [...]}``; ``unaccepted`` lists the files nobody
        takes. Pure dispatch — nothing is created."""
        order = self._import_sources_in_order()
        owner = {}
        for index, file in enumerate(files):
            for source_key in order:
                if self._source_accepts_attachment(source_key, file):
                    owner[index] = source_key
                    break
        routes = {}
        taken = set()
        for source_key in order:
            mine = [i for i in range(len(files)) if owner.get(i) == source_key and i not in taken]
            if not mine:
                continue
            taken.update(mine)
            pool = [i for i in range(len(files)) if i not in taken]
            claimed = self._source_claim_companions(
                source_key, [files[i] for i in mine], [files[i] for i in pool]
            )
            companions = [i for i in pool if any(files[i] is item for item in claimed)]
            taken.update(companions)
            routes[source_key] = {
                "files": [files[i] for i in mine],
                "companions": [files[i] for i in companions],
            }
        unaccepted = [files[i] for i in range(len(files)) if i not in taken]
        return routes, unaccepted

    @api.model
    def _mail_refuse(self, msg_dict, company, values, files):
        """No installed reader takes anything of the mail:
        one failed record that keeps the files and says so."""
        values = dict(values)
        values["name"] = msg_dict.get("subject") or _("Email without an importable attachment")
        record = self._create_for_source("email", company, files, values)
        labels = dict(self._fields["reader"]._description_selection(self.env))
        readable = ", ".join(
            labels.get(key) or key for key in self._import_sources_in_order()
        )
        if files:
            message = _(
                "Format not supported: %(files)s. %(readers)s",
                files=", ".join(name for name, _c, _m in files),
                readers=(
                    _("The installed imports read: %s.", readable)
                    if readable
                    else _("No import source is installed.")
                ),
            )
        else:
            message = _("Format not supported: the email has no attachment to import.")
        record._fail("import.unsupported_format", message)
        return record

    @api.model
    def _mail_post_company_note(self, record, alias_named_company):
        """Say which books a mailed invoice landed in, and how that was
        decided — a company chosen by fallback says so."""
        if alias_named_company:
            body = _(
                "Received by email and filed under %(company)s, as set by "
                "the alias.",
                company=record.company_id.display_name,
            )
        else:
            body = _(
                "Received by email and filed under %(company)s. ⚠️ The alias "
                "that received it names no company, so this is a fallback, "
                "not a decision — with more than one chart of accounts, give "
                "each company its own alias and set its Default Values to "
                "{'company_id': <id>}. The incoming mail server does not "
                "decide this.",
                company=record.company_id.display_name,
            )
        record.message_post(body=Markup("<p>%s</p>") % body)

    def _import_extraction_vals(self, ai_result, extracted):
        """The values `_finalize_import` persists from a read, before the bill
        is built. A source adds its own (the AI add-on: model and token
        usage)."""
        vals = {
            "extraction_data": json.dumps(extracted, indent=2, ensure_ascii=False),
        }
        # The numbers the duplicate rule looks the record up by.
        vals.update(lifecycle.invoice_identity_values(extracted))
        return vals

    def _import_before_reimport(self, extracted):
        """Hook for `action_reimport_from_json`, run on the stored extraction
        before the UBL is generated from it. Mutates ``extracted`` in place.
        Nothing generic to do; the AI add-on re-asks the PDF here."""
        return None

    def _import_reimport_source_label(self, extracted):
        """The source named in a re-imported bill's chatter. The core has no
        AI reading, so it is always the record's source; the AI add-on answers
        None (its own note) for a PDF it read."""
        return self._import_source_label()

    def _import_source_label(self):
        """The record's source as a person reads it ("Upload", "APIX"...)."""
        self.ensure_one()
        selection = dict(
            self._fields["source"]._description_selection(self.env)
        )
        return selection.get(self.source) or self.source or _("an invoice document")

    def _import_reimport_meta(self):
        """The ``usage`` and ``model_name`` a re-import reports, since no read
        happens on that path. The AI add-on reports the stored ones; a
        structured reader reports none (AC-21: no AI usage, ever)."""
        spec = self._import_reader_spec()
        if spec and spec.structured:
            return {"usage": None, "model_name": None}
        return {
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "model_name": "manual-reimport",
        }

    # ------------------------------------------------------------------
    # The source interface (contracts/public-api.md §1, the core/source split)
    # ------------------------------------------------------------------

    @api.model
    def _create_for_source(self, source, company, files, values=None):
        """Create the import record of one received document set, in
        ``waiting``, with ``files`` attached. Returns the new record (a
        recordset of one).

        ``source`` is a value of the ``source`` selection (``upload``,
        ``email``; source add-ons add theirs). ``files`` is a list of
        ``(filename, bytes, mimetype)``; the first PDF among them becomes the
        record's ``pdf_attachment_id``. ``values`` may set ``journal_id``,
        ``upload_move_type``, ``source_email``, ``name`` and a source add-on's
        own fields.

        Nothing is enqueued and nothing is read here: the caller decides when
        the import runs (a source's job, `_enqueue_import`, or at once)."""
        files = list(files or [])
        vals = dict(values or {})
        vals.update({"source": source, "company_id": company.id})
        vals.setdefault("state", lifecycle.STATE_WAITING)
        if not vals.get("name"):
            vals["name"] = files[0][0] if files else source
        record = self.with_company(company).create(vals)
        Attachment = self.env["ir.attachment"]
        for filename, content, mimetype in files:
            attachment = Attachment.create(
                {
                    "name": filename,
                    "raw": content or b"",
                    "mimetype": mimetype or "application/octet-stream",
                    "res_model": self._name,
                    "res_id": record.id,
                }
            )
            if not record.pdf_attachment_id and (
                attachment.mimetype == "application/pdf"
                or (filename or "").lower().endswith(".pdf")
            ):
                record.pdf_attachment_id = attachment
        return record

    def _import_from_invoice_data(self, invoice_data, policy=None, document_xml=None,
                                  main_attachment=None, extra_attachments=None):
        """Import a bill from invoice data a source has already read.
       Returns an `ImportResult`.

        ``main_attachment`` (an `ir.attachment`, e.g. the Finvoice message)
        and ``extra_attachments`` (the package's other files) are copied onto
        the bill; see `_import_build_bill` for which one becomes its main
        document.

        Validates ``invoice_data`` with the core validator (spec §6
        tolerance), runs the duplicate rule, generates the standard e-invoice
        document — or takes ``document_xml``, a document the source already
        generated from the same data — decodes it into a draft bill under the
        source's matching ``policy`` (§1.3; None = the default policy) and
        post-processes it. No PDF and no AI is needed. Never commits; never
        creates or overwrites a vendor.

        The record is left ``imported``, ``duplicate`` or ``failed`` to match
        the result; a failure stores the data so an Accountant can fix it and
        re-import without the source reading anything again."""
        self.ensure_one()
        policy = policy or DEFAULT_POLICY
        Move = self.env["account.move"]
        if self.move_id:
            return ImportResult(
                OUTCOME_IMPORTED,
                move=self.move_id,
                duplicate_of=Move,
                detail="OK: already processed",
            )
        if self.state in lifecycle.SETTLED_STATES:
            # Closed without bill: nothing more happens to it.
            return ImportResult(
                OUTCOME_FAILED,
                move=Move,
                duplicate_of=Move,
                code="import.already_settled",
                message=_("This import is already settled (%s).", self.state),
                detail="FAILED: already settled",
            )
        try:
            Move._ai_assert_core_profile()
        except UserError as e:
            return self._import_failed("import.core_unknown", str(e), f"FAILED: {e}")

        data = json.loads(json.dumps(invoice_data)) if invoice_data is not None else None
        if isinstance(data, dict):
            # Marks the stored data as a structured source's, so a re-import
            # names the source rather than an AI reading.
            data.setdefault("source_meta", {})
        stored = {}
        if isinstance(data, dict):
            stored = {
                "extraction_data": json.dumps(data, indent=2, ensure_ascii=False),
                **lifecycle.invoice_identity_values(data),
            }
        try:
            self._import_validator()._validate_extraction(data)
        except InvoiceTotalsError as e:
            return self._import_failed(
                "import.totals_mismatch", str(e), f"FAILED: {e}", extra_vals=stored
            )
        except InvoiceDataError as e:
            return self._import_failed(
                "import.invalid_data", str(e), f"FAILED: {e}", extra_vals=stored
            )

        # 🔴 THE DUPLICATE RULE RUNS BEFORE THE DOCUMENT IS GENERATED (T5,
        # ADR-0009): a duplicate costs nothing more than its lookup. The bill
        # builder runs it as well, for the sources that come in through
        # `_finalize_import`; here it has already run.
        stop = self._import_duplicate_stop(data, policy, extra_vals=stored)
        if stop:
            return stop

        xml_bytes = document_xml
        if not xml_bytes:
            try:
                # AC-29, AC-31: a stated document is generated so the bill
                # carries its rows at their stated totals, nothing added.
                precision = self.env["decimal.precision"].precision_get
                document_data = stated_document_for_the_bill(
                    data, precision("Product Price"),
                    discount_digits=precision("Discount"),
                    import_discount=finvoice_rows.import_discount,
                )
                xml_bytes = Move._ai_generate_ubl_from_extracted(document_data, self.company_id)
            except Exception as e:
                _logger.exception("UBL generation failed for import %s", self.id)
                return self._import_failed(
                    "import.invalid_data",
                    f"UBL generation failed: {e}",
                    f"FAILED: {e}",
                    extra_vals=stored,
                )
        return self._import_build_bill(
            data,
            xml_bytes,
            policy=policy,
            source_label=self._import_source_label(),
            check_duplicate=False,
            main_attachment=main_attachment,
            extra_attachments=extra_attachments,
        )

    def _import_validator(self):
        """The validator `_import_from_invoice_data` runs: the AI reading's
        checks and tolerance, minus the country-currency inference (a
        structured document states its currency)."""
        return SourceDataValidator(
            currency_is_active=self.env["account.move"]._ai_currency_is_active
        )

    # ------------------------------------------------------------------
    # The duplicate rule (spec AC-22, ADR-0009, the core/source split)
    # ------------------------------------------------------------------

    def _import_duplicate_stop(self, invoice_data, policy=None, extra_vals=None):
        """Run the duplicate rule for this record. Returns an `ImportResult`
        with outcome ``duplicate`` — the record flagged, no bill — or None
        when the import may go on.

        The vendor is resolved here, under the source's ``policy``, with the
        same matcher the decoder uses, and never created. Skipped once an
        Accountant has overridden the flag (``duplicate_override``)."""
        self.ensure_one()
        if self.duplicate_override or not isinstance(invoice_data, dict):
            return None
        vendor = self.env["res.partner"]._ai_vendor_decision(
            invoice_data.get("seller"),
            company=self.company_id,
            policy=policy or DEFAULT_POLICY,
        )["vendor"]
        duplicate_of = self._import_check_duplicate(invoice_data, vendor)[:1]
        if not duplicate_of:
            return None
        vals = dict(extra_vals or {})
        vals.update(
            {
                "state": lifecycle.STATE_DUPLICATE,
                "duplicate_of_move_id": duplicate_of.id,
                "error_message": False,
                "failure_kind": False,
            }
        )
        self.write(vals)
        self.message_post(
            body=Markup(
                _(
                    "Not imported: this invoice looks like a duplicate of "
                    "%(bill)s, which has the same vendor and invoice number. "
                    "Close it without a bill, or import it anyway if it really "
                    "is a second invoice."
                )
            )
            % {"bill": duplicate_of._get_html_link()}
        )
        _logger.info(
            "Invoice Import %s [import.duplicate]: duplicate of account.move %s",
            self.id,
            duplicate_of.id,
        )
        return ImportResult(
            OUTCOME_DUPLICATE,
            move=self.env["account.move"],
            duplicate_of=duplicate_of,
            code="import.duplicate",
            message=_("Duplicate of %s", duplicate_of.display_name),
            detail=f"DUPLICATE: {duplicate_of.display_name}",
        )

    def _import_check_duplicate(self, invoice_data, vendor):
        """The existing bill this invoice duplicates, or an empty
        `account.move`.

        A duplicate is a bill (or a credit note — the same kind as
        ``invoice_data["type"]``) of this record's company that is not
        cancelled, whose reference equals the invoice number ignoring case,
        spaces and separators, and that belongs to ``vendor``'s company —
        or, when no vendor matched, to a partner carrying the seller's VAT
        number or Business ID. An earlier import record of the same company,
        imported, with the same number and seller, whose bill is not
        cancelled, counts as well (its bill's reference may differ).

        ``vendor`` is the vendor the source's matching policy resolves the
        seller to (possibly empty). Not called when the record carries
        ``duplicate_override``."""
        self.ensure_one()
        Move = self.env["account.move"]
        data = invoice_data if isinstance(invoice_data, dict) else {}
        number = lifecycle.normalize_invoice_number(data.get("invoice_number"))
        if not number:
            return Move
        kind = "in_refund" if data.get("type") == "CreditNote" else "in_invoice"
        vendor = vendor.commercial_partner_id[:1] if vendor else vendor
        seller = data.get("seller") if isinstance(data.get("seller"), dict) else {}
        vat = lifecycle.normalize_vat(seller.get("vat"))
        registry = self.env["res.partner"]._registry_variants(
            seller.get("business_id")
        )
        if not vendor and not vat and not registry:
            return Move
        found = self._import_duplicate_bill(number, kind, vendor, vat, registry)
        if found:
            return Move.browse(found)
        return self._import_duplicate_earlier_record(number, kind, vendor, vat, registry)

    def _import_duplicate_bill(self, number, kind, vendor, vat, registry):
        """The id of an existing bill by normalised reference, or None.

        🔴 THE REFERENCE EXPRESSION IS SPELLED EXACTLY AS THE INDEX
        `goodahead_invoice_import_move_ref_norm_idx` (hooks.py), and the
        index's own predicate is repeated, so PostgreSQL can use it: any other
        spelling is a sequential scan of every journal entry."""
        self.env["account.move"].flush_model(
            ["company_id", "move_type", "ref", "state", "partner_id"]
        )
        self.env["res.partner"].flush_model(
            ["vat", "company_registry", "commercial_partner_id"]
        )
        self.env.cr.execute(
            self._import_duplicate_bill_query(number, kind, vendor, vat, registry)
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _import_duplicate_bill_query(self, number, kind, vendor, vat, registry):
        """The `SQL` of `_import_duplicate_bill` (separate so a test can
        EXPLAIN it)."""
        if vendor:
            partner_clause = SQL("cp.id = %s", vendor.id)
        else:
            clauses = []
            if vat:
                # `vat` is normalised (`import_record.normalize_vat`); the
                # partners' VAT is normalised the same way here, so
                # "FI 2345 6789" on the contact matches "FI23456789".
                clauses.append(
                    SQL(
                        "upper(replace(p.vat, ' ', '')) = %s"
                        " OR upper(replace(cp.vat, ' ', '')) = %s",
                        vat,
                        vat,
                    )
                )
            if registry:
                registry = tuple(registry)
                clauses.append(
                    SQL(
                        "p.company_registry IN %s OR cp.company_registry IN %s",
                        registry,
                        registry,
                    )
                )
            partner_clause = SQL("(%s)", SQL(" OR ").join(clauses))
        return SQL(
            """
            SELECT m.id
              FROM account_move m
              JOIN res_partner p ON p.id = m.partner_id
              JOIN res_partner cp ON cp.id = p.commercial_partner_id
             WHERE m.company_id = %(company)s
               AND m.move_type IN ('in_invoice', 'in_refund')
               AND upper(regexp_replace(m.ref, '[^A-Za-z0-9]', '', 'g')) = %(number)s
               AND m.move_type = %(kind)s
               AND m.state != 'cancel'
               AND %(partner)s
             ORDER BY m.id
             LIMIT 1
            """,
            company=self.company_id.id,
            number=number,
            kind=kind,
            partner=partner_clause,
        )

    def _import_duplicate_earlier_record(self, number, kind, vendor, vat, registry):
        """The bill of an earlier, imported record of the same invoice — the
        case where that bill's reference was changed by hand."""
        seller_domain = []
        if vat:
            # Stored normalised since 19.0.1.8.0 (the migration normalises
            # the records from before), so this is an equality.
            seller_domain.append([("seller_vat", "=", vat)])
        if registry:
            seller_domain.append([("seller_business_id", "in", list(registry))])
        if vendor:
            seller_domain.append(
                [("move_id.partner_id.commercial_partner_id", "=", vendor.id)]
            )
        earlier = (
            self.sudo()
            .with_context(active_test=False)
            .search(
                expression.AND(
                    [
                        [
                            ("id", "!=", self.id),
                            ("company_id", "=", self.company_id.id),
                            ("state", "=", lifecycle.STATE_IMPORTED),
                            ("invoice_number_normalized", "=", number),
                            ("move_id.state", "!=", "cancel"),
                            ("move_id.move_type", "=", kind),
                        ],
                        expression.OR(seller_domain),
                    ]
                ),
                order="id",
                limit=1,
            )
        )
        return self.env["account.move"].browse(earlier.move_id.ids)

    def _import_failed(self, code, message, detail, extra_vals=None):
        """`_fail` the record and say so as an `ImportResult`."""
        self._fail(code, message, extra_vals=extra_vals)
        Move = self.env["account.move"]
        return ImportResult(
            OUTCOME_FAILED,
            move=Move,
            duplicate_of=Move,
            code=code,
            message=message,
            detail=detail,
        )

    def _finalize_import(self, ai_result):
        """Steps 2-4 for a source that has already read the document: persist
        the extraction, create the vendor bill, post-process.

        Shared by the Claude path (`_import_read_and_finalize`) and the
        re-import-from-JSON recovery action (which reuses stored, possibly
        hand-fixed, data with no new API call). Returns the OK/FAILED result
        string a queue job reports.

        🔴 SINCE THE CORE/SOURCE SPLIT THIS IS A THIN WRAPPER around `_import_build_bill`,
        the body every source shares; `_import_from_invoice_data` is the other
        door into it. It runs under the DEFAULT matching policy — the AI
        source's — and does NOT re-validate: the AI reading validated already,
        and a hand-edited re-import is deliberately not re-asked. The
        duplicate rule DOES run: a duplicate PDF is flagged,
        not billed a second time.

        🔴 SINCE T9b THE POLICY IS THE READER'S, not always the default: a
        re-imported Finvoice record is matched under the strict policy it was
        first imported under, and its bill gets the reader's files
        (`_source_bill_attachments`) exactly as on the first import. An AI
        record's reader policy IS the default, so its path is unchanged.
        """
        self.ensure_one()
        result = self._import_build_bill(
            ai_result["extracted"],
            ai_result["xml_bytes"],
            usage=ai_result["usage"],
            model_name=ai_result["model_name"],
            meta=ai_result,
            policy=ai_result.get("policy") or self._import_reader_policy(),
            source_label=ai_result.get("source_label"),
            main_attachment=ai_result.get("main_attachment"),
            extra_attachments=ai_result.get("extra_attachments"),
        )
        return result.detail

    def _import_build_bill(self, extracted, xml_bytes, usage=None,
                           model_name=None, meta=None, policy=None,
                           source_label=None, check_duplicate=True,
                           main_attachment=None, extra_attachments=None):
        """Persist the extraction, build the draft bill from ``xml_bytes``,
        post-process. Returns an `ImportResult`. Never commits; never creates
        a vendor.

        Files on the bill, all copied BEFORE the decoder runs: the
        record's PDF first, as the main document; then ``main_attachment``
        (the source's own document, e.g. a Finvoice message), which becomes
        the main document only when there was no PDF; then
        ``extra_attachments``.

        ``usage``/``model_name`` are an AI reading's (None for a structured
        source); ``meta`` is what `_import_extraction_vals` sees (the AI
        source's whole result dict); ``policy`` the source's `MatchingPolicy`
        (None = default); ``source_label`` names a non-AI source in the
        chatter. ``check_duplicate`` False means the caller has run
        the duplicate rule already (`_import_from_invoice_data` does, before
        it generates the document).
        """
        self.ensure_one()
        company = self.company_id
        AccountMove = self.env["account.move"]
        policy = policy or DEFAULT_POLICY
        if self.source == "email" and not policy.contact_details_need_review:
            # A source that passes its reader's policy explicitly gets the
            # mail rule too (see `_import_reader_policy`).
            policy = dataclasses.replace(policy, contact_details_need_review=True)
        if (
            not policy.is_legacy
            or policy.bank_account_lookup_only
            or policy.contact_details_need_review
        ):
            # Both core pipelines, `res.partner.bank` and the post-processing
            # read it from here; the default policy puts nothing on the
            # context, so the AI path runs exactly as it did.
            AccountMove = AccountMove.with_context(
                **{MATCHING_POLICY_CONTEXT_KEY: policy}
            )
        decode_context = self._import_decode_context()
        if decode_context:
            AccountMove = AccountMove.with_context(**decode_context)
        if meta is None:
            meta = {"usage": usage, "model_name": model_name}

        # 🔴 THE BACK CATALOGUE HEALS ITSELF — BUT NOT HERE, SINCE 19.0.3.20.0
        # (found in review). 19.0.3.19.0 recomputed the text-layer verdict at this
        # point. That was half a repair and it was the wrong half of the
        # method: the only consumer on the re-import path is
        # `_ai_surviving_currency_marker`, which returns at once when the JSON
        # carries no marker, so the recompute could DROP a wrong marker and
        # never ADD a missing one — and the missing case is the very bill the
        # feature was written for. The resolver has to be re-run as well, and it has to
        # run BEFORE the UBL is generated, which happens in
        # `action_reimport_from_json` and not in here. See
        # `account.move._ai_reask_the_pdf`.
        #
        # Nothing is done on the forward path either way: `extract_invoice`
        # has already written the verdict and run the resolver.

        # 🔴 BEFORE THE JSON IS PERSISTED, NOT AFTER. The stored
        # extraction may be a HAND-EDITED one arriving back through
        # `action_reimport_from_json`, which never re-runs `_validate_extraction`
        # and therefore never re-asks the three questions behind
        # `currency_inferred_from_country`. `_ai_surviving_currency_marker` asks
        # them against the data as it is now and DROPS the key when it no longer
        # holds — here, so the claim does not survive into `extraction_data` or
        # the `extraction.json` attachment and get inherited by the next
        # re-import. It is idempotent; `_ai_post_process` asks again, for any
        # caller that does not come through this method.
        AccountMove._ai_surviving_currency_marker(extracted)

        # Persist the extraction first; it is flushed before the savepoint below
        # (so it survives a rollback), keeping it visible + re-importable even if a
        # later step fails.
        extraction_vals = self._import_extraction_vals(meta, extracted)
        self.write(extraction_vals)

        # 🔴 THE DUPLICATE RULE, FOR EVERY SOURCE — INCLUDING A PDF READ BY
        # THE AI (spec AC-22, AC-26's one deliberate change; ADR-0009). After
        # the extraction is persisted (a paid reading is kept, and an
        # override re-imports from it), before any bill exists. Until
        # the core/source split a duplicate PDF became a second draft bill carrying core's
        # non-blocking "same reference" warning.
        if check_duplicate:
            stop = self._import_duplicate_stop(extracted, policy)
            if stop:
                return stop

        # Build on the journal the upload targeted; only the email path (no stored
        # journal) falls back to the company's first purchase journal. move_type
        # follows the upload too (the UBL decoder still corrects invoice vs refund
        # from the document content afterwards).
        journal = self.journal_id
        if not journal:
            journal = self.env["account.journal"].search(
                [("type", "=", "purchase"), ("company_id", "=", company.id)],
                limit=1,
            )
        if not journal:
            return self._import_failed(
                "import.no_journal",
                "No purchase journal found",
                "FAILED: No purchase journal",
            )
        move_type = self.upload_move_type or "in_invoice"

        # Steps 2-3: create a fresh vendor bill and decode the generated UBL
        # DIRECTLY into it (avoiding journal._create_document_from_attachment,
        # whose internal commit's flush hits an empty-user access check in the
        # queue-job context and leaves an empty bill).
        #
        # The create-and-decode runs inside a SAVEPOINT rather than a full
        # cr.rollback(): on a decode error or a raw DB error it rolls back only
        # this block (discarding the empty move + xml attachment) and leaves a
        # usable cursor — WITHOUT discarding the caller's other work. That matters
        # on the interactive re-import path, where the cursor belongs to the RPC
        # request, not to us. A _FlushingSavepoint also clears the ORM cache on
        # rollback, so no half-written record leaks, and it behaves identically
        # under tests (no in_test_mode special-casing needed).
        try:
            move, xml_attachment = self._import_create_and_decode(
                AccountMove,
                journal,
                move_type,
                xml_bytes,
                extracted,
                main_attachment=main_attachment,
                extra_attachments=extra_attachments,
            )
        except _AiImportDecodeError as e:
            return self._import_failed(
                "import.decode_failed",
                f"UBL import error: {e}",
                f"FAILED: {e}",
                extra_vals=extraction_vals,
            )
        except Exception as e:
            _logger.exception("Odoo import failed for invoice import %s", self.id)
            return self._import_failed(
                "import.decode_failed",
                f"Odoo import failed: {e}",
                f"FAILED: {e}",
                extra_vals=extraction_vals,
            )

        # Bill built successfully. Link it + its generated XML now (outside the
        # savepoint, so nothing is left dangling if the block had rolled back), so
        # the bill is never orphaned and a retry can't create a duplicate.
        self.xml_attachment_id = xml_attachment.id
        self.move_id = move.id

        # Step 4: post-processing. This code does NOT commit, so a savepoint IS
        # valid here — a DB-level error rolls back to a usable cursor, the bill
        # stays linked, and we mark the record failed (terminal) instead of raising.
        try:
            with self.env.cr.savepoint():
                # ⚠️ The original PDF is NOT copied here any more. It is attached
                # and made the main attachment in `_import_create_and_decode`,
                # before the decode, so core
                # cannot generate a "Generated by Odoo" substitute and claim the
                # preview with force=True. Putting a copy back here would both
                # restore the defect and leave TWO copies of the PDF on the bill.
                # Shared post-processing (partner, categories, chatter)
                AccountMove._ai_post_process(
                    move, extracted,
                    usage, model_name, company,
                    source_label=source_label,
                )
                # finvoice content AC-21, AC-22: last, so nothing above
                # rewrites them.
                self._import_einvoice_references(extracted, move)
                self._import_keep_stated_row_amounts(extracted, move)
        except Exception as e:
            _logger.exception(
                "Post-import processing failed for import %s (move %s already "
                "created)",
                self.id,
                move.id,
            )
            result = self._import_failed(
                "import.post_process_failed",
                f"Post-import processing failed (vendor bill id {move.id} was "
                f"created): {e}",
                f"FAILED: {e}",
            )
            result.move = move
            return result

        self.write({"state": lifecycle.STATE_IMPORTED, "failure_kind": False})

        detail = f"OK: Created {move.display_name}"
        # The bill's references in the job result, so the job list can be
        # searched by vendor, invoice number or amount.
        references = [
            move.partner_id.name,
            move.ref and f"No. {move.ref}",
            f"{move.amount_total:.{move.currency_id.decimal_places}f} "
            f"{move.currency_id.name or ''}".strip(),
        ]
        detail += "".join(f" · {r}" for r in references if r)
        if usage:
            detail += (
                f" | Tokens: {usage['input_tokens']} in / "
                f"{usage['output_tokens']} out"
            )
        return ImportResult(
            OUTCOME_IMPORTED,
            move=move,
            duplicate_of=self.env["account.move"],
            detail=detail,
        )

    def _import_einvoice_references(self, extracted, move):
        """finvoice content AC-21: the buyer's order number and the buyer
        reference the vendor sent, on the record and on the draft bill (its
        source document and buyer reference). The bill's own reference stays
        the vendor's invoice number; no purchase order is looked for. A
        message without them leaves both exactly as before."""
        data = extracted if isinstance(extracted, dict) else {}
        order = (data.get("order_reference") or "").strip()
        buyer = (data.get("buyer_reference") or "").strip()
        if not (order or buyer):
            return
        self.write({
            "einvoice_order_reference": order or False,
            "einvoice_buyer_reference": buyer or False,
        })
        bill_vals = {}
        if order:
            bill_vals["invoice_origin"] = order
        if buyer and len(buyer) <= BUYER_REFERENCE_MAX:
            bill_vals["einvoice_buyer_reference"] = buyer
        move.write(bill_vals)

    def _import_keep_stated_row_amounts(self, extracted, move):
        """finvoice content AC-22: a row sent with its gross price and a
        discount carries the chosen percentage (the stated one when it makes
        the stated row amount, else the worked-out one), both checked at the
        price as stored; a decoded line that still misses the stated amount
        goes without a discount by the rule of every other row - its net
        price, or one unit at the stated amount when that price cannot be
        stored (review F1) - so the stated amount wins."""
        data = extracted if isinstance(extracted, dict) else {}
        rows = [row for row in data.get("lines") or [] if row.get("line_total") is not None]
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == "product")
        if not any("gross_price" in row for row in rows) or len(rows) != len(lines):
            return
        currency = move.currency_id
        precision = self.env["decimal.precision"].precision_get
        digits = precision("Discount")
        price_digits = precision("Product Price")
        for line, row in zip(lines, rows):
            if "gross_price" not in row or not line.discount:
                continue
            # Signed: a negative row stays negative (review F4).
            stated = row["line_total"]
            choice = finvoice_rows.import_discount(
                row["quantity"], stored_price(row["gross_price"], price_digits), 1,
                row.get("discount_percent"), row.get("discount_amount"), stated, digits=digits)
            if choice:
                # A write equal at the field's precision is skipped by the ORM,
                # so the unrounded value is cleared first.
                line.write({"discount": 0.0})
                line.write({"discount": float(choice["percent"])})
            if currency.is_zero(line.price_subtotal - stated):
                continue
            self._import_row_without_discount(line, row, price_digits)

    @staticmethod
    def _import_row_without_discount(line, row, price_digits):
        """The stated row as a line without a discount (review F1): the
        same rule as every other stated row (``undiscounted_row``)."""
        stated = row["line_total"]
        quantity = line.quantity or 1.0
        undiscounted = undiscounted_row(quantity, stated, price_digits, row.get("price_unit"))
        if not undiscounted or not undiscounted["note"]:
            line.write({"price_unit": stated / quantity, "discount": 0.0})
            return
        line.write({
            "name": "\n".join(part for part in (line.name, undiscounted["note"]) if part),
            "quantity": 1.0,
            "price_unit": stated,
            "discount": 0.0,
        })

    def _import_create_and_decode(self, AccountMove, journal, move_type, xml_bytes,
                                  extracted, main_attachment=None,
                                  extra_attachments=None):
        """Steps 2-3 of `_import_build_bill`, in one savepoint: store the
        generated document, create the draft bill, put the files on it and
        decode the document into it. Returns ``(move, xml_attachment)``;
        raises `_AiImportDecodeError` when the decoder refuses, or whatever
        the ERP raised — the savepoint is rolled back either way, so no
        empty bill or stray attachment survives."""
        self.ensure_one()
        xml_name = (
            self.name.rsplit(".", 1)[0] + ".xml" if self.name else "invoice.xml"
        )
        with self.env.cr.savepoint():
            xml_attachment = self.env["ir.attachment"].create(
                {
                    "name": xml_name,
                    "raw": xml_bytes,
                    "mimetype": "application/xml",
                    "res_model": self._name,
                    "res_id": self.id,
                }
            )
            move = AccountMove.create(
                {"move_type": move_type, "journal_id": journal.id}
            )

            # 🔴 THE VENDOR'S OWN PDF MUST BE THE MAIN ATTACHMENT *BEFORE*
            # THE DECODER RUNS. This copy used to live in the post-process
            # block of `_import_build_bill`, i.e. AFTER the decode, and that
            # ordering is what put an Odoo-generated placeholder in the bill's
            # preview.
            #
            # What a bill looked like before the fix — three attachments,
            # all created in the same second, and the id order is the proof
            # of who ran first:
            #   <id>  " Bill (INV100001) - Generated by Odoo.pdf"  ~20 kB
            #           ← and it held message_main_attachment_id
            #   <id>  "INV100001 - Northwind Invoice.pdf"            ~70 kB
            #           ← the vendor's own document, attached by us, too late
            #   <id>  "extraction.json"                               ~2 kB
            #
            # Core, at account_edi_ubl_cii/models/account_edi_ubl.py:3921-3969
            # (`_import_attachments`), renders a substitute PDF with QWeb,
            # watermarks it "Generated by Odoo" (:3950), names it
            # "<invoice> - Generated by Odoo.pdf" (:3955-3958) and then CLAIMS
            # the slot with `_message_set_main_attachment_id(..., force=True,
            # filter_xml=False)` (:3965) — force, so it beats anything we set
            # afterwards. Its four-way early return at :3926-3932 includes
            # `invoice.message_main_attachment_id`, so a main attachment that
            # already exists makes core generate nothing at all.
            #
            # That feature is right for a genuine Peppol e-invoice, which is
            # XML only and has nothing a human can read. Our pipeline is the
            # reverse: we START from the vendor's PDF and synthesise the UBL
            # from it, so core's substitute is a redundant copy of a document
            # we already hold.
            #
            # Why this shape and not the alternatives:
            #  * it puts the REAL document in the preview, which is the point;
            #  * it saves a QWeb PDF render on EVERY import — pure waste today;
            #  * core also offers a global switch, the system parameter
            #    `account_edi_ubl_cii.disable_pdf_in_xml` (read at :3924). The
            #    19.0.3.1.0 migration sets it as a backstop for any path this
            #    hook point does not reach (a re-import, the email route, a
            #    future caller). Both are deliberate and neither is redundant:
            #    the parameter is global and would also silence the substitute
            #    PDF for a genuine Peppol XML-only import, which is a real
            #    feature for a document that has no human-readable part; this
            #    ordering fix is precise and touches only our own imports.
            #    Do not delete one as "already covered by the other".
            #
            # 🔴 OF THE TWO, ONLY THIS ONE TRAVELS (the dual-core design, step 7). Since
            # 19.0.3.10.0 the parameter is written only on a core that reads
            # it; this ordering fix is unconditional, because it is correct
            # on both measured cores and inert where nothing competes for
            # the slot. ⚠️ "Inert" is not "pointless", and the legacy
            # meaning is different from the US-deployment one: that core has no
            # substitute-PDF feature at all, so nothing is being beaten to
            # the slot — what the early `.copy()` buys there is that the
            # bill HAS a main attachment, and that it is the vendor's
            # document, from the moment it is created. (On that core the
            # generated UBL is not stored on the bill either — its
            # `_import_attachments` only extracts embedded
            # `AdditionalDocumentReference` files — so the vendor's PDF and
            # `extraction.json` are the whole of the bill's evidence.)
            #
            # The copy is the same `.copy()` as before — the original stays on
            # the goodahead.invoice.import record, the bill gets its own — so there is
            # exactly ONE PDF on the move, not two. It sits inside this
            # savepoint so a decode failure discards it along with the move.
            #
            # A structured source may have no PDF at all: then
            # there is nothing to copy, and core's own substitute is the
            # only readable copy of the invoice.
            if self.pdf_attachment_id:
                pdf_on_move = self.pdf_attachment_id.copy(
                    {
                        "res_model": "account.move",
                        "res_id": move.id,
                    }
                )
                move._message_set_main_attachment_id(pdf_on_move)
            # The source's own document and the package's other files
            # (until then a Finvoice-only decoder override fed by
            # context keys). Same savepoint, same reason: before the
            # decode, so the decoder finds a main attachment and renders
            # no substitute, and a failed decode discards the copies.
            self._import_attach_files(move, main_attachment, extra_attachments)

            error = AccountMove._ai_decode_ubl_into_move(
                move, xml_bytes, extracted, attachment=xml_attachment
            )
            if error:
                raise _AiImportDecodeError(error)
        return move, xml_attachment

    def _import_attach_files(self, move, main_attachment=None, extra_attachments=None):
        """Copy ``main_attachment`` and ``extra_attachments`` onto ``move``;
        the copy of ``main_attachment`` becomes the bill's main document when
        it has none yet (no PDF came with the invoice — AC-25)."""
        Attachment = self.env["ir.attachment"]
        on_move = {"res_model": "account.move", "res_id": move.id}
        main = (main_attachment or Attachment).exists()
        main_copy = main.copy(on_move) if main else Attachment
        for extra in (extra_attachments or Attachment).exists():
            extra.copy(on_move)
        if main_copy and not move.message_main_attachment_id:
            # filter_xml=False: by default the ERP never makes an XML file the
            # main attachment, and here it is the only document there is.
            move._message_set_main_attachment_id(main_copy, force=True, filter_xml=False)

    def _check_reimport_data(self, extracted):
        """SCR-02 "validation": a structured source's edited data is checked
        with the core validator BEFORE anything changes, and a re-import that
        would fail again is refused with the reason — the record stays failed
        with its data. An AI reading's hand edit is deliberately not re-asked
        (see `_finalize_import`), so the AI path is unchanged."""
        spec = self._import_reader_spec()
        if not spec or not spec.structured:
            return
        try:
            self._import_validator()._validate_extraction(json.loads(json.dumps(extracted)))
        except InvoiceDataError as e:
            raise UserError(
                _("The invoice data still does not pass the checks, so it was not re-imported: %s", e)
            ) from e

    def action_reimport_from_json(self):
        """Re-run UBL generation + import from the stored (possibly hand-edited)
        extraction JSON, WITHOUT calling Claude again — a cheap recovery when the
        AI extraction was almost right (e.g. a missing date you can fill in)."""
        self.ensure_one()
        # Server-side guard: the button's visibility rule is UI-only, so enforce
        # the state here too (a direct RPC must not re-import a draft/processing
        # record and create a bill).
        if self.state != lifecycle.STATE_FAILED:
            raise UserError(
                _("Only failed imports can be re-imported (this one is '%s').")
                % self.state
            )
        if self.move_id:
            raise UserError(
                _(
                    "A vendor bill (%s) is already linked; re-import is blocked to "
                    "avoid creating a duplicate."
                )
                % self.move_id.display_name
            )
        if not self.extraction_data:
            raise UserError(_("There is no stored extraction JSON to re-import."))
        try:
            extracted = json.loads(self.extraction_data)
        except json.JSONDecodeError as e:
            raise UserError(_("The stored extraction JSON is not valid: %s") % e)
        self._check_reimport_data(extracted)
        self._reimport_from_extraction(extracted)

    def _reimport_from_extraction(self, extracted):
        """Build the bill from ``extracted`` — stored invoice data — in the
        caller's request, without reading the document again: the body of
        "Re-import from JSON" and of a duplicate override of a paid reading."""
        self.ensure_one()
        AccountMove = self.env["account.move"]
        # Not failed any more while it runs (ADR-0008: there is no stored
        # "processing"; this runs in the caller's request, not in a job).
        self.state = lifecycle.STATE_WAITING
        # The source's chance to repair the stored extraction before the
        # XML is built from it — see `_import_before_reimport`.
        self._import_before_reimport(extracted)
        try:
            xml_bytes = AccountMove._ai_generate_ubl_from_extracted(
                extracted, self.company_id
            )
        except Exception as e:
            _logger.exception("UBL generation from stored JSON failed for %s", self.id)
            self._fail(f"UBL generation from stored JSON failed: {e}")
            return
        # The reader's policy and files, as on the first import.
        reader = self._import_reader()
        main_attachment, extra_attachments = (
            self._source_bill_attachments(reader) if reader else (None, None)
        )
        self._finalize_import(
            {
                "extracted": extracted,
                **self._import_reimport_meta(),
                "xml_bytes": xml_bytes,
                "source_label": self._import_reimport_source_label(extracted),
                "policy": self._import_reader_policy(),
                "main_attachment": main_attachment,
                "extra_attachments": extra_attachments,
            }
        )

    @api.model
    def _systray_attention_domain(self):
        """What the systray bubble counts: the displayed status
        failed, duplicate or possibly lost — a record whose retry is queued
        shows processing and is not counted — in the user's active companies,
        so the bubble never leaks another company's records."""
        return [
            ("display_state", "in", list(ATTENTION_STATES)),
            ("company_id", "in", self.env.companies.ids),
        ]

    @api.model
    def _systray_attention_data(self, limit=10):
        """``{"count", "imports"}`` for the systray bubble; nothing for a user
        who does not work the imports."""
        if not self._ai_user_works_imports():
            return {"count": 0, "imports": []}
        count, items = self._systray_attention_items(limit)
        # The route's shape stays as it was: no date.
        imports = [{k: v for k, v in item.items() if k != "date"} for item in items]
        return {"count": count, "imports": imports}

    @api.model
    def _systray_attention_items(self, limit=10):
        """``(count, items)`` of the records needing attention, newest first;
        each item ``{id, name, status, error, date}``."""
        domain = self._systray_attention_domain()
        count = self.search_count(domain)
        labels = dict(self._fields["display_state"]._description_selection(self.env))
        imports = []
        for record in self.search(domain, limit=limit, order="write_date desc"):
            if record.display_state == lifecycle.STATE_DUPLICATE:
                detail = _("Duplicate of %s", record.duplicate_of_move_id.display_name or "?")
            else:
                detail = record.error_message or labels.get(record.display_state, "")
            imports.append(
                {
                    "id": record.id,
                    "name": " ".join(
                        filter(None, [record.seller_name, record.invoice_number])
                    ) or record.name or "",
                    "status": labels.get(record.display_state, record.display_state),
                    "error": detail if len(detail) <= 120 else detail[:117] + "...",
                    "date": fields.Datetime.to_string(record.write_date),
                }
            )
        return count, imports

    @api.model
    def _attention_failed_imports_section(self):
        """The ``failed_imports`` section of the shared e-invoicing attention
        bubble (send ADR-0014): the same records as the bubble always counted
       , for the user's active companies."""
        count, imports = self._systray_attention_items()
        return {
            "label": _("Failed imports"),
            "count": count,
            "items": [
                {
                    "id": item["id"],
                    "title": item["name"],
                    "subtitle": item["error"],
                    "badge": item["status"],
                    "date": item["date"],
                }
                for item in imports
            ],
            "action": {
                "type": "ir.actions.act_window",
                "name": _("Incoming invoices needing attention"),
                "res_model": self._name,
                "views": [[False, "list"], [False, "form"]],
                "domain": [["display_state", "in", list(ATTENTION_STATES)]],
            },
            "item_action": {
                "type": "ir.actions.act_window",
                "res_model": self._name,
                "views": [[False, "form"]],
            },
        }

    @api.model
    def _ai_user_works_imports(self, user=None):
        """Whether ``user`` (default: the current one) is somebody who works the
        AI imports — and so sees the failed-imports bubble and the import menus.

        🔴 BOOKKEEPER OR ADMINISTRATOR, NOT PLAIN INVOICING. The access rules stay
        on `group_account_invoice` so that anyone who may open a vendor bill can
        open the import behind it; what is narrower is who gets TOLD about
        imports. It was `group_account_invoice` from 19.0.3.x, and a deployment
        where Invoicing is what lets people raise proformas and customer invoices
        found every one of those users looking at a red vendor-bill error bubble.

        ⚠️ BOTH GROUPS, BECAUSE NEITHER IMPLIES THE OTHER IN 19.0 CORE.
        `group_account_manager` implies `group_account_invoice`, not
        `group_account_user` ("Show Full Accounting Features") — checking only
        the latter hid the bubble from accounting Administrators, who are the
        people working vendor bills on a database without OCA
        `account_usability`. With that module the two are renamed Accountant and
        Bookkeeper and Accountant implies Bookkeeper, so the second check is
        redundant there and harmless.

        The `failed_imports` section of the shared attention bubble
        (`FAILED_IMPORTS_GROUPS`) and the menu items carry the same two groups;
        keep all three in step.
        """
        user = user or self.env.user
        return user.has_group("account.group_account_user") or user.has_group(
            "account.group_account_manager"
        )

    def _fail(self, code, message=None, extra_vals=None, kind="data"):
        """Record the import as failed.

        ``code`` is a neutral ``module.error_name`` (§1.4) for the log;
        ``message`` the plain-language reason an Accountant reads; ``kind``
        ``data`` (waits for an Accountant) or ``interrupted`` (retried).

        The pre-split form ``_fail(message, extra_vals=...)`` still works:
        with no ``message``, the first argument is the message."""
        if message is None:
            code, message = False, code
        _logger.error(
            "Invoice Import %s failed%s: %s",
            self.id,
            f" [{code}]" if code else "",
            message,
        )
        vals = {
            "state": lifecycle.STATE_FAILED,
            "error_message": message,
            "failure_kind": kind or "data",
        }
        if extra_vals:
            vals.update(extra_vals)
        self.write(vals)
