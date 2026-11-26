# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Recognising packages imported before.

An earlier APIX connector may or may not record what it imported; what is
always left is APIX's received flag and the bills, with the package files on
them. A package is recognised:

0. by the earlier connector's own record, when it keeps one
   (`_apix_bill_from_earlier_receiver`, a hook: nothing here, the migration
   add-on asks the connector's migration source): exact, no download;
1. by the attachment checksum — a vendor bill of the company already carries
   a file with the same content as the package's Finvoice message or its
   invoice image: certain;
2. otherwise by the duplicate rule (vendor company and normalised invoice
   number), with the seller-identifier fallback: the seller's VAT number or
   Business ID also find a bill booked on another vendor record (the first
   adopter has many duplicate vendors).

A package matched by none is not recognised. Two jobs use this:

* the shadow classification (`_apix_shadow_classify`): stores
  the shadow results (outcome, vendor decision, failure reason), nothing
  else — no bill, no mark, never;
* the live recognition of packages APIX shows as received but that have no
  bill here (`_apix_recognise_received`): a match links the bill (imported,
  confirmed at APIX); no match leaves the record possibly lost for an
  Accountant. Nothing is imported automatically.

Both download WITHOUT marking received and keep nothing of the package.
"""

import hashlib
import logging
from collections import namedtuple

from markupsafe import Markup
from odoo import _, models
from odoo.addons.goodahead_apix.lib.apix_client import ApixError
from odoo.addons.goodahead_invoice_import.lib import import_record as lifecycle
from odoo.addons.goodahead_invoice_import.lib.import_source import STRICT_POLICY
from odoo.addons.goodahead_invoice_import.lib.invoice_data import InvoiceDataError
from odoo.addons.goodahead_invoice_import_finvoice.lib import finvoice
from odoo.addons.queue_job.exception import RetryableJobError

from ..lib import apix_listing, apix_package
from .goodahead_invoice_import import (
    APIX_CHANNEL,
    LOCKED_RETRY_SECONDS,
    PACKAGE_MAX_RETRIES,
    unpack_package,
)

_logger = logging.getLogger(__name__)

SHADOW_IDENTITY_PREFIX = "apix-shadow:"
RECOGNISE_IDENTITY_PREFIX = "apix-recognise:"

#: `Recognition.kind`
MATCH_EARLIER = "earlier"  # the earlier connector recorded this package's bill
MATCH_ATTACHMENT = "attachment"  # a bill carries the package's file
MATCH_BILL = "bill"  # the duplicate rule found the bill
NO_MATCH = "none"
UNREADABLE = "unreadable"  # not a package, no Finvoice, bad invoice data

BILL_TYPES = ("in_invoice", "in_refund")

#: `Recognition.vendor_match` (the `shadow_vendor_match` values)
VENDOR_MATCHED = "matched"
VENDOR_AMBIGUOUS = "ambiguous"
VENDOR_NONE = "none"

#: ``vendor`` / ``vendor_match``: who the vendor would be under the APIX
#: matching policy (False when the package could not be read that far).
Recognition = namedtuple(
    "Recognition", "kind move reason vendor vendor_match", defaults=(None, False)
)


def shadow_identity_key(record_id):
    return f"{SHADOW_IDENTITY_PREFIX}{record_id}"


def recognise_identity_key(record_id):
    return f"{RECOGNISE_IDENTITY_PREFIX}{record_id}"


def shadow_outcome_for(recognition, apix_status):
    """The `shadow_outcome` a recognition stands for: what a live
    run would do with the package."""
    received = apix_status == apix_listing.STORAGE_RECEIVED
    if recognition.kind in (MATCH_EARLIER, MATCH_ATTACHMENT):
        return "imported_before"
    if recognition.kind == MATCH_BILL:
        return "imported_before" if received else "would_duplicate"
    if recognition.kind == UNREADABLE:
        return "would_fail"
    return "would_be_possibly_lost" if received else "would_import"


class GoodaheadInvoiceImport(models.Model):
    _inherit = "goodahead.invoice.import"

    # ------------------------------------------------------------------
    # Recognition
    # ------------------------------------------------------------------

    def _apix_recognise(self):
        """Look for the bill this record's package became: first the earlier
        connector's own record of it (no download), then download the
        package without marking it received and match its files and its
        invoice. Returns a `Recognition`; raises `ApixError` on a technical
        problem (the caller retries)."""
        self.ensure_one()
        record = self.sudo()
        earlier = record._apix_earlier_bill()
        if earlier:
            return Recognition(MATCH_EARLIER, earlier, False, earlier.partner_id, VENDOR_MATCHED)
        if not record.apix_storage_id or not record.apix_storage_key:
            raise ApixError("APIX has not given a download key for this package yet")
        connection = (
            self.env["goodahead.apix.connection"].sudo()._get_working_connection(record.company_id)
        )
        if not connection:
            raise ApixError("the company has no working APIX connection")
        # 🔴 mark_received=False, always: recognising is reading, never receiving.
        body = connection._client().download(
            record.apix_storage_id, record.apix_storage_key, mark_received=False
        )
        listing = record.apix_listing_data or {}
        try:
            package = unpack_package(body, listing.get("DocumentName"), listing.get("ImageName"))
        except apix_package.PackageError as error:
            if error.code == apix_package.TRUNCATED:
                raise ApixError(error.message) from None
            return Recognition(UNREADABLE, None, error.message)
        if package.finvoice is None:
            return Recognition(UNREADABLE, None, _("The APIX package holds no Finvoice message."))

        files = [package.finvoice_file, package.image_file]
        attached_to = record._apix_bill_with_file([f.content for f in files if f is not None])

        try:
            messages = finvoice.parse(package.finvoice_file.content)
            invoice_data = finvoice.to_invoice_data(messages[0])
        except finvoice.FinvoiceError as error:
            if attached_to:
                return Recognition(
                    MATCH_ATTACHMENT, attached_to, False, attached_to.partner_id, VENDOR_MATCHED
                )
            return Recognition(UNREADABLE, None, str(error))
        vendor, vendor_match = record._apix_vendor_decision(invoice_data)
        if attached_to:
            vendor = vendor or attached_to.partner_id
            return Recognition(MATCH_ATTACHMENT, attached_to, False, vendor, VENDOR_MATCHED)
        if len(messages) > 1:
            # The package job refuses it the same way (one package, one bill).
            return Recognition(
                UNREADABLE,
                None,
                _(
                    "The APIX package holds %s invoices (Finvoice messages); one package is "
                    "imported as one bill.",
                    len(messages),
                ),
                vendor,
                vendor_match,
            )
        move = record._apix_bill_by_duplicate_rule(invoice_data, vendor)
        if move:
            return Recognition(MATCH_BILL, move, False, vendor, vendor_match)
        try:
            record._import_validator()._validate_extraction(invoice_data)
        except InvoiceDataError as error:
            return Recognition(UNREADABLE, None, str(error), vendor, vendor_match)
        return Recognition(NO_MATCH, None, False, vendor, vendor_match)

    def _apix_bill_from_earlier_receiver(self):
        """Hook: the vendor bill an earlier APIX connector recorded for this
        package (company and storage id), or an empty recordset. Nothing
        here; the migration add-on asks the migration source the company
        was migrated from (`_migration_<code>_find_bill`)."""
        self.ensure_one()
        return self.env["account.move"]

    def _apix_earlier_bill(self):
        """`_apix_bill_from_earlier_receiver`, kept to what a recognition may
        link: one vendor bill or credit note of the record's company that is
        not cancelled, or an empty move."""
        self.ensure_one()
        Move = self.env["account.move"]
        if not self.apix_storage_id:
            return Move
        moves = (
            self._apix_bill_from_earlier_receiver()
            .sudo()
            .exists()
            .filtered(
                lambda m: (
                    m.company_id == self.company_id
                    and m.move_type in BILL_TYPES
                    and m.state != "cancel"
                )
            )
        )
        return Move.browse(moves.sorted("id")[:1].ids)

    def _apix_vendor_decision(self, invoice_data):
        """``(vendor, match)`` under the APIX matching policy:
        ``match`` is ``matched``, ``ambiguous`` (several vendors share the
        seller's identifiers, or they name different vendors) or ``none``."""
        self.ensure_one()
        decision = self.env["res.partner"]._ai_vendor_decision(
            invoice_data.get("seller"), company=self.company_id, policy=STRICT_POLICY
        )
        vendor = decision["vendor"]
        if vendor:
            return vendor, VENDOR_MATCHED
        if decision["reason"] in ("ambiguous", "conflict"):
            return vendor, VENDOR_AMBIGUOUS
        return vendor, VENDOR_NONE

    def _apix_bill_with_file(self, contents):
        """The company's vendor bill (not cancelled) that carries a file with
        the same content as one of ``contents``, or an empty move. Only the
        Finvoice message and the invoice image are compared: other package
        files (terms, price lists) can be the same on many invoices."""
        self.ensure_one()
        Move = self.env["account.move"]
        checksums = list({hashlib.sha1(content).hexdigest() for content in contents if content})
        if not checksums:
            return Move
        attachments = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("checksum", "in", checksums),
                    ("res_model", "=", "account.move"),
                    ("res_id", "!=", False),
                ]
            )
        )
        moves = (
            Move.sudo()
            .browse(attachments.mapped("res_id"))
            .exists()
            .filtered(
                lambda m: (
                    m.company_id == self.company_id
                    and m.move_type in BILL_TYPES
                    and m.state != "cancel"
                )
            )
        )
        return Move.browse(moves.sorted("id")[:1].ids)

    def _apix_bill_by_duplicate_rule(self, invoice_data, vendor):
        """The duplicate rule with ``vendor`` (the APIX matching
        policy's decision), then — when the vendor resolved but its bill was
        not found — once more by the seller's identifiers alone, so a bill an
        earlier connector booked on another (duplicate) vendor record is
        found too."""
        self.ensure_one()
        Partner = self.env["res.partner"]
        move = self._import_check_duplicate(invoice_data, vendor)[:1]
        if not move and vendor:
            move = self._import_check_duplicate(invoice_data, Partner.browse())[:1]
        return move

    # ------------------------------------------------------------------
    # Shadow classification: no bill, no mark, ever
    # ------------------------------------------------------------------

    def _apix_enqueue_shadow_classification(self):
        jobs = []
        for record in self:
            jobs.append(
                record.with_company(record.company_id).with_delay(
                    description=record._apix_job_description("shadow classification"),
                    identity_key=shadow_identity_key(record.id),
                    channel=APIX_CHANNEL,
                    max_retries=PACKAGE_MAX_RETRIES,
                )._apix_shadow_classify()
            )
        return jobs

    def _apix_shadow_classify(self):
        """Store what a live run would do with this package (Flow F): the
        shadow results only (outcome, vendor decision, failure reason). A
        technical problem is retried; the last try
        leaves the outcome unset, so the next shadow run classifies it."""
        self.ensure_one()
        record = self.sudo()
        receiving = (
            self.env["goodahead.apix.connection"]
            .sudo()
            .search([("company_id", "=", record.company_id.id)], limit=1)
        )
        if receiving.receiving_state != "shadow":
            return "SKIPPED: not in shadow"
        if record.move_id or record.state in lifecycle.SETTLED_STATES:
            return "SKIPPED: settled"
        try:
            recognition = record._apix_recognise()
        except ApixError as exc:
            if self._apix_is_last_attempt():
                _logger.info(
                    "APIX shadow classification for record %s gave up [%s]; the next run retries",
                    self.id,
                    exc.code,
                )
                return f"FAILED: {exc.code}"
            raise RetryableJobError(f"{exc.code}: {exc.message}") from None
        outcome = shadow_outcome_for(recognition, record.apix_status)
        # AC-17: what a live run would do, who the vendor would be, and why a
        # package would fail.
        record.write(
            {
                "shadow_outcome": outcome,
                "shadow_vendor_id": recognition.vendor.id if recognition.vendor else False,
                "shadow_vendor_match": recognition.vendor_match or False,
                "shadow_reason": recognition.reason if outcome == "would_fail" else False,
            }
        )
        _logger.info("APIX shadow classification for record %s: %s", self.id, outcome)
        return f"OK: {outcome}"

    # ------------------------------------------------------------------
    # Live: packages received at APIX without a bill here
    # ------------------------------------------------------------------

    def _apix_enqueue_recognition(self):
        """Possibly lost until recognised: the job links the bill when it
        finds one. Linked to the record, so it shows as processing."""
        job = None
        for record in self:
            job = record.with_company(record.company_id).with_delay(
                description=record._apix_job_description("recognise received package"),
                identity_key=recognise_identity_key(record.id),
                channel=APIX_CHANNEL,
                max_retries=PACKAGE_MAX_RETRIES,
            )._apix_recognise_received()
            record._link_job(job)
        return job

    def _apix_recognise_received(self):
        """One package APIX shows as received, with no bill here.

        * matched (earlier connector's record, attachment or duplicate rule)
          and the bill is no other
          record's -> imported, the bill linked, confirmed at APIX: recognised,
          never imported again;
        * matched, but the bill already belongs to another record -> duplicate
          of it, for an Accountant;
        * not matched or unreadable -> stays possibly lost: an Accountant
          imports or closes it.
        """
        self.ensure_one()
        if not self._apix_lock():
            raise RetryableJobError(
                "APIX recognition: the record is locked by another transaction",
                seconds=LOCKED_RETRY_SECONDS,
                ignore_retry=True,
            )
        self.invalidate_recordset()
        record = self.sudo()
        if record.move_id or record.state != lifecycle.STATE_POSSIBLY_LOST:
            return "SKIPPED: no longer possibly lost"
        try:
            recognition = record._apix_recognise()
        except ApixError as exc:
            if not self._apix_is_last_attempt():
                raise RetryableJobError(f"{exc.code}: {exc.message}") from None
            record.message_post(
                body=_(
                    "APIX shows this package as received, but it could not be downloaded to "
                    "look for its bill (%s). Import or close it.",
                    exc.code,
                )
            )
            return f"FAILED: {exc.code}"

        if recognition.kind in (MATCH_EARLIER, MATCH_ATTACHMENT, MATCH_BILL):
            return record._apix_link_recognised_bill(recognition)
        if recognition.kind == UNREADABLE:
            record.write({"error_message": recognition.reason})
        record.message_post(
            body=_(
                "APIX shows this package as received, but no bill of it was found here. "
                "It may have been lost by the earlier import: import it, or close it "
                "without a bill."
            )
        )
        return "OK: possibly lost"

    def _apix_link_recognised_bill(self, recognition):
        self.ensure_one()
        move = recognition.move
        owner = self.search([("move_id", "=", move.id), ("id", "!=", self.id)], limit=1)
        how = {
            MATCH_EARLIER: _("the earlier APIX connector recorded this package on it"),
            MATCH_ATTACHMENT: _("the same file is attached to it"),
        }.get(recognition.kind) or _("same vendor and invoice number")
        if owner:
            self.write(
                {
                    "state": lifecycle.STATE_DUPLICATE,
                    "duplicate_of_move_id": move.id,
                    "error_message": False,
                }
            )
            self.message_post(
                body=Markup(
                    _("Received at APIX; its bill %(bill)s (%(how)s) belongs to another import.")
                )
                % {"bill": move._get_html_link(), "how": how}
            )
            return "OK: duplicate"
        self.write(
            {
                "state": lifecycle.STATE_IMPORTED,
                "move_id": move.id,
                "apix_mark_confirmed": self.apix_status == apix_listing.STORAGE_RECEIVED,
                "error_message": False,
                "failure_kind": False,
            }
        )
        self.message_post(
            body=Markup(
                _(
                    "Imported before the switch: recognised as %(bill)s (%(how)s). "
                    "Not imported again."
                )
            )
            % {"bill": move._get_html_link(), "how": how}
        )
        self._apix_after_package_settled()
        return "OK: recognised"
