# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Receiving state and the per-company Scheduler run (sad.md §6 Flow A).

`apix.run.requested.v1`: the Scheduler, or an
Administrator's fetch now, enqueues one run per company, identity key
`apix-run:<company_id>`, channel `root.apix`. Each run is its own job and its
own transaction, so one company's failure never stops another.

A run never raises on an APIX problem: raising would roll back the very
failure record and to-do it has to leave behind. It classifies instead:

* receiving state off -> nothing happens;
* credentials rejected -> connection rejected (fetching stops until an
  Administrator's check succeeds) + one to-do;
* empty or unreadable answer, unreachable, anything unexpected -> failed run
  recorded on the connection + one to-do; never "no invoices";
* a valid listing (possibly empty) -> one import record per storage id, the
  listing-cap to-do at 500 packages, one package job per waiting
  record (live; mirror also for packages APIX shows as received).
"""

import logging

from odoo import _, api, fields, models
from odoo.addons.goodahead_apix.lib.apix_client import ApixError, ApixRejected
from odoo.addons.goodahead_invoice_import.lib import import_record as lifecycle
from odoo.exceptions import UserError
from odoo.tools import mute_logger
from psycopg2.errors import LockNotAvailable, UniqueViolation

from ..lib import apix_listing
from .goodahead_invoice_import import (
    ADMIN_GROUP,
    APIX_CHANNEL,
    MIRROR,
    PACKAGE_IDENTITY_PREFIX,
    SHADOW_CLEAR,
)

_logger = logging.getLogger(__name__)

#: Context key and marker of `_apix_set_receiving_state`.
CTX_RECEIVING_GUARD = "goodahead_apix_receiving_guard"
_RECEIVING_GUARD = object()

RUN_IDENTITY_PREFIX = "apix-run:"

#: Administrator to-do conditions. The key is written into the
#: summary as `[apix:<key>]` so an open to-do is found again instead of a
#: second one being created.
TODO_REJECTED = "rejected"
TODO_RUN_FAILED = "run-failed"
TODO_LISTING_CAP = "listing-cap"

#: Failed runs in a row after which the open to-do's deadline is moved to
#: today (Flow A: "failing for longer than two schedules -> escalate").
ESCALATE_AFTER_FAILURES = 2


def run_identity_key(company_id):
    return f"{RUN_IDENTITY_PREFIX}{company_id}"


def todo_marker(key):
    return f"[apix:{key}]"


class GoodaheadApixConnection(models.Model):
    _name = "goodahead.apix.connection"
    _inherit = ["goodahead.apix.connection", "mail.activity.mixin"]

    receiving_state = fields.Selection(
        [("off", "Off"), ("shadow", "Shadow"), (MIRROR, "Mirror"), ("live", "Live")],
        string="Receiving",
        required=True,
        default="off",
        tracking=True,
        help="Off: nothing is fetched. Shadow: packages are listed and "
        "classified but no bill is created and nothing is marked received "
        "(migration tool). Mirror: for a test or staging copy next to "
        "production — every listed package, received ones too, becomes a "
        "draft bill and nothing is ever marked received. Live: packages are "
        "imported as bills and marked received.",
    )
    receiving_state_since = fields.Datetime(
        string="Receiving state since",
        readonly=True,
        copy=False,
        help="When the receiving state last changed (the migration tool shows "
        "how long the shadow run has lasted, and since when receiving is live).",
    )
    last_run_date = fields.Datetime(string="Last run", readonly=True, copy=False)
    last_run_state = fields.Selection(
        [("success", "Succeeded"), ("failed", "Failed")],
        string="Last run result",
        readonly=True,
        copy=False,
    )
    last_run_message = fields.Char(string="Last run details", readonly=True, copy=False)
    run_failure_count = fields.Integer(
        string="Failed runs in a row",
        readonly=True,
        copy=False,
    )

    receiving_via_migration = fields.Boolean(
        compute="_compute_receiving_via_migration",
        help="Receiving goes live and back with the migration tool only: another "
        "APIX receiver is installed, or the credentials were carried over from one.",
    )

    # Who edits `receiving_state`: whoever may write the connection, i.e.
    # accounting administrators only (goodahead_apix access rights). Every
    # change goes through `_apix_check_receiving_change` (from `write`, so
    # the form's radio, an import or an RPC cannot bypass it):
    # * leaving live is refused while a mark is unconfirmed or a package
    #   job of the company runs (the switch-back gate, AC-19b);
    # * when `_apix_receiving_via_migration()` says so (another receiver is
    #   installed, or the connection was migrated from one), going live and
    #   leaving live happen in the migration tool only
    #   (`_apix_set_receiving_state`), which pauses and resumes the other
    #   receiver. This add-on knows no other receiver: the migration add-on
    #   answers the hook.

    def _compute_receiving_via_migration(self):
        for connection in self:
            connection.receiving_via_migration = connection._apix_receiving_via_migration()

    def _apix_receiving_via_migration(self):
        """Hook: whether this connection goes live and back through the
        migration tool only. False here; the migration add-on answers True
        while a migration source is installed or the connection was
        migrated from one."""
        self.ensure_one()
        return False

    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        for vals in vals_list:
            vals.setdefault("receiving_state_since", now)
        connections = super().create(vals_list)
        if self._apix_receiving_guarded():
            return connections
        for connection in connections:
            if connection.receiving_state != "live":
                continue
            if connection._apix_receiving_via_migration():
                raise UserError(self._apix_via_migration_message())
        return connections

    def write(self, vals):
        if "receiving_state" in vals:
            self._apix_check_receiving_change(vals["receiving_state"])
        if "receiving_state" in vals and "receiving_state_since" not in vals:
            changed = self.filtered(lambda c: c.receiving_state != vals["receiving_state"])
            if changed:
                super(GoodaheadApixConnection, changed).write(
                    {"receiving_state_since": fields.Datetime.now()}
                )
        return super().write(vals)

    # ------------------------------------------------------------------
    # Guarded receiving-state changes
    # ------------------------------------------------------------------

    def _apix_receiving_guarded(self):
        """Whether the change comes through `_apix_set_receiving_state`. The
        marker is a Python object, which no RPC call can put in a context."""
        return self.env.context.get(CTX_RECEIVING_GUARD) is _RECEIVING_GUARD

    @api.model
    def _apix_via_migration_message(self):
        return _(
            "Another APIX connector receives in this database (or this company was migrated "
            "from one): switch to live and back with the migration tool, which pauses and "
            "resumes it."
        )

    def _apix_set_receiving_state(self, state):
        """The migration tool's way to change receiving: the same checks as
        any change, without the "use the migration tool" refusal."""
        return self.with_context(**{CTX_RECEIVING_GUARD: _RECEIVING_GUARD}).write(
            {"receiving_state": state}
        )

    def _apix_check_receiving_change(self, state):
        guarded = self._apix_receiving_guarded()
        for connection in self:
            current = connection.sudo().receiving_state
            if current == state:
                continue
            if (
                not guarded
                and (state == "live" or current == "live")
                and connection._apix_receiving_via_migration()
            ):
                raise UserError(self._apix_via_migration_message())
            if current == "live":
                connection._apix_check_can_leave_live()

    def _apix_check_can_leave_live(self):
        """Refuse to stop receiving live while APIX has not confirmed a bill
        or close of this add-on (another receiver would import it again,
        AC-19b) or while a package or recognition job of the company runs.
        Takes the connection row's update lock first (NOWAIT): a package job
        holds a share lock on it while it bills (`_apix_lock_for_share`)."""
        self.ensure_one()
        connection = self.sudo()
        if not connection._apix_lock_for_update():
            raise UserError(
                _("A package of this company is being imported from APIX right now; try again in a minute.")
            )
        Import = self.env["goodahead.invoice.import"]
        unconfirmed = Import._apix_unconfirmed_marks(connection.company_id)
        if unconfirmed:
            raise UserError(
                _(
                    "%s records not yet confirmed as received by APIX — try again later.",
                    len(unconfirmed),
                )
            )
        if connection._apix_started_jobs():
            raise UserError(
                _("A package of this company is being imported from APIX right now; try again in a minute.")
            )

    def _apix_started_jobs(self):
        """The package-import and recognition jobs of this company that are
        running now."""
        self.ensure_one()
        jobs = (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("state", "=", "started"),
                    ("model_name", "=", "goodahead.invoice.import"),
                    "|",
                    ("identity_key", "=like", f"{PACKAGE_IDENTITY_PREFIX}%"),
                    ("identity_key", "=like", "apix-recognise:%"),
                ]
            )
        )
        company = self.sudo().company_id
        return jobs.filtered(lambda job: company in job.records.sudo().exists().company_id)

    def _apix_lock_row(self, mode):
        """Row lock on the connection (``mode``: "UPDATE" or "SHARE"),
        NOWAIT. False when another transaction holds a conflicting lock."""
        self.ensure_one()
        self.flush_recordset()
        try:
            with mute_logger("odoo.sql_db"), self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    f"SELECT id FROM goodahead_apix_connection WHERE id = %s FOR {mode} NOWAIT",
                    [self.id],
                )
        except LockNotAvailable:
            return False
        # What another transaction committed before the lock is what counts.
        self.invalidate_recordset()
        return True

    def _apix_lock_for_update(self):
        return self._apix_lock_row("UPDATE")

    def _apix_lock_for_share(self):
        return self._apix_lock_row("SHARE")

    # ------------------------------------------------------------------
    # Triggers: Scheduler and fetch now (apix.run.requested.v1)
    # ------------------------------------------------------------------

    @api.model
    def _cron_apix_run(self):
        """Recurring Scheduler: only enqueues, one run per company whose
        connection is working and whose receiving is not off."""
        connections = self.sudo().search(
            [("status", "=", "working"), ("receiving_state", "!=", "off")]
        )
        for connection in connections:
            connection._apix_enqueue_run()
        return True

    def _apix_enqueue_run(self):
        """Enqueue the run of this connection's company. Off or not working ->
        nothing (the run would do nothing anyway)."""
        self.ensure_one()
        connection = self.sudo()
        if connection.receiving_state == "off":
            return False
        if not self.env["goodahead.apix.connection"]._get_working_connection(connection.company_id):
            return False
        company = connection.company_id
        return (
            self.env["goodahead.apix.connection"]
            .sudo()
            .with_delay(
                description=f"APIX run: {company.name}",
                identity_key=run_identity_key(company.id),
                channel=APIX_CHANNEL,
                # No automatic retry: the next schedule is the retry (events.md).
                max_retries=1,
            )
            ._apix_run(company.id)
        )

    def action_fetch_now(self):
        """AC-12b: the same run the Scheduler would do, for this company only,
        following its receiving state."""
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP) and not self.env.su:
            raise UserError(_("Only accounting administrators can fetch from APIX."))
        if self.sudo().receiving_state == "off":
            return self._notify(_("Receiving is off for this company: nothing is fetched."), "warning")
        if self.status != "working":
            raise UserError(_("Check the APIX connection first: fetching needs a working connection."))
        self._apix_enqueue_run()
        return self._notify(_("Fetching from APIX started in the background."), "info")

    def action_start_mirror(self):
        """Mirror mode, from off or shadow: for a test or staging copy that
        receives next to production. Never from live, whose packages this
        database has to keep marking."""
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP) and not self.env.su:
            raise UserError(_("Only accounting administrators can change APIX receiving."))
        if self.sudo().receiving_state == "live":
            raise UserError(
                _("Receiving is live here: mirror mode is for a test or staging copy only.")
            )
        self._apix_set_receiving_state(MIRROR)
        return True

    def action_stop_mirror(self):
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP) and not self.env.su:
            raise UserError(_("Only accounting administrators can change APIX receiving."))
        if self.sudo().receiving_state == MIRROR:
            self._apix_set_receiving_state("off")
        return True

    def _apix_copy_warning(self):
        """Mirror mode means this copy holds production credentials: every
        sending surface warns (send AC-40). Otherwise the connection's own
        answer (neutralized production copy)."""
        if len(self) == 1 and self.sudo().receiving_state == MIRROR:
            return self._apix_copy_warning_text()
        return super()._apix_copy_warning()

    def _notify(self, message, kind):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": message, "type": kind, "sticky": False},
        }

    # ------------------------------------------------------------------
    # The run (Flow A)
    # ------------------------------------------------------------------

    @api.model
    def _apix_run(self, company_id):
        """One company's run. Returns a short outcome string (job result)."""
        company = self.env["res.company"].browse(company_id).exists()
        if not company:
            return "no company"
        Connection = self.sudo().with_company(company)
        connection = Connection._get_working_connection(company)
        if not connection:
            # Rejected or unchecked meanwhile: rejected credentials stop
            # fetching until an Administrator's check succeeds (AC-10/13).
            return "no working connection"
        if connection.receiving_state == "off":
            return "receiving off"

        try:
            groups = connection._client().list_packages(connection.transfer_id, connection.transfer_key)
        except ApixRejected as exc:
            connection._apix_run_rejected(exc)
            return "rejected"
        except ApixError as exc:
            # Empty, unreadable, unreachable, unexpected: a failed run, never
            # "no invoices"; the next schedule retries.
            connection._apix_run_failed(exc)
            return "failed"
        return connection._apix_process_listing(groups)

    def _apix_run_rejected(self, exc):
        self.ensure_one()
        message = _("APIX rejected the transfer credentials: %s", exc.message)
        self._set_status("rejected", message, fields.Datetime.now())
        self._apix_record_failed_run(message)
        self._apix_open_todo(
            TODO_REJECTED,
            _("APIX: credentials rejected — fetching stopped"),
            _(
                "APIX rejected this company's transfer credentials, so no e-invoices are "
                "fetched. Check the connection details and press Check connection; "
                "fetching resumes when the check succeeds.<br/>%s",
                exc.message,
            ),
        )

    def _apix_run_failed(self, exc):
        self.ensure_one()
        message = _("The APIX run failed (%(code)s): %(reason)s", code=exc.code, reason=exc.message)
        self._apix_record_failed_run(message)
        self._apix_open_todo(
            TODO_RUN_FAILED,
            _("APIX: fetching e-invoices failed"),
            _(
                "The last APIX run did not get a valid listing, so new e-invoices may be "
                "waiting at APIX. It is retried on the next run.<br/>%s",
                message,
            ),
            escalate=self.run_failure_count > ESCALATE_AFTER_FAILURES,
        )

    def _apix_record_failed_run(self, message):
        self.write(
            {
                "last_run_date": fields.Datetime.now(),
                "last_run_state": "failed",
                "last_run_message": message,
                "run_failure_count": self.run_failure_count + 1,
            }
        )
        _logger.info("APIX run for company %s failed", self.company_id.id)

    def _apix_process_listing(self, groups):
        """A valid listing (possibly empty): upsert, warn at the cap, enqueue."""
        self.ensure_one()
        records = self._apix_upsert_listing(groups)
        capped = apix_listing.is_capped(groups)
        if capped:
            self._apix_open_todo(
                TODO_LISTING_CAP,
                _("APIX: listing limit reached — some packages may not be visible"),
                _(
                    "APIX returned %(count)s packages, the most one listing can hold. "
                    "Packages beyond the limit may not be visible until others are "
                    "received. Every listed package is still processed.",
                    count=len(groups),
                ),
            )
        else:
            self._apix_close_todos([TODO_LISTING_CAP])
        enqueued = 0
        if self.receiving_state == "live":
            # Received elsewhere first, then import what waits.
            self._apix_recognise_received()
            enqueued = self._apix_enqueue_waiting()
        elif self.receiving_state == MIRROR:
            # No recognition: a package production received is billed too.
            enqueued = self._apix_enqueue_waiting(include_received=True)
        elif self.receiving_state == "shadow":
            self._apix_enqueue_shadow_classification()
        self.write(
            {
                "last_run_date": fields.Datetime.now(),
                "last_run_state": "success",
                "last_run_message": _(
                    "%(listed)s packages listed, %(new)s new, %(enqueued)s queued for import",
                    listed=len(groups),
                    new=len(records),
                    enqueued=enqueued,
                ),
                "run_failure_count": 0,
            }
        )
        self._apix_close_todos([TODO_REJECTED, TODO_RUN_FAILED])
        _logger.info(
            "APIX run for company %s: %s listed, %s new, %s queued",
            self.company_id.id,
            len(groups),
            len(records),
            enqueued,
        )
        return "capped" if capped else "success"

    def _apix_upsert_listing(self, groups):
        """One import record per (company, storage id). Returns the new ones.

        Known records get the listing refreshed and never change state here;
        in shadow, a package whose APIX status changed is classified again.
        Unknown packages:
        * NEW (still being created) and UNRECEIVED -> a waiting record;
        * RECEIVED -> a waiting record too: shadow classifies it (imported
          before / would be possibly lost); live hands it to the recognition
          (`_apix_recognise_received`), which links the bill it became or
          leaves it possibly lost — never imported
          automatically;
        * DELETED -> nothing (it is about to disappear at APIX).
        """
        self.ensure_one()
        company = self.company_id
        Import = self.env["goodahead.invoice.import"].sudo().with_company(company)
        packages = apix_listing.packages_by_storage_id(groups)
        if not packages:
            return Import.browse()
        existing = Import.search(
            [("company_id", "=", company.id), ("apix_storage_id", "in", list(packages))]
        )
        by_storage_id = {record.apix_storage_id: record for record in existing}
        to_create = []
        for storage_id, group in packages.items():
            vals = Import._apix_listing_vals(group)
            record = by_storage_id.get(storage_id)
            if (
                record
                and record.apix_mark_confirmed
                and vals.get("apix_status") == apix_listing.STORAGE_RECEIVED
            ):
                # Received and confirmed: its key is not needed, not kept.
                vals.pop("apix_storage_key", None)
            if record:
                if self.receiving_state == "shadow" and record.apix_status != vals["apix_status"]:
                    vals.update(SHADOW_CLEAR)
                record.write(vals)
                continue
            if not self._apix_creates_record_for(apix_listing.storage_status(group)):
                continue
            # Created in one batch rather than one `_create_for_source` call
            # per package: a listing holds up to 500 packages and the record
            # has no files yet (they come with the package job).
            to_create.append(
                dict(
                    vals,
                    name=self._apix_record_name(storage_id, group),
                    company_id=company.id,
                    source="apix",
                    state=lifecycle.STATE_WAITING,
                    apix_storage_id=storage_id,
                )
            )
        return self._apix_create_listed_records(to_create)

    def _apix_create_listed_records(self, vals_list):
        """Create the records of newly listed packages, with ON CONFLICT DO
        NOTHING semantics: when a run overlapping this one (a fetch now next
        to the Scheduler's) created a package's record first, that package is
        skipped instead of failing the whole run on the unique index. One
        batch normally; one savepoint per record only after a conflict."""
        self.ensure_one()
        Import = self.env["goodahead.invoice.import"].sudo().with_company(self.company_id)
        if not vals_list:
            return Import.browse()
        try:
            with mute_logger("odoo.sql_db"), self.env.cr.savepoint():
                return Import.create(vals_list)
        except UniqueViolation:
            pass
        created = Import.browse()
        for vals in vals_list:
            try:
                with mute_logger("odoo.sql_db"), self.env.cr.savepoint():
                    created |= Import.create(vals)
            except UniqueViolation:
                _logger.info(
                    "APIX run for company %s: package %s was listed by another run meanwhile",
                    self.company_id.id,
                    vals.get("apix_storage_id"),
                )
        return created

    def _apix_creates_record_for(self, status):
        return status != apix_listing.STORAGE_DELETED

    @api.model
    def _apix_record_name(self, storage_id, group):
        number = group.get("DocumentID") or storage_id
        seller = group.get("SenderName")
        return f"{seller} — {number}" if seller else f"APIX {number}"

    def _apix_enqueue_waiting(self, include_received=False):
        """Live and mirror: one package job per waiting APIX record of the
        company (plus interrupted failures, which the next run retries per
        events.md). NEW packages are shown as waiting but not fetched yet.
        ``include_received`` (mirror): packages APIX shows as received are
        imported too."""
        self.ensure_one()
        Import = self.env["goodahead.invoice.import"].sudo().with_company(self.company_id)
        received_domain = [
            # RECEIVED: someone else received it — recognised, not
            # imported (`_apix_recognise_received`), unless an Accountant
            # asked for the import.
            "|",
            ("apix_status", "!=", apix_listing.STORAGE_RECEIVED),
            ("apix_import_requested", "=", True),
        ]
        records = Import.search(
            [
                ("company_id", "=", self.company_id.id),
                ("source", "=", "apix"),
                ("apix_storage_id", "!=", False),
                ("move_id", "=", False),
                ("apix_status", "not in", [apix_listing.STORAGE_NEW, apix_listing.STORAGE_DELETED]),
                *([] if include_received else received_domain),
                # Reopened: waits for an explicit Retry.
                "|",
                ("apix_reopened", "=", False),
                ("apix_import_requested", "=", True),
                "|",
                ("state", "=", lifecycle.STATE_WAITING),
                "&",
                ("state", "=", lifecycle.STATE_FAILED),
                ("failure_kind", "=", "interrupted"),
            ]
        )
        records._apix_enqueue_package_import()
        return len(records)

    def _apix_recognise_received(self):
        """Live: waiting records APIX shows as received (by an earlier APIX
        connector, or by anybody but this add-on) become possibly lost and get
        a recognition job, which links the bill when one is found (Flow F,
        ADR-0012). Records whose job is running are left to it. Returns the
        records handed over.

        Records an Accountant already handled are not: one closed and
        reopened (`apix_reopened`; APIX shows it received because this add-on
        marked it at the close, `apix_mark_confirmed`) or one an Accountant
        asked to import — they wait for an explicit Retry."""
        self.ensure_one()
        Import = self.env["goodahead.invoice.import"].sudo().with_company(self.company_id)
        records = Import.search(
            [
                ("company_id", "=", self.company_id.id),
                ("source", "=", "apix"),
                ("apix_storage_id", "!=", False),
                ("move_id", "=", False),
                ("state", "=", lifecycle.STATE_WAITING),
                ("apix_status", "=", apix_listing.STORAGE_RECEIVED),
                ("apix_mark_confirmed", "=", False),
                ("apix_reopened", "=", False),
                ("apix_import_requested", "=", False),
            ]
        ).filtered(lambda r: r.display_state != lifecycle.STATE_PROCESSING)
        if records:
            records.write({"state": lifecycle.STATE_POSSIBLY_LOST, **SHADOW_CLEAR})
            records._apix_enqueue_recognition()
        return records

    def _apix_enqueue_shadow_classification(self):
        """Shadow: one classification job per listed package not classified
        yet. Settled records are not packages to receive."""
        self.ensure_one()
        Import = self.env["goodahead.invoice.import"].sudo().with_company(self.company_id)
        records = Import.search(
            [
                ("company_id", "=", self.company_id.id),
                ("source", "=", "apix"),
                ("apix_storage_id", "!=", False),
                ("move_id", "=", False),
                ("shadow_outcome", "=", False),
                ("state", "not in", list(lifecycle.SETTLED_STATES)),
                (
                    "apix_status",
                    "in",
                    [apix_listing.STORAGE_UNRECEIVED, apix_listing.STORAGE_RECEIVED],
                ),
            ]
        )
        records._apix_enqueue_shadow_classification()
        return records

    # ------------------------------------------------------------------
    # Administrator to-dos: one open per condition and company
    # ------------------------------------------------------------------

    def _apix_open_todos(self, key=None):
        self.ensure_one()
        activities = self.sudo().activity_ids
        if key is None:
            return activities.filtered(lambda a: (a.summary or "").find("[apix:") >= 0)
        marker = todo_marker(key)
        return activities.filtered(lambda a: marker in (a.summary or ""))

    def _apix_admin_user(self):
        """The Administrator the to-do is assigned to: the first active
        internal accounting administrator allowed in the company."""
        self.ensure_one()
        users = (
            self.env["res.users"]
            .sudo()
            .search(
                [
                    ("all_group_ids", "in", self.env.ref(ADMIN_GROUP).id),
                    ("company_ids", "in", self.company_id.id),
                    ("share", "=", False),
                ],
                order="id",
            )
        )
        users = users.filtered(lambda u: not u._is_superuser())
        return users[:1] or self.env.ref("base.user_admin", raise_if_not_found=False) or self.env.user

    def _apix_open_todo(self, key, title, note, escalate=False):
        """Open the to-do for ``key`` unless one is open already."""
        self.ensure_one()
        existing = self._apix_open_todos(key)
        if existing:
            if escalate:
                existing.write({"date_deadline": fields.Date.context_today(self)})
            return existing
        return self.sudo().activity_schedule(
            "mail.mail_activity_data_todo",
            summary=f"{title} {todo_marker(key)}",
            note=note,
            user_id=self._apix_admin_user().id,
        )

    def _apix_close_todos(self, keys):
        self.ensure_one()
        for key in keys:
            activities = self._apix_open_todos(key)
            if activities:
                activities.action_feedback(feedback=_("Resolved: the last APIX run succeeded."))
