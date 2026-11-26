# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Sending sources in the migration-source registry and the earlier-senders
guard of the sending switch.

A migration source (receiving registry, ``_migration_sources()``) is a
**sending source** when its adapter implements any of the
``_migration_<code>_sending_*`` methods. A sending source missing a required
one counts as sending ("the migration source is incomplete"), so the switch
stays refused until its add-on is updated. A receiving-only source is not a
sender.
"""

from odoo import _, api, models

#: Methods every sending source implements.
SENDING_METHODS = ("sending_status", "sending_read", "sending_stop", "sending_resume")
#: ``_migration_<code>_sending_status`` values.
SENDING = "sending"
STOPPED = "stopped"
NONE = "none"


class GoodaheadApixConnection(models.Model):
    _inherit = "goodahead.apix.connection"

    @api.model
    def _migration_sending_method(self, code, method):
        """The bound ``_migration_<code>_<method>`` (superuser), or None."""
        return getattr(self.sudo(), f"_migration_{code}_{method}", None)

    @api.model
    def _migration_sending_sources(self):
        """Installed sources that implement any sending method."""
        return [
            code for code in self._migration_sources()
            if any(self._migration_sending_method(code, m) for m in SENDING_METHODS)
        ]

    @api.model
    def _migration_sending_incomplete(self, code):
        return [m for m in SENDING_METHODS if not self._migration_sending_method(code, m)]

    @api.model
    def _migration_sending_status(self, code, company):
        """``(sending | stopped | none, reason)`` of ``code`` for
        ``company``; an incomplete source counts as sending."""
        if self._migration_sending_incomplete(code):
            return SENDING, _("the migration source is incomplete — update its add-on")
        status = self._migration_sending_method(code, "sending_status")(company)
        state, reason = status if isinstance(status, (tuple, list)) else (status, "")
        if state not in (SENDING, STOPPED, NONE):
            return SENDING, _("the migration source answered an unknown status")
        return state, reason or ""

    @api.model
    def _apix_earlier_senders(self, company):
        senders = list(super()._apix_earlier_senders(company))
        sending_sources = self._migration_sending_sources()
        for code in sending_sources:
            state, reason = self._migration_sending_status(code, company)
            if state != SENDING:
                continue
            senders.append({
                "code": code,
                "label": self._migration_source_label(code),
                "reason": reason,
            })
        return senders + self._migration_senders_without_adapter(company, sending_sources)

    @api.model
    def _migration_senders_without_adapter(self, company, sending_sources):
        """Connectors the receiving migration tool detects for ``company``
        (their transfer credentials are found) that have no sending adapter:
        nothing can tell or stop their sending, so on the safe side they
        count as still sending."""
        found, _problems = self._migration_detect_all(company)
        return [
            {
                "code": code,
                "label": self._migration_source_label(code),
                "reason": _("it has no sending adapter, so the migration tool cannot stop "
                            "its sending"),
                "no_adapter": True,
            }
            for code in found
            if code not in sending_sources
        ]
