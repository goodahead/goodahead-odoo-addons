# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The community APIX modules as the migration source ``community``.


* detect: reads exactly four fields of the company's community backend;
* status: the community methods this adapter overrides or calls still have
  the names and parameters it relies on, and the four fields exist;
* pause: the company's queued community listings and downloads are
  cancelled; its listing, download and re-fetch are skipped or refused by the
  `apix.backend` overrides while it is live in the receiving add-on;
* resume: one community fetch is queued for the company.

No ``find_bill``: the community modules keep no record of which package
became which bill; the generic attachment and duplicate-rule
matching recognises their bills.
"""

import inspect
import logging

from odoo import _, api, models
from odoo.exceptions import UserError

from odoo.addons.goodahead_invoice_import_apix_migration.models.apix_connection import (
    SOURCE_MISMATCH,
    SOURCE_OK,
)

_logger = logging.getLogger(__name__)

#: This source's code in the migration registry.
COMMUNITY = "community"

#: The community connector's backend model and module.
COMMUNITY_MODEL = "apix.backend"
COMMUNITY_MODULE = "connector_apix"

#: The backend fields detect reads. 🔴 Never `password` nor `username`.
COMMUNITY_CREDENTIAL_FIELDS = ("environment", "transfer_id", "transfer_key", "business_id")

#: The community methods the adapter overrides or calls, with the parameters
#: (after ``self``) it relies on.
COMMUNITY_METHODS = {
    "action_cron_einvoice_fetch": (),
    "action_einvoice_fetch": (),
    "action_einvoice_refetch": (),
    "list_invoices": ("refetch",),
    "download_invoice": ("storage_id", "storage_key"),
}

#: The community jobs that fetch for a backend: cancelled at the pause.
COMMUNITY_JOB_METHODS = ("list_invoices", "download_invoice")

#: Job states a cancel applies to (queue_job's own "Cancel" button). A job
#: already started finishes; its download skips a live company.
CANCELLABLE_JOB_STATES = ("pending", "enqueued", "failed")


def community_mismatches(backend_class):
    """What ``backend_class`` — the community connector's own class of the
    backend — lacks of what the adapter relies on. Empty when it matches."""
    problems = []
    for name, params in COMMUNITY_METHODS.items():
        method = getattr(backend_class, name, None)
        if not callable(method):
            problems.append(f"{name}: missing")
            continue
        found = list(inspect.signature(method).parameters)[1:]
        if found[: len(params)] != list(params):
            problems.append(f"{name}{tuple(found)}: expected {params}")
    return problems


class GoodaheadApixConnection(models.Model):
    _inherit = "goodahead.apix.connection"

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    @api.model
    def _migration_sources(self):
        sources = super()._migration_sources()
        if COMMUNITY_MODEL not in self.env:
            return sources
        return sources + [COMMUNITY]

    @api.model
    def _migration_source_label(self, code):
        if code == COMMUNITY:
            return _("Community APIX modules")
        return super()._migration_source_label(code)

    # ------------------------------------------------------------------
    # Source methods (required: detect, status, pause, resume)
    # ------------------------------------------------------------------

    @api.model
    def _migration_community_detect(self, company):
        """The community backend's credentials for ``company`` (environment,
        transfer_id, transfer_key, business_id), or None without a backend.
        🔴 Reads exactly these four fields: never the web password nor the
        user name it belongs to. Raises `UserError` when the backend model
        does not have them."""
        Backend = self.env[COMMUNITY_MODEL].sudo().with_context(active_test=False)
        missing = [
            name
            for name in ("company_id",) + COMMUNITY_CREDENTIAL_FIELDS
            if name not in Backend._fields
        ]
        if missing:
            raise UserError(
                _("The installed community APIX modules are not the version this tool supports.")
            )
        backend = Backend.search([("company_id", "=", company.id)], limit=1)
        if not backend:
            return None
        values = backend.read(list(COMMUNITY_CREDENTIAL_FIELDS))[0]
        return {name: values.get(name) or False for name in COMMUNITY_CREDENTIAL_FIELDS}

    @api.model
    def _migration_community_status(self):
        backend_class = self._community_backend_class()
        if backend_class:
            problems = community_mismatches(backend_class)
        else:
            problems = ["apix.backend: not found"]
        fields = self.env[COMMUNITY_MODEL]._fields
        problems += [
            f"field {name}: missing"
            for name in ("company_id",) + COMMUNITY_CREDENTIAL_FIELDS
            if name not in fields
        ]
        if not problems:
            return SOURCE_OK, False
        _logger.warning("APIX community adapter mismatch: %s", "; ".join(problems))
        return SOURCE_MISMATCH, _(
            "The installed community APIX modules are not the version this tool supports."
        )

    @api.model
    def _migration_community_pause(self, company):
        """Before ``company`` goes live: its listing and re-fetch are paused
        by its live receiving state (`apix.backend` overrides); here its
        queued community listings and downloads are cancelled, so none of
        them races the first live run. Sending is untouched."""
        jobs = self._community_queued_jobs(company)
        jobs.button_cancelled()
        _logger.info(
            "APIX community paused for company %s: %s queued community jobs cancelled",
            company.id,
            len(jobs),
        )
        return True

    @api.model
    def _migration_community_resume(self, company):
        """After ``company`` switched back: its community backend fetches
        again with the credentials it kept; one listing is queued now."""
        backends = self._community_backends(company).filtered("transfer_id")
        backends.action_einvoice_fetch()
        _logger.info("APIX community resumed for company %s", company.id)
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @api.model
    def _community_backend_class(self):
        """The class the community connector defines `apix.backend` with,
        before any override (this adapter's included)."""
        for cls in type(self.env[COMMUNITY_MODEL]).__mro__:
            if getattr(cls, "_module", None) == COMMUNITY_MODULE and "list_invoices" in vars(cls):
                return cls
        return None

    @api.model
    def _community_backends(self, company):
        return self.env[COMMUNITY_MODEL].sudo().search([("company_id", "=", company.id)])

    @api.model
    def _community_queued_jobs(self, company):
        backends = self._community_backends(company)
        if not backends:
            return self.env["queue.job"]
        jobs = (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("model_name", "=", COMMUNITY_MODEL),
                    ("method_name", "in", list(COMMUNITY_JOB_METHODS)),
                    ("state", "in", list(CANCELLABLE_JOB_STATES)),
                ]
            )
        )
        return jobs.filtered(lambda job: set(job.records.ids) & set(backends.ids))
