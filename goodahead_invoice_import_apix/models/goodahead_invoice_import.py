# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""APIX fields of the import record (data-model.md, "Fields added by
goodahead_invoice_import_apix"), the APIX reader in the core's source dispatch
 and the package-import job."""

import logging
import os

from odoo import _, api, fields, models
from odoo.addons.goodahead_apix.lib.apix_client import ApixError
from odoo.addons.goodahead_invoice_import.lib import import_record as lifecycle
from odoo.addons.goodahead_invoice_import.lib.import_source import (
    STRICT_POLICY,
    SourceReader,
)
from odoo.addons.goodahead_invoice_import_finvoice.lib import finvoice
from odoo.addons.goodahead_invoice_import_finvoice.lib.sniff import looks_like_finvoice
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import UserError
from odoo.tools import mute_logger
from psycopg2.errors import LockNotAvailable

from ..lib import apix_listing, apix_package

_logger = logging.getLogger(__name__)

#: Set by the APIX package job around the core import, so overrides can tell an
#: APIX import apart. The bank-account rule no longer needs it (it lives in the
#: core under STRICT_POLICY since T11b).
CTX_APIX_PACKAGE = "goodahead_apix_package_import"

ADMIN_GROUP = "account.group_account_manager"

#: Dedicated channel: AI backlogs never delay e-invoices.
APIX_CHANNEL = "root.apix"

#: The APIX reader (T9b source dispatch). Asked before the Finvoice reader
#: (priority 10): an APIX record holds a Finvoice message too, but is read by
#: downloading its package and imported under the APIX rules.
APIX_READER = "apix"
APIX_PRIORITY = 5

PACKAGE_IDENTITY_PREFIX = "apix-import:"
MARK_IDENTITY_PREFIX = "apix-mark:"

#: contracts/events.md: technical failures are retried 3 times (tries in all,
#: queue_job's `max_retries`); the mark-received job 5 times.
PACKAGE_MAX_RETRIES = 3
MARK_MAX_RETRIES = 5

#: A job that finds its record locked by another one tries again after this
#: many seconds, without using up a retry.
LOCKED_RETRY_SECONDS = 30

#: Shadow results: written by the shadow classification, cleared
#: together whenever a package is to be classified again or receiving goes live.
SHADOW_CLEAR = {
    "shadow_outcome": False,
    "shadow_vendor_id": False,
    "shadow_vendor_match": False,
    "shadow_reason": False,
}

#: Mirror mode: a test or staging copy receives next to production. It bills
#: every listed package, received ones too, and never marks anything received
#: at APIX, so the production receiver still gets every package.
MIRROR = "mirror"

#: Receiving states in which packages become bills.
BILLING_STATES = ("live", MIRROR)


def unpack_package(body, document_name=None, image_name=None):
    """`apix_package.unpack` with the Finvoice add-on's sniff."""
    return apix_package.unpack(body, document_name, image_name, sniff=looks_like_finvoice)


def package_identity_key(record_id):
    """contracts/events.md, `apix.package.import.requested.v1`."""
    return f"{PACKAGE_IDENTITY_PREFIX}{record_id}"


def mark_identity_key(record_id):
    """contracts/events.md, `apix.package.mark_received.requested.v1`."""
    return f"{MARK_IDENTITY_PREFIX}{record_id}"


