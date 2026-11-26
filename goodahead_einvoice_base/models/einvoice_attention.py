# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The shared e-invoicing attention bubble.

One systray bubble for every e-invoicing add-on. Each add-on contributes one
section by extending ``_attention_sections`` and appending (never mutating
``super()``'s list)::

    @api.model
    def _attention_sections(self):
        return super()._attention_sections() + [
            self._attention_section("failed_sends", "account.group_account_invoice",
                                    self._my_failed_sends_section),
        ]

The builder returns ``{label, count, items, action, item_action}`` for the
current user's allowed companies, or ``None``. It runs only for users in one of
the section's groups; a builder that raises drops its own section (logged by
key) and the other sections are still returned.
"""

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

SECTION_KEYS = ("label", "count", "items", "action", "item_action")
MAX_ITEMS = 10


class EinvoiceAttention(models.AbstractModel):
    _name = "goodahead.einvoice.attention"
    _description = "E-invoicing attention bubble"

    @api.model
    def _attention_sections(self):
        """Super-chained registry: each add-on appends its section (or None)."""
        return []

    @api.model
    def _attention_user_in(self, groups):
        """Whether the current user is in at least one of ``groups``."""
        user = self.env.user
        return any(user.has_group(xmlid.strip()) for xmlid in groups.split(",") if xmlid.strip())

    @api.model
    def _attention_section(self, key, groups, builder):
        """Build one section for the current user, or None.

        None when the user is in none of ``groups``, when the builder answers
        nothing or when it raises (logged by key, its queries rolled back).
        """
        if not self._attention_user_in(groups):
            return None
        try:
            with self.env.cr.savepoint():
                built = builder()
        except Exception:
            _logger.exception("E-invoicing attention section %s failed; dropped", key)
            return None
        if not built:
            return None
        section = {name: built.get(name) for name in SECTION_KEYS}
        section["count"] = int(section["count"] or 0)
        section["items"] = list(section["items"] or [])[:MAX_ITEMS]
        section.update(key=key, groups=groups)
        return section

    @api.model
    def _attention_data(self):
        """``{total, sections}``: the user's visible, non-empty sections."""
        sections = [s for s in self._attention_sections() if s and s["count"] > 0]
        return {"total": sum(s["count"] for s in sections), "sections": sections}
