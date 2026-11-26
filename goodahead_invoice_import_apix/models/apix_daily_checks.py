# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The daily ageing and reconciliation checks (sad.md §6 Flow H).

`apix.daily_checks.requested.v1`: a daily recurring job
enqueues one check per company whose receiving is shadow or live (whatever
its connection status), identity key `apix-daily:<company_id>:<date>`,
channel `root.apix`. No retry: the next day retries.

* Ageing: APIX lists the current and the previous calendar month.
  Unsettled APIX records within 7 days of leaving that window (or past it)
  raise one `[apix:ageing]` to-do with seller, invoice number and age of each;
  it is closed when none remain.
* Reconciliation (spec §6 "Loss / duplication"): the listing is compared with
  the inbox; any difference raises one `[apix:reconciliation]` to-do, closed
  when the comparison is clean. A failed listing, or no working connection,
  skips it with a log line: the hourly run already raises its own to-do.
"""

import logging

from markupsafe import Markup

from odoo import _, api, fields, models

from odoo.addons.goodahead_apix.lib.apix_client import ApixError
from odoo.addons.goodahead_invoice_import.lib import import_record as lifecycle

from ..lib import apix_listing, apix_window
from .goodahead_invoice_import import APIX_CHANNEL

_logger = logging.getLogger(__name__)

DAILY_IDENTITY_PREFIX = "apix-daily:"

TODO_AGEING = "ageing"
TODO_RECONCILIATION = "reconciliation"

#: Unsettled states an APIX record can wait in (ageing, reconciliation b/c).
UNSETTLED_STATES = (
    lifecycle.STATE_WAITING,
    lifecycle.STATE_FAILED,
    lifecycle.STATE_DUPLICATE,
    lifecycle.STATE_POSSIBLY_LOST,
)

#: Reconciliation difference kinds.
DIFF_NOT_MARKED = "not_marked"  # (a) settled in the inbox, not received at APIX
DIFF_RECEIVED_UNSETTLED = "received_unsettled"  # (b) received at APIX, unsettled here
DIFF_RECEIVED_UNKNOWN = "received_unknown"  # (b) received at APIX, unknown here (live)
DIFF_GONE = "gone"  # (c) unsettled here, no longer listed by APIX


def daily_identity_key(company_id, day):
    return f"{DAILY_IDENTITY_PREFIX}{company_id}:{fields.Date.to_string(day)}"


class GoodaheadApixConnection(models.Model):
    _inherit = "goodahead.apix.connection"

    # ------------------------------------------------------------------
    # Trigger (apix.daily_checks.requested.v1)
    # ------------------------------------------------------------------

    @api.model
    def _cron_apix_daily_checks(self):
        """Daily recurring job: only enqueues, one check per company whose
        receiving is shadow or live — whatever its connection's status:
        packages age out of the listing also while the connection is
        rejected. Mirror mode has no checks: production's receiver owns the
        packages."""
        connections = self.sudo().search([("receiving_state", "in", ["shadow", "live"])])
        today = fields.Date.context_today(self)
        for connection in connections:
            connection._apix_enqueue_daily_checks(today)
        return True

    def _apix_enqueue_daily_checks(self, today=None):
        self.ensure_one()
        company = self.sudo().company_id
        today = today or fields.Date.context_today(self)
        return (
            self.env["goodahead.apix.connection"]
            .sudo()
            .with_delay(
                description=f"APIX daily checks: {company.name}",
                identity_key=daily_identity_key(company.id, today),
                channel=APIX_CHANNEL,
                # No automatic retry: the next day is the retry (events.md).
                max_retries=1,
            )
            ._apix_daily_checks(company.id)
        )

    # ------------------------------------------------------------------
    # The checks (Flow H)
    # ------------------------------------------------------------------

    @api.model
    def _apix_daily_checks(self, company_id, today=None):
        """One company's daily checks. ``today`` is for tests; the job uses
        the current date. Returns a short outcome string (job result)."""
        company = self.env["res.company"].browse(company_id).exists()
        if not company:
            return "no company"
        Connection = self.sudo().with_company(company)
        connection = Connection.search([("company_id", "=", company.id)], limit=1)
        if not connection:
            return "no connection"
        if connection.receiving_state not in ("shadow", "live"):
            return "receiving off"
        today = fields.Date.to_date(today) if today else fields.Date.context_today(connection)

        # Ageing needs the inbox only; the reconciliation lists the packages,
        # so it needs a working connection (a rejected one has its own to-do).
        ageing = connection._apix_check_ageing(today)
        if Connection._get_working_connection(company):
            reconciliation = connection._apix_check_reconciliation()
        else:
            _logger.info(
                "APIX daily reconciliation for company %s skipped: no working connection",
                company.id,
            )
            reconciliation = "skipped (no working connection)"
        return f"ageing: {ageing}; reconciliation: {reconciliation}"

    def _apix_unsettled_records(self):
        self.ensure_one()
        return (
            self.env["goodahead.invoice.import"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("source", "=", "apix"),
                    ("apix_storage_id", "!=", False),
                    ("move_id", "=", False),
                    ("state", "in", list(UNSETTLED_STATES)),
                ],
                order="apix_document_date, id",
            )
        )

    # -- ageing -------------------------------------------------

    def _apix_check_ageing(self, today):
        """Open, refresh or close the `[apix:ageing]` to-do. Returns the
        number of ageing records."""
        self.ensure_one()
        ageing = []
        for record in self._apix_unsettled_records():
            document_date = _document_date(record)
            if apix_window.is_ageing(document_date, today):
                ageing.append((record, document_date))
        if not ageing:
            self._apix_resolve_todo(
                TODO_AGEING, _("Resolved: no unsettled APIX package is about to leave the listing.")
            )
            return 0
        lines = [
            _(
                "%(seller)s, invoice %(number)s: %(age)s days old, %(when)s",
                seller=_seller(record),
                number=_invoice_number(record),
                age=apix_window.age_days(document_date, today),
                when=_leaves_listing(document_date, today),
            )
            for record, document_date in ageing
        ]
        self._apix_upsert_todo(
            TODO_AGEING,
            _("APIX: %s unsettled packages about to leave the listing", len(ageing)),
            _(
                "APIX lists invoices of the current and the previous month only. These "
                "packages are not settled yet and will soon no longer be listed: import or "
                "close them before they disappear from the listing."
            ),
            lines,
        )
        return len(ageing)

    # -- reconciliation (spec §6 Loss / duplication) ---------------------

    def _apix_check_reconciliation(self):
        """List the packages once and compare them with the inbox. Returns
        the number of differences, or "skipped" when the listing failed."""
        self.ensure_one()
        try:
            groups = self._client().list_packages(self.transfer_id, self.transfer_key)
        except ApixError as exc:
            # The hourly run raises its own to-do for a failing listing.
            _logger.info(
                "APIX daily reconciliation for company %s skipped: the listing failed [%s]",
                self.company_id.id,
                exc.code,
            )
            return "skipped"
        differences = self._apix_reconciliation_differences(
            apix_listing.packages_by_storage_id(groups)
        )
        self._apix_reenqueue_marks(
            [record for kind, record, _storage_id in differences if kind == DIFF_NOT_MARKED]
        )
        if not differences:
            self._apix_resolve_todo(
                TODO_RECONCILIATION, _("Resolved: APIX and the import inbox agree.")
            )
            return 0
        self._apix_upsert_todo(
            TODO_RECONCILIATION,
            _("APIX: %s differences between APIX and the import inbox", len(differences)),
            _(
                "The daily comparison of APIX's received status with the import inbox "
                "found these differences. Marks APIX has not confirmed are being sent again."
            ),
            [_difference_line(kind, record, storage_id) for kind, record, storage_id in differences],
        )
        return len(differences)

    def _apix_reconciliation_differences(self, packages):
        """``packages``: `{storage_id: group}` of one listing. Returns
        `[(kind, record or None, storage_id)]`."""
        self.ensure_one()
        company = self.company_id
        Import = self.env["goodahead.invoice.import"].sudo()
        differences = []

        # (a) settled in the inbox, but APIX has not confirmed it or still
        # lists it as not received.
        not_marked = Import._apix_unconfirmed_marks(company)
        settled_listed = Import.search(
            [
                ("company_id", "=", company.id),
                ("source", "=", "apix"),
                ("apix_storage_id", "in", list(packages)),
                ("id", "not in", not_marked.ids),
                # Mirrored packages are production's to mark.
                ("apix_mirrored", "=", False),
                "|",
                ("state", "in", list(lifecycle.SETTLED_STATES)),
                ("move_id", "!=", False),
            ]
        )
        for record in settled_listed:
            status = apix_listing.storage_status(packages[record.apix_storage_id])
            if status in (apix_listing.STORAGE_NEW, apix_listing.STORAGE_UNRECEIVED):
                not_marked |= record
        for record in not_marked.sorted("id"):
            differences.append((DIFF_NOT_MARKED, record, record.apix_storage_id))

        # (b) received at APIX, unsettled here or (live) unknown here.
        received_ids = [
            storage_id
            for storage_id, group in packages.items()
            if apix_listing.storage_status(group) == apix_listing.STORAGE_RECEIVED
        ]
        known = Import.search(
            [("company_id", "=", company.id), ("apix_storage_id", "in", received_ids)]
        )
        by_storage_id = {record.apix_storage_id: record for record in known}
        for storage_id in received_ids:
            record = by_storage_id.get(storage_id)
            if record is None:
                if self.receiving_state == "live":
                    differences.append((DIFF_RECEIVED_UNKNOWN, None, storage_id))
                continue
            if record.state in UNSETTLED_STATES and not record.move_id:
                differences.append((DIFF_RECEIVED_UNSETTLED, record, storage_id))

        # (c) unsettled here, no longer listed.
        for record in self._apix_unsettled_records():
            if record.apix_storage_id not in packages:
                differences.append((DIFF_GONE, record, record.apix_storage_id))
        return differences

    def _apix_reenqueue_marks(self, records):
        """Send the marks of (a) again — live only: shadow never marks.
       A record APIX still lists as not received is unconfirmed
        again, so the mark job (which skips confirmed records) runs."""
        self.ensure_one()
        if self.receiving_state != "live" or not records:
            return
        Import = self.env["goodahead.invoice.import"].sudo()
        to_mark = Import.browse([record.id for record in records])
        to_mark.filtered("apix_mark_confirmed").write({"apix_mark_confirmed": False})
        to_mark._apix_enqueue_mark_received()

    # -- to-dos ----------------------------------------------------------

    def _apix_upsert_todo(self, key, title, intro, lines):
        """One open to-do per ``key``: opened, or its text refreshed."""
        self.ensure_one()
        note = Markup("<p>%s</p><ul>%s</ul>") % (
            intro,
            Markup("").join(Markup("<li>%s</li>") % line for line in lines),
        )
        existing = self._apix_open_todos(key)
        if existing:
            existing.write({"summary": f"{title} [apix:{key}]", "note": note})
            return existing
        return self._apix_open_todo(key, title, note)

    def _apix_resolve_todo(self, key, feedback):
        self.ensure_one()
        activities = self._apix_open_todos(key)
        if activities:
            activities.action_feedback(feedback=feedback)


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------


def _document_date(record):
    if record.apix_document_date:
        return record.apix_document_date
    return record.create_date.date() if record.create_date else None


def _seller(record):
    listing = record.apix_listing_data or {}
    return listing.get("SenderName") or _("unknown seller")


def _invoice_number(record):
    listing = record.apix_listing_data or {}
    return listing.get("DocumentID") or record.name or record.apix_storage_id


def _leaves_listing(document_date, today):
    left = apix_window.days_left(document_date, today)
    if left <= 0:
        return _("no longer listed by APIX")
    return _("leaves the listing in %s days", left)


def _difference_line(kind, record, storage_id):
    storage = storage_id
    if kind == DIFF_NOT_MARKED:
        return _(
            "%(seller)s, invoice %(number)s (storage %(storage)s): settled here, "
            "but APIX has not confirmed it as received — the mark is sent again",
            seller=_seller(record),
            number=_invoice_number(record),
            storage=storage,
        )
    if kind == DIFF_RECEIVED_UNSETTLED:
        return _(
            "%(seller)s, invoice %(number)s (storage %(storage)s): received at APIX, "
            "but %(state)s here",
            seller=_seller(record),
            number=_invoice_number(record),
            storage=storage,
            state=record.state,
        )
    if kind == DIFF_RECEIVED_UNKNOWN:
        return _(
            "Storage %s: received at APIX, but unknown to the import inbox", storage
        )
    return _(
        "%(seller)s, invoice %(number)s (storage %(storage)s): %(state)s here, "
        "but gone from the APIX listing",
        seller=_seller(record),
        number=_invoice_number(record),
        storage=storage,
        state=record.state,
    )
