# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The migration tool: moving one company at a time from an earlier APIX
connector to the APIX receiving add-on.

Per company: detect the earlier connector's credentials and carry them over
(never a web password), run in shadow, switch to live, switch back.

The earlier connectors are **migration sources**, each implemented by an
adapter add-on (``goodahead_invoice_import_apix_migration_<x>``) that knows
that connector. This add-on knows none of them. It finds them through a
registry and calls them through code-based dispatch, like the import core's
sources: ``getattr(self, f"_migration_{code}_{method}")``. The contract is below and
in this add-on's README ("Writing an adapter for another connector").

Registry (on ``goodahead.apix.connection``, super-chained by the adapters):

* ``_migration_sources()`` -> list of the installed source codes;
* ``_migration_source_label(code)`` -> the name shown to the Administrator.

Per source ``<code>``, required: ``_migration_<code>_detect(company)``,
``_migration_<code>_status()``, ``_migration_<code>_pause(company)``,
``_migration_<code>_resume(company)``; optional:
``_migration_<code>_find_bill(company, storage_id)``.
"""

import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.goodahead_invoice_import.lib import import_record as lifecycle
from odoo.addons.goodahead_invoice_import_apix.lib import apix_listing
from odoo.addons.goodahead_invoice_import_apix.models.goodahead_invoice_import import (
    ADMIN_GROUP,
    SHADOW_CLEAR,
    package_identity_key,
)

_logger = logging.getLogger(__name__)

#: What `_migration_<code>_detect` may return, and all the carry-over copies.
#: 🔴 Never a web password nor the user name it belongs to.
CREDENTIAL_KEYS = ("environment", "transfer_id", "transfer_key", "business_id")

#: Methods every source implements; `find_bill` is optional.
REQUIRED_METHODS = ("detect", "status", "pause", "resume")
OPTIONAL_METHODS = ("find_bill",)

#: `_migration_<code>_status()` values (first item of the returned pair).
SOURCE_OK = "ok"  # the connector is the version the adapter was written for
SOURCE_MISMATCH = "mismatch"  # it changed under the adapter: no switch

#: Shadow run length the migration tool recommends before going live (spec
#: §8, default): at least a week, with no package unaccounted for.
SHADOW_MIN_DAYS = 7

#: Records not settled at the switch back: the earlier connector takes over.
UNSETTLED_STATES = (
    lifecycle.STATE_WAITING,
    lifecycle.STATE_FAILED,
    lifecycle.STATE_DUPLICATE,
    lifecycle.STATE_POSSIBLY_LOST,
)


class GoodaheadApixConnection(models.Model):
    _inherit = "goodahead.apix.connection"

    migrated_from = fields.Char(
        string="Migrated from (code)",
        readonly=True,
        copy=False,
        help="Code of the migration source the transfer credentials were carried "
        "over from (set by the migration tool), or empty.",
    )
    migrated_from_label = fields.Char(
        string="Migrated from",
        compute="_compute_migrated_from_label",
    )

    @api.depends("migrated_from")
    def _compute_migrated_from_label(self):
        for connection in self:
            code = connection.migrated_from
            connection.migrated_from_label = self._migration_source_label(code) if code else False

    # ------------------------------------------------------------------
    # Hook of the receiving add-on
    # ------------------------------------------------------------------

    def _apix_receiving_via_migration(self):
        """Live and back through the migration tool only while a migration
        source is installed (its connector may still receive for the
        company) or the connection was migrated."""
        self.ensure_one()
        if super()._apix_receiving_via_migration():
            return True
        return bool(self._migration_sources()) or bool(self.sudo().migrated_from)

    # ------------------------------------------------------------------
    # Source registry. Adapters extend these two.
    # ------------------------------------------------------------------

    @api.model
    def _migration_sources(self):
        """The codes of the installed migration sources. Each adapter returns
        ``super()._migration_sources() + [its code]`` while its connector is
        installed: a new list, never the one ``super()`` returned, mutated."""
        return []

    @api.model
    def _migration_source_label(self, code):
        """The name the Administrator sees for ``code``; the code itself when
        no adapter names it (e.g. an adapter uninstalled since)."""
        return code

    @api.model
    def _migration_source_method(self, code, method):
        """The bound ``_migration_<code>_<method>``, or None for a missing
        optional method. Refuses a code that is not installed and a source
        that lacks a required method."""
        if code not in self._migration_sources():
            raise UserError(_("The migration source %s is not installed.", code))
        handler = getattr(self, f"_migration_{code}_{method}", None)
        if handler is None and method in REQUIRED_METHODS:
            raise UserError(
                _(
                    "The migration source %(code)s is incomplete: it has no %(method)s. "
                    "Update its add-on.",
                    code=code,
                    method=method,
                )
            )
        return handler

    @api.model
    def _migration_source_detect(self, code, company):
        """``_migration_<code>_detect(company)``, run as superuser after the
        access checks, normalised: a dict with exactly `CREDENTIAL_KEYS`, or
        None when the source has nothing for ``company``. A source that
        returns any other key is refused (it could be a password)."""
        detected = self.sudo()._migration_source_method(code, "detect")(company)
        if not detected:
            return None
        extra = set(detected) - set(CREDENTIAL_KEYS)
        if extra:
            _logger.error(
                "APIX migration source %s returned keys it must not: %s", code, sorted(extra)
            )
            raise UserError(
                _(
                    "The migration source %s returned more than the transfer credentials. "
                    "Nothing was carried over; update its add-on.",
                    code,
                )
            )
        environment = detected.get("environment")
        return {
            "environment": environment if environment in ("test", "production") else "test",
            "transfer_id": detected.get("transfer_id") or False,
            "transfer_key": detected.get("transfer_key") or False,
            "business_id": detected.get("business_id") or company.company_registry or False,
        }

    @api.model
    def _migration_detect_all(self, company):
        """``(found, problems)``: ``{code: credentials}`` for every installed
        source with a transfer ID for ``company``, and ``{code: message}``
        for every source whose detection refused (e.g. a changed connector)."""
        found, problems = {}, {}
        for code in self._migration_sources():
            try:
                detected = self._migration_source_detect(code, company)
            except UserError as error:
                problems[code] = str(error)
                continue
            if detected and detected["transfer_id"]:
                found[code] = detected
        return found, problems

    @api.model
    def _migration_source_status(self, code):
        """``(SOURCE_OK, False)`` or ``(SOURCE_MISMATCH, message)``: whether
        the source's connector is the version its adapter was written for."""
        status = self.sudo()._migration_source_method(code, "status")()
        state, message = status if isinstance(status, (tuple, list)) else (status, False)
        if state == SOURCE_OK:
            return SOURCE_OK, False
        return SOURCE_MISMATCH, message or _(
            "The installed %s is not the version this migration tool supports.",
            self._migration_source_label(code),
        )

    @api.model
    def _migration_problem(self):
        """The first plain-language reason a switch cannot pause or resume
        every installed source, or False."""
        for code in self._migration_sources():
            state, message = self._migration_source_status(code)
            if state != SOURCE_OK:
                return message
        return False

    @api.model
    def _migration_pause_sources(self, company):
        """Every installed source stops receiving for ``company``; one that
        does not serve the company does nothing."""
        for code in self._migration_sources():
            self.sudo()._migration_source_method(code, "pause")(company)
            _logger.info("APIX migration: source %s paused for company %s", code, company.id)

    @api.model
    def _migration_resume_sources(self, company):
        """Every installed source receives for ``company`` again, as before
        the switch to live (the mirror of `_migration_pause_sources`)."""
        for code in self._migration_sources():
            self.sudo()._migration_source_method(code, "resume")(company)
            _logger.info("APIX migration: source %s resumed for company %s", code, company.id)

    @api.model
    def _migration_find_bill(self, company, storage_id):
        """The bill an earlier connector recorded for the package
        ``storage_id`` of ``company``: asks the source the company was
        migrated from, or every installed source when that is unknown, and
        returns the first answer (an ``account.move`` recordset, maybe
        empty). Sources without ``find_bill`` are skipped."""
        Move = self.env["account.move"]
        sources = self._migration_sources()
        if not storage_id or not sources:
            return Move
        connection = self.sudo().search([("company_id", "=", company.id)], limit=1)
        codes = [connection.migrated_from] if connection.migrated_from in sources else sources
        for code in codes:
            find_bill = self.sudo()._migration_source_method(code, "find_bill")
            if find_bill is None:
                continue
            move = find_bill(company, storage_id)
            if move:
                return move
        return Move

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    @api.model
    def _migration_check_admin(self):
        if not self.env.su and not self.env.user.has_group(ADMIN_GROUP):
            raise UserError(_("Only accounting administrators can use the migration tool."))

    @api.model
    def _migration_check_company(self, company):
        """The migration tool acts on the user's own companies only: an
        AccessError for any other, before anything runs as superuser."""
        if self.env.su or self.env.user._is_superuser():
            return
        if company not in self.env.user.company_ids:
            raise AccessError(
                _("You are not allowed to use the APIX migration tool for this company.")
            )

    def _migration_check_access(self):
        """Administrator of the connection's company (`_migration_*` on a
        connection). Read as the user: a connection of a company the user
        may not see raises AccessError already."""
        self.ensure_one()
        self._migration_check_admin()
        self._migration_check_company(self.company_id)

    # ------------------------------------------------------------------
    # Steps 1 and 2: detect, carry over
    # ------------------------------------------------------------------

    @api.model
    def _migration_carry_over(self, company, code=None):
        """Copy the transfer credentials the source ``code`` has for
        ``company`` into its connection (created when missing), mark it
        migrated from ``code``, start the shadow run and check the
        connection. Without ``code``: the one source that has credentials
        for the company; refused when several have. Returns the
        connection."""
        self._migration_check_admin()
        self._migration_check_company(company)
        if code:
            detected = self._migration_source_detect(code, company)
        else:
            code, detected = self._migration_single_source(company)
        if not detected or not detected["transfer_id"] or not detected["transfer_key"]:
            raise UserError(
                _(
                    "No earlier APIX connection with transfer credentials was found for %s. "
                    "Connect this add-on to APIX by hand instead.",
                    company.name,
                )
            )
        if not detected["business_id"]:
            raise UserError(_("The company has no Business ID: enter it on the company first."))
        Connection = self.sudo()
        connection = Connection.search([("company_id", "=", company.id)], limit=1)
        if connection.receiving_state == "live":
            raise UserError(_("Receiving is live already: switch back first."))
        vals = dict(detected, migrated_from=code, receiving_state="shadow")
        if connection:
            connection.write(vals)
        else:
            connection = Connection.create(dict(vals, company_id=company.id))
        connection.message_post(
            body=_(
                "Transfer credentials carried over from %s; shadow run started.",
                self._migration_source_label(code),
            )
        )
        connection._check_connection()
        _logger.info(
            "APIX migration: credentials carried over from %s for company %s", code, company.id
        )
        return connection

    @api.model
    def _migration_single_source(self, company):
        """``(code, credentials)`` of the only source with credentials for
        ``company``, ``(None, None)`` when none has; refused when several
        have (the Administrator chooses) or a detection failed."""
        found, problems = self._migration_detect_all(company)
        if len(found) > 1:
            raise UserError(
                _(
                    "Several APIX connectors have transfer credentials for %s: choose the one "
                    "to migrate from.",
                    company.name,
                )
            )
        if not found and problems:
            raise UserError("\n".join(problems.values()))
        return next(iter(found.items()), (None, None))

    # ------------------------------------------------------------------
    # Step 3: shadow
    # ------------------------------------------------------------------

    def _migration_records(self, domain=None):
        self.ensure_one()
        self._migration_check_access()
        return (
            self.env["goodahead.invoice.import"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("source", "=", "apix"),
                    ("apix_storage_id", "!=", False),
                ]
                + list(domain or [])
            )
        )

    def _migration_shadow_counts(self):
        """``{outcome: count}`` for every shadow outcome, plus
        ``unaccounted``: listed packages not classified (yet)."""
        self.ensure_one()
        self._migration_check_access()
        Import = self.env["goodahead.invoice.import"]
        counts = {key: 0 for key, _label in Import._fields["shadow_outcome"].selection}
        for record in self._migration_records([("shadow_outcome", "!=", False)]):
            counts[record.shadow_outcome] += 1
        counts["unaccounted"] = len(
            self._migration_records(
                [
                    ("shadow_outcome", "=", False),
                    ("move_id", "=", False),
                    ("state", "not in", list(lifecycle.SETTLED_STATES)),
                    (
                        "apix_status",
                        "in",
                        [apix_listing.STORAGE_UNRECEIVED, apix_listing.STORAGE_RECEIVED],
                    ),
                ]
            )
        )
        return counts

    # ------------------------------------------------------------------
    # Step 4: switch to live
    # ------------------------------------------------------------------

    def _migration_switch_to_live(self):
        """Pause every migration source for this company, clear the shadow
        results, go live and enqueue a fresh run. Nothing is imported or
        recognised here: the statuses the shadow run saw may be stale (the
        earlier connector received until the pause). The run lists the
        packages again, hands the ones APIX now shows as received to the
        recognition (bill found -> recognised, else possibly lost) and only
        then enqueues the imports of the waiting ones."""
        self.ensure_one()
        self._migration_check_access()
        connection = self.sudo()
        if connection.receiving_state == "live":
            raise UserError(_("Receiving is live already."))
        if not self.env["goodahead.apix.connection"]._get_working_connection(connection.company_id):
            raise UserError(
                _("Check the APIX connection first: switching to live needs a working connection.")
            )
        problem = self._migration_problem()
        if problem:
            raise UserError(problem)

        self._migration_pause_sources(connection.company_id)
        self._migration_records([("shadow_outcome", "!=", False)]).write(dict(SHADOW_CLEAR))
        connection._apix_set_receiving_state("live")
        connection._apix_enqueue_run()
        connection.message_post(
            body=Markup("<p>%s</p>")
            % _(
                "Receiving switched to live. A run lists the packages again: the ones APIX "
                "shows as received are checked against existing bills, the others are "
                "imported."
            )
        )
        _logger.info("APIX migration: company %s switched to live", connection.company_id.id)
        return True

    # ------------------------------------------------------------------
    # Switch back
    # ------------------------------------------------------------------

    def _migration_unconfirmed(self):
        self.ensure_one()
        self._migration_check_access()
        return self.env["goodahead.invoice.import"]._apix_unconfirmed_marks(self.company_id)

    def _migration_unsettled(self):
        """Packages nothing has been decided for yet: the earlier connector
        imports the ones APIX still shows as not received when it resumes."""
        self.ensure_one()
        return self._migration_records(
            [("move_id", "=", False), ("state", "in", list(UNSETTLED_STATES))]
        )

    def _migration_check_switch_back(self):
        """Refuse while a bill or close of the receiving add-on is not yet
        confirmed received at APIX (the earlier connector would import it
        again) or while a package or recognition job of the company runs.
        The gate takes the connection row's update lock first, so a package
        job that holds its share lock is waited for by nobody: the switch is
        refused."""
        self.ensure_one()
        self._migration_check_access()
        connection = self.sudo()
        if connection.receiving_state != "live":
            raise UserError(_("Receiving is not live: there is nothing to switch back."))
        connection._apix_check_can_leave_live()
        problem = self._migration_problem()
        if problem:
            raise UserError(problem)
        return self._migration_unsettled()

    def _migration_switch_back(self):
        """Receiving off for the receiving add-on, migration sources resumed.
        The Administrator has been shown the unsettled packages first."""
        self.ensure_one()
        self._migration_check_switch_back()
        connection = self.sudo()
        unsettled = self._migration_unsettled()
        self._migration_cancel_package_jobs()
        connection._apix_set_receiving_state("off")
        self._migration_resume_sources(connection.company_id)
        connection.message_post(
            body=_(
                "Receiving switched back to the earlier APIX connector; %s unsettled packages "
                "left to it.",
                len(unsettled),
            )
        )
        _logger.info("APIX migration: company %s switched back", connection.company_id.id)
        return True

    def _migration_cancel_package_jobs(self):
        """Package imports still queued for this company are cancelled: the
        earlier connector receives from now on (the job would skip anyway)."""
        self.ensure_one()
        records = self._migration_records()
        if not records:
            return self.env["queue.job"]
        jobs = (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "in", [package_identity_key(rid) for rid in records.ids]),
                    ("state", "in", ["pending", "enqueued", "failed"]),
                ]
            )
        )
        jobs.button_cancelled()
        return jobs
