# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import api, models

#: Only Administrators are told about the connection itself:
#: Accountants see their failed sends, not the connection trouble as such.
CONNECTION_TROUBLE_GROUPS = "account.group_account_manager"


class EinvoiceAttention(models.AbstractModel):
    _inherit = "goodahead.einvoice.attention"

    @api.model
    def _attention_sections(self):
        """The connection's section of the shared bubble: APIX connections of
        the user's allowed companies that are not working."""
        section = self._attention_section(
            "connection_trouble",
            CONNECTION_TROUBLE_GROUPS,
            self.env["goodahead.apix.connection"]._attention_connection_trouble_section,
        )
        return super()._attention_sections() + [section]