class GoodaheadInvoiceImport(models.Model):
    _inherit = "goodahead.invoice.import"

    source = fields.Selection(
        selection_add=[("apix", "APIX")],
        ondelete={"apix": "set default"},
    )
    reader = fields.Selection(
        selection_add=[(APIX_READER, "APIX")],
        ondelete={APIX_READER: "set null"},
    )
    apix_storage_id = fields.Char(
        string="APIX storage ID",
        readonly=True,
        copy=False,
    )
    # 🔴 Per-package download secret: accounting administrators only, never
    # copied, never exported, never logged.
    apix_storage_key = fields.Char(
        string="APIX storage key",
        readonly=True,
        copy=False,
        exportable=False,
        groups=ADMIN_GROUP,
    )
    apix_listing_data = fields.Json(
        string="APIX listing data",
        readonly=True,
        copy=False,
        help="Seller, invoice number, amount, dates and statuses as APIX "
        "listed the package, before it was downloaded.",
    )
    apix_status = fields.Char(
        string="APIX status",
        readonly=True,
        copy=False,
        help="Last StorageStatus APIX reported: NEW, UNRECEIVED, RECEIVED or DELETED.",
    )
    apix_mark_confirmed = fields.Boolean(
        string="Received at APIX",
        readonly=True,
        copy=False,
        help="APIX confirmed the package as received.",
    )
    apix_mirrored = fields.Boolean(
        string="Mirrored",
        readonly=True,
        copy=False,
        help="Settled while receiving was in mirror mode: never marked received "
        "at APIX, not even after receiving goes live.",
    )
    apix_document_date = fields.Date(
        string="APIX document date",
        readonly=True,
        copy=False,
        index=True,
    )
    shadow_outcome = fields.Selection(
        [
            ("would_import", "Would import"),
            ("would_duplicate", "Would be a duplicate"),
            ("imported_before", "Imported before"),
            ("would_be_possibly_lost", "Would be possibly lost"),
            ("would_fail", "Would fail"),
        ],
        readonly=True,
        copy=False,
        help="What a live run would do with this package. Set during the "
        "migration tool's shadow run; cleared on going live.",
    )
    # AC-17: who the vendor would be, and why a package would fail.
    shadow_vendor_id = fields.Many2one(
        "res.partner",
        string="Shadow vendor",
        readonly=True,
        copy=False,
        help="The vendor a live import would book this package on (shadow run).",
    )
    shadow_vendor_match = fields.Selection(
        [
            ("matched", "Matched"),
            ("ambiguous", "Ambiguous"),
            ("none", "No vendor found"),
        ],
        string="Shadow vendor match",
        readonly=True,
        copy=False,
        help="How the shadow run decided the vendor: one vendor matched, several "
        "vendors share the seller's identifiers, or none was found.",
    )
    shadow_reason = fields.Char(
        string="Shadow failure reason",
        readonly=True,
        copy=False,
        help="Why a live import would fail (shadow run).",
    )
    apix_reopened = fields.Boolean(
        string="Reopened",
        readonly=True,
        copy=False,
        help="Closed without bill and reopened: waits for an explicit Retry, the "
        "APIX runs never pick it up by themselves.",
    )
    apix_import_requested = fields.Boolean(
        string="Import requested",
        readonly=True,
        copy=False,
        help="An Accountant asked for this package to be imported (Retry, Import "
        "anyway, Import a possibly lost package): imported even when APIX shows "
        "it as received.",
    )

    # ADR-0006: one package, one record per company — the database rule behind
    # "one package, one bill". Partial: records of other sources have no id.
    _company_apix_storage_uniq = models.UniqueIndex(
        "(company_id, apix_storage_id) WHERE apix_storage_id IS NOT NULL",
        "This APIX package is already in the import inbox of this company.",
    )

    # ------------------------------------------------------------------
    # Inbox columns: the listing until the package is read
    # ------------------------------------------------------------------

    @api.depends("extraction_data", "apix_listing_data", "apix_document_date")
    def _compute_invoice_summary(self):
        """A package listed but not downloaded yet has no invoice data; the
        inbox shows what the APIX listing says of it (seller, number, amount,
        date) until the Finvoice message has been read."""
        apix_listed = self.filtered(
            lambda r: r.apix_listing_data and not r.extraction_data
        )
        super(GoodaheadInvoiceImport, self - apix_listed)._compute_invoice_summary()
        for record in apix_listed:
            listing = record.apix_listing_data
            listing = listing if isinstance(listing, dict) else {}
            amount = str(listing.get("AmountToPay") or "").replace(",", ".").strip()
            try:
                amount = float(amount) if amount else 0.0
            except ValueError:
                amount = 0.0
            record.update(
                {
                    "seller_name": str(listing.get("SenderName") or "").strip() or False,
                    "invoice_number": str(listing.get("DocumentID") or "").strip() or False,
                    "invoice_date": record.apix_document_date or False,
                    "currency_id": record._summary_currency(listing.get("Currency")).id,
                    "amount_total": amount,
                }
            )

    def action_open_apix_todos(self):
        """The inbox's link to the Administrator to-dos: the open
        to-dos the APIX runs and daily checks raised on the connections of
        the user's companies.

        A list header button: the web client calls it on the selected records
        (often none), so it is a record method that ignores the selection,
        not an ``api.model`` one."""
        connections = self.env["goodahead.apix.connection"].search(
            [("company_id", "in", self.env.companies.ids)]
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("APIX to-dos"),
            "res_model": "mail.activity",
            "view_mode": "list,form",
            "views": [(False, "list"), (False, "form")],
            "domain": [
                ("res_model", "=", "goodahead.apix.connection"),
                ("res_id", "in", connections.ids),
            ],
        }

    # ------------------------------------------------------------------
    # Package import job (apix.package.import.requested.v1)
    # ------------------------------------------------------------------

    def _apix_job_description(self, action):
        """A job title that says which bill it is about, so the job list can
        be searched by what the APIX listing tells before the import: seller,
        invoice number, amount, document date and the storage id."""
        self.ensure_one()
        parts = [self.seller_name, self.invoice_number and f"No. {self.invoice_number}"]
        listing = self.apix_listing_data if isinstance(self.apix_listing_data, dict) else {}
        # A stated zero is shown too; an amount neither listed nor read is not.
        if self.amount_total or str(listing.get("AmountToPay") or "").strip():
            currency = self.currency_id
            digits = currency.decimal_places if currency else 2
            parts.append(f"{self.amount_total:.{digits}f} {currency.name or ''}".strip())
        if self.invoice_date:
            parts.append(fields.Date.to_string(self.invoice_date))
        parts.append(self.apix_storage_id)
        return f"APIX {action}: " + " · ".join(p for p in parts if p)

    def _apix_enqueue_package_import(self):
        """One package-import job per record on `root.apix`, keyed
        `apix-import:<id>` so a record never has two queued jobs. Linked in
        the caller's transaction. Technical failures are retried
        by the queue (events.md: 3 tries with backoff, the backoff is the job
        function's retry pattern). Returns the last job."""
        job = None
        for record in self:
            job = record.with_company(record.company_id).with_delay(
                description=record._apix_job_description("package import"),
                identity_key=package_identity_key(record.id),
                channel=APIX_CHANNEL,
                max_retries=PACKAGE_MAX_RETRIES,
            )._apix_import_package()
            record._link_job(job)
        return job

    def _enqueue_import(self, description):
        """An Accountant's retry or duplicate override of an APIX record runs
        the package job, not the generic import job (events.md: producers of
        `apix.package.import.requested.v1`)."""
        self.ensure_one()
        if self.source != "apix":
            return super()._enqueue_import(description)
        return self._apix_enqueue_package_import()

    # ------------------------------------------------------------------
    # The APIX reader in the core's source dispatch
    # ------------------------------------------------------------------

    @api.model
    def _import_readers(self):
        readers = super()._import_readers()
        readers[APIX_READER] = SourceReader(
            APIX_READER,
            priority=APIX_PRIORITY,
            policy=STRICT_POLICY,
            structured=True,
        )
        return readers

    def _source_read(self, source_key):
        """A generic import job of an APIX record does what the package job
        does."""
        if source_key != APIX_READER:
            return super()._source_read(source_key)
        return self._apix_import_package()

    def _source_claims_record(self, source_key):
        if source_key != APIX_READER:
            return super()._source_claims_record(source_key)
        return self.source == "apix"

    def _source_bill_attachments(self, source_key):
        """The package's Finvoice message (the bill's main document when no
        image came with it) and its other files. The image is the
        record's PDF, which the core puts on the bill itself."""
        if source_key != APIX_READER:
            return super()._source_bill_attachments(source_key)
        return self.finvoice_attachment_id, self.finvoice_extra_attachment_ids

    # ------------------------------------------------------------------
    # Accountant actions on APIX records: live only, decisions remembered
    # ------------------------------------------------------------------

    def _apix_records(self):
        return self.filtered(lambda r: r.source == "apix")

    def _apix_check_live(self):
        """An Accountant's import of an APIX record runs only while the
        company receives live or in mirror mode: in shadow nothing is billed,
        off hands the packages to whoever receives instead."""
        Connection = self.env["goodahead.apix.connection"].sudo()
        for record in self._apix_records():
            receiving = Connection.search([("company_id", "=", record.company_id.id)], limit=1)
            if receiving.receiving_state not in BILLING_STATES:
                raise UserError(
                    _(
                        "%(name)s cannot be imported now: APIX receiving of %(company)s is "
                        "%(state)s, not live. Imports from APIX run only while receiving is "
                        "live or in mirror mode.",
                        name=record.display_name,
                        company=record.company_id.name,
                        state=dict(
                            Connection._fields["receiving_state"]._description_selection(self.env)
                        ).get(receiving.receiving_state, _("not set up")),
                    )
                )

    def _apix_request_import(self):
        """Remember that an Accountant asked for the import: the package job
        then imports even a package APIX shows as received."""
        self._apix_check_live()
        self._apix_records().filtered(lambda r: not r.apix_import_requested).sudo().write(
            {"apix_import_requested": True}
        )

    def action_retry(self):
        self._apix_request_import()
        return super().action_retry()

    def action_override_duplicate(self):
        self._apix_request_import()
        return super().action_override_duplicate()

    def action_import_possibly_lost(self):
        self._apix_request_import()
        return super().action_import_possibly_lost()

    def action_reopen(self):
        """A reopened APIX record waits for an explicit Retry: the APIX runs
        neither import it nor turn it possibly lost by themselves."""
        result = super().action_reopen()
        self._apix_records().sudo().write({"apix_reopened": True})
        return result

    def action_reimport_from_json(self):
        """A re-import of an APIX record follows the import's rules: no bank
        account written, and APIX hears of the bill once it is saved. Live
        only."""
        self.ensure_one()
        if self.source != "apix":
            return super().action_reimport_from_json()
        self._apix_check_live()
        result = super(
            GoodaheadInvoiceImport, self.with_context(**{CTX_APIX_PACKAGE: True})
        ).action_reimport_from_json()
        if self.move_id:
            self._on_settled()
        return result

    def _apix_import_package(self):
        """Import one APIX package. Returns the job result.

        1. Idempotency: skip a settled record or one with a bill,
           after taking the record's row lock, so two jobs never import it
           at the same time.
        2. Download WITHOUT marking received and keep every file of
           the package on the record — unless an earlier attempt kept them
           already (a retry after a data failure, a duplicate override): then
           the kept files are imported again and APIX is not asked.
        3. Finvoice -> invoice data -> the core, under the strict matching
           policy.
        4. A bill was saved -> the mark-received job is enqueued in THIS
           transaction (`_on_settled`), so it exists only if the bill commits.
           Duplicate and failed outcomes do not mark.

        Technical problems (APIX unreachable, empty answer, a ZIP cut off) are
        raised as `RetryableJobError` so the queue retries; the last try
        records the failure as ``interrupted`` instead, and the next run
        enqueues the record again (events.md). On the last try ANY error is
        recorded that way, so a record never waits without a reason. Bad
        invoice data fails as ``data`` and is never retried automatically.

        Switch-back race: the job holds a share lock on the company's
        connection row from the receiving check until it commits, and the
        switch back takes the row's update lock before its own checks — so a
        company never leaves live while one of its packages is being billed.

        A package APIX shows as RECEIVED (in the last run's listing) that no
        Accountant asked to import is not imported: someone else received it,
        so it goes to the recognition. The status comes
        from the latest run: a fresh listing per package would list the whole
        company (up to 500 packages) for every job, and the run that enqueues
        imports refreshes every status just before.
        """
        self.ensure_one()
        if not self._apix_lock():
            # Another job holds the record; try again shortly without using
            # up a retry — it will find the record settled.
            raise RetryableJobError(
                "APIX package import: the record is locked by another transaction",
                seconds=LOCKED_RETRY_SECONDS,
                ignore_retry=True,
            )
        self.invalidate_recordset()
        if self.state in lifecycle.SETTLED_STATES or self.move_id:
            return "OK: already processed"
        receiving = self.env["goodahead.apix.connection"].sudo().search(
            [("company_id", "=", self.company_id.id)], limit=1
        )
        if receiving and not receiving._apix_lock_for_share():
            # The receiving state is being changed (a switch back): try again
            # shortly without using up a retry — it will find it off.
            raise RetryableJobError(
                "APIX package import: the company's connection is locked by a switch",
                seconds=LOCKED_RETRY_SECONDS,
                ignore_retry=True,
            )
        if receiving and receiving.receiving_state not in BILLING_STATES:
            # Shadow never creates a bill; off hands the package to
            # whoever receives instead (an earlier APIX connector, AC-19).
            _logger.info(
                "APIX package import for record %s skipped: receiving is %s",
                self.id,
                receiving.receiving_state,
            )
            return f"SKIPPED: receiving {receiving.receiving_state}"
        # Mirror bills a package production already received: that is the point.
        mirror = receiving and receiving.receiving_state == MIRROR
        if not mirror and self._apix_received_elsewhere():
            self.sudo().write({"state": lifecycle.STATE_POSSIBLY_LOST, **SHADOW_CLEAR})
            self._apix_enqueue_recognition()
            _logger.info(
                "APIX package import for record %s: received at APIX already, sent to recognition",
                self.id,
            )
            return "SKIPPED: received at APIX, recognising"
        if not self._apix_is_last_attempt():
            return self._apix_import_package_steps()
        try:
            with self.env.cr.savepoint():
                return self._apix_import_package_steps()
        except Exception as error:  # the last try records any error
            _logger.exception("APIX package import for record %s failed on its last try", self.id)
            # The savepoint rolled the try back: forget what it left in the cache.
            self.env.invalidate_all(flush=False)
            self._fail(
                "apix.unexpected",
                _(
                    "The import from APIX stopped on an unexpected error (%s). It is tried "
                    "again on the next run; if it keeps failing, tell the administrator.",
                    type(error).__name__,
                ),
                kind="interrupted",
            )
            return "FAILED: apix.unexpected"

    def _apix_import_package_steps(self):
        """Download unless the files are kept, then import (Flow B 2-4)."""
        self.ensure_one()
        if not self.finvoice_attachment_id:
            stopped = self._apix_fetch_package()
            if stopped:
                return stopped
        return self._apix_import_stored_package()

    def _apix_received_elsewhere(self):
        """APIX shows the package as received (in the latest run's listing),
        this add-on never confirmed it, and no Accountant asked for the
        import: it is not imported but recognised."""
        self.ensure_one()
        return (
            self.apix_status == apix_listing.STORAGE_RECEIVED
            and not self.apix_mark_confirmed
            and not self.apix_import_requested
            and not self.apix_reopened
        )

    def _apix_lock(self):
        """Row lock on the record, NOWAIT. False when another transaction
        holds it."""
        self.flush_recordset()
        try:
            with mute_logger("odoo.sql_db"), self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    "SELECT id FROM goodahead_invoice_import WHERE id = %s FOR UPDATE NOWAIT",
                    [self.id],
                )
        except LockNotAvailable:
            return False
        return True

    def _apix_fetch_package(self):
        """Download the package (never marking it received) and store its
        files on the record. Returns the job result when the import stops
        here, None when the stored package can be imported."""
        self.ensure_one()
        record = self.sudo()
        if not record.apix_storage_id or not record.apix_storage_key:
            return self._apix_interrupted(
                "apix.no_storage_key",
                _("APIX has not given a download key for this package yet; the next run refreshes it."),
            )
        connection = self.env["goodahead.apix.connection"].sudo()._get_working_connection(
            self.company_id
        )
        if not connection:
            return self._apix_interrupted(
                "apix.no_connection",
                _("The company has no working APIX connection, so the package could not be downloaded."),
            )
        try:
            # 🔴 mark_received=False, always. The package is marked
            # by the mark-received job, after the bill has committed.
            body = connection._client().download(
                record.apix_storage_id, record.apix_storage_key, mark_received=False
            )
        except ApixError as exc:
            return self._apix_interrupted(
                exc.code,
                _("Downloading the package from APIX failed: %s", exc.message),
            )

        listing = record.apix_listing_data or {}
        try:
            package = unpack_package(
                body, listing.get("DocumentName"), listing.get("ImageName")
            )
        except apix_package.PackageError as error:
            if error.code == apix_package.TRUNCATED:
                return self._apix_interrupted(error.code, error.message)
            # Not a package at all: keep what came, so nobody has to fetch
            # it again to see it, and wait for an Accountant.
            name = os.path.basename(listing.get("DocumentName") or "") or "apix-package.bin"
            self._apix_store_package(
                apix_package.Package(
                    files=[apix_package.PackageFile(name, body, apix_package.mimetype_of(name, body, looks_like_finvoice))]
                )
            )
            self._fail(error.code, error.message, kind="data")
            return f"FAILED: {error.code}"

        self._apix_store_package(package)
        if package.finvoice is None:
            message = _(
                "The APIX package holds no Finvoice message (files: %s).",
                ", ".join(f.name for f in package.files),
            )
            self._fail("apix.no_finvoice", message, kind="data")
            return "FAILED: apix.no_finvoice"
        return None

    def _apix_store_package(self, package):
        """Every file of ``package`` as an attachment of the record: the
        Finvoice message (`finvoice_attachment_id`), the image
        (`pdf_attachment_id`, the bill's main document) and the rest
        (`finvoice_extra_attachment_ids`) — the fields the Finvoice add-on
        hands to the bill. Files an earlier download kept without a
        Finvoice message are replaced, not duplicated."""
        self.ensure_one()
        stale = self.finvoice_extra_attachment_ids | self.pdf_attachment_id
        if stale:
            stale.sudo().unlink()
        attachments = self.env["ir.attachment"].create(
            [
                {
                    "name": package_file.name,
                    "raw": package_file.content,
                    "mimetype": package_file.mimetype,
                    "res_model": self._name,
                    "res_id": self.id,
                }
                for package_file in package.files
            ]
        )
        finvoice_attachment = (
            attachments[package.finvoice] if package.finvoice is not None else attachments.browse()
        )
        image_attachment = (
            attachments[package.image] if package.image is not None else attachments.browse()
        )
        self.write(
            {
                "finvoice_attachment_id": finvoice_attachment.id,
                "pdf_attachment_id": image_attachment.id,
                "finvoice_extra_attachment_ids": [
                    (6, 0, (attachments - finvoice_attachment - image_attachment).ids)
                ],
            }
        )

    def _apix_import_stored_package(self):
        """Translate the kept Finvoice message and import it.

        One message per package is expected. A package with several fails as
        data with a plain reason: one APIX package is one record, one bill
        and one received mark, so splitting it the way the
        Finvoice upload does would need several records per storage id and a
        mark only once all of them are settled — disproportionate for a case
        APIX operators do not send. An Accountant books the invoices by hand
        and closes the record, which marks the package received."""
        self.ensure_one()
        message_attachment = self.finvoice_attachment_id
        try:
            messages = finvoice.parse(message_attachment.raw or b"")
            if len(messages) > 1:
                _logger.warning(
                    "APIX package import for record %s: %s Finvoice messages in one package",
                    self.id,
                    len(messages),
                )
                self._fail(
                    "apix.several_messages",
                    _(
                        "The APIX package holds %(count)s invoices (Finvoice messages) in "
                        "%(file)s, but one package is imported as one bill. Create these "
                        "bills by hand from the attached file, then close this record "
                        "without bill.",
                        count=len(messages),
                        file=message_attachment.name,
                    ),
                    kind="data",
                )
                return "FAILED: apix.several_messages"
            invoice_data = finvoice.to_invoice_data(messages[0])
        except finvoice.FinvoiceError as error:
            self._finvoice_fail(error)
            return f"FAILED: {error.code}"
        for warning in (invoice_data.get("source_meta") or {}).get("warnings") or []:
            _logger.info("Invoice Import %s, Finvoice warning: %s", self.id, warning)

        # The context keeps the decoder from writing bank accounts (AC-06,
        # AC-07); the package's files go on the bill.
        main_attachment, extra_attachments = self._source_bill_attachments(APIX_READER)
        result = self.with_context(**{CTX_APIX_PACKAGE: True})._import_from_invoice_data(
            invoice_data,
            policy=self._import_readers()[APIX_READER].policy,
            main_attachment=main_attachment,
            extra_attachments=extra_attachments,
        )
        if self.move_id:
            # The bill is saved in this transaction: tell APIX once it has
            # committed. Also when only the post-processing failed —
            # the bill exists, and no later run imports this record again.
            self._on_settled()
        return result.detail or result.outcome

    def _apix_interrupted(self, code, message):
        """A technical stop: retried by the queue, or — on the last try —
        recorded as an interrupted failure the next run enqueues again."""
        self.ensure_one()
        if self._apix_is_last_attempt():
            self._fail(code, message, kind="interrupted")
            return f"FAILED: {code}"
        _logger.info(
            "APIX package import for record %s interrupted [%s]; the queue retries it",
            self.id,
            code,
        )
        raise RetryableJobError(f"{code}: {message}")

    def _apix_is_last_attempt(self):
        """Whether this run of the job is its last try. Outside a stored job
        (run at once, a test double) there is no retry, so it is."""
        uuid = self.env.context.get("job_uuid")
        job = uuid and self.env["queue.job"].sudo().search([("uuid", "=", uuid)], limit=1)
        if not job:
            return True
        if not job.max_retries:
            return False
        # `retry` counts the tries before this one (queue_job increments it
        # in memory while performing and stores it on a retryable error).
        return job.retry + 1 >= job.max_retries

    # ------------------------------------------------------------------
    # Mark received (apix.package.mark_received.requested.v1)
    # ------------------------------------------------------------------

    def _on_settled(self):
        """A bill was saved, or the record was closed without a bill: APIX
        hears of it through the mark-received job, enqueued in the
        caller's transaction. Then `_apix_after_package_settled` tells an
        add-on that cares (e.g. a migration source)."""
        result = super()._on_settled()
        packages = self.filtered(lambda r: r.source == "apix" and r.apix_storage_id)
        mirrored = packages._apix_in_mirror()
        if mirrored:
            # Never marked, now or after a switch to live: production
            # receives these packages.
            mirrored.sudo().write({"apix_mirrored": True})
        packages.filtered(
            lambda r: not r.apix_mark_confirmed and not r.apix_mirrored
        )._apix_enqueue_mark_received()
        if packages:
            packages._apix_after_package_settled()
        return result

    def _apix_after_package_settled(self):
        """Hook: these APIX records were just settled — a bill was saved or
        recognised for them, or they were closed without a bill. Called in
        the settling transaction, after the mark-received job was enqueued.
        Nothing here.

        An override (e.g. a migration source that tells an earlier connector
        its package was handled) must not raise: it would undo the bill or
        the close. It works in a savepoint and logs what it could not do,
        and it may not call APIX."""
        return None

    def _apix_in_mirror(self):
        """The records whose company receives in mirror mode now."""
        companies = self.company_id
        if not companies:
            return self.browse()
        mirroring = (
            self.env["goodahead.apix.connection"]
            .sudo()
            .search([("company_id", "in", companies.ids), ("receiving_state", "=", MIRROR)])
            .company_id
        )
        return self.filtered(lambda r: r.company_id in mirroring)

    def _apix_enqueue_mark_received(self):
        jobs = []
        for record in self:
            jobs.append(
                record.with_company(record.company_id).with_delay(
                    description=record._apix_job_description("mark received"),
                    identity_key=mark_identity_key(record.id),
                    channel=APIX_CHANNEL,
                    max_retries=MARK_MAX_RETRIES,
                )._apix_mark_received()
            )
        return jobs

    def _apix_mark_received(self):
        """Tell APIX the package was received.

        The job exists only if the bill or the close committed: `_on_settled`
        enqueued it in that transaction. Returns the job result.

        * Idempotent (events.md): nothing when `apix_mark_confirmed` is set,
          or when the record has no storage id or key to mark with.
        * Nothing while the record is not settled any more (reopened before
          this job ran, AC-03): a package is marked only for a saved bill or
          a close. The re-import or the next close enqueues the job again.
        * Shadow never marks anything (Flow F, AC-17): a log line and a
          chatter note, no APIX call; the record stays unconfirmed.
        * Download again with ``mark_received=True`` through the company's
          working connection; success sets `apix_mark_confirmed`.
        * Any APIX problem — rejected credentials included, which the run's
          to-do already covers — or no working connection: retried by the
          queue (5 tries, exponential backoff: the job function's retry
          pattern). The last try gives up quietly: the record stays
          unconfirmed and the daily reconciliation raises the to-do.

        Reopening a confirmed record does not un-mark it at APIX (there is no
        such call) and clears nothing: `apix_mark_confirmed` stays set and a
        re-import uses the kept files.

        🔴 Never log the storage key: logs and job results carry the record
        id and the storage id only.
        """
        self.ensure_one()
        record = self.sudo()
        if record.apix_mark_confirmed:
            return "OK: already confirmed"
        if record.apix_mirrored:
            _logger.info("APIX mark received for record %s skipped: mirrored", self.id)
            return "SKIPPED: mirrored"
        if not record.apix_storage_id or not record.apix_storage_key:
            _logger.info("APIX mark received for record %s skipped: no storage id or key", self.id)
            return "SKIPPED: no storage id or key"
        if record.state not in lifecycle.SETTLED_STATES and not record.move_id:
            _logger.info(
                "APIX mark received for record %s skipped: neither imported nor closed (state %s)",
                self.id,
                record.state,
            )
            return "SKIPPED: not settled"
        Connection = self.env["goodahead.apix.connection"].sudo()
        receiving = Connection.search([("company_id", "=", record.company_id.id)], limit=1)
        if receiving.receiving_state == MIRROR:
            _logger.info("APIX mark received for record %s skipped: mirror mode", self.id)
            return "SKIPPED: mirror"
        if receiving.receiving_state == "shadow":
            _logger.info(
                "APIX mark received for record %s skipped: receiving is in shadow mode", self.id
            )
            record.message_post(
                body=_("Not marked received at APIX: receiving is in shadow mode.")
            )
            return "SKIPPED: shadow"

        connection = Connection._get_working_connection(record.company_id)
        if not connection:
            return self._apix_mark_failed(
                "apix.no_connection", "the company has no working APIX connection"
            )
        try:
            connection._client().download(
                record.apix_storage_id, record.apix_storage_key, mark_received=True
            )
        except ApixError as exc:
            return self._apix_mark_failed(exc.code, exc.message)
        record.write(
            {"apix_mark_confirmed": True, "apix_status": apix_listing.STORAGE_RECEIVED}
        )
        record.message_post(body=_("Marked received at APIX."))
        _logger.info("APIX mark received for record %s confirmed", self.id)
        return "OK: marked received"

    def _apix_mark_failed(self, code, message):
        """A mark that did not go through: retried by the queue, or — on the
        last try — left unconfirmed for the daily reconciliation."""
        self.ensure_one()
        if not self._apix_is_last_attempt():
            _logger.info(
                "APIX mark received for record %s failed [%s]; the queue retries it",
                self.id,
                code,
            )
            raise RetryableJobError(f"{code}: {message}")
        _logger.warning(
            "APIX mark received for record %s gave up after the last try [%s]: %s; "
            "the record stays unconfirmed for the daily reconciliation",
            self.id,
            code,
            message,
        )
        self.sudo().message_post(
            body=_(
                "APIX could not be told this package was received (%(code)s). "
                "The daily check follows it up.",
                code=code,
            )
        )
        return f"FAILED: {code}"

    @api.model
    def _apix_unconfirmed_marks(self, company):
        """APIX records of ``company`` that have a bill or were closed without
        one but that APIX has not confirmed as received. Read by the daily
        reconciliation and the switch-back gate."""
        return self.sudo().search(
            [
                ("company_id", "=", company.id),
                ("source", "=", "apix"),
                ("apix_storage_id", "!=", False),
                ("apix_mark_confirmed", "=", False),
                # Mirrored packages belong to production's receiver.
                ("apix_mirrored", "=", False),
                "|",
                ("state", "in", list(lifecycle.SETTLED_STATES)),
                ("move_id", "!=", False),
            ]
        )

    def _apix_listing_vals(self, group):
        """Values refreshed from a listing group on every run."""
        return {
            "apix_storage_key": group.get("StorageKey") or False,
            "apix_listing_data": apix_listing.public_listing_data(group),
            "apix_status": apix_listing.storage_status(group) or False,
            "apix_document_date": apix_listing.parse_apix_date(group.get("DocumentDate")),
        }
