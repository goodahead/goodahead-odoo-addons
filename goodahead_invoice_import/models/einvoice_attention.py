# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import api, models

#: Who is told about failed imports: Bookkeeper or accounting Administrator,
#: not plain Invoicing — see `goodahead.invoice.import._ai_user_works_imports`.
FAILED_IMPORTS_GROUPS = "account.group_account_user,account.group_account_manager"


class EinvoiceAttention(models.AbstractModel):
    _inherit = "goodahead.einvoice.attention"

    @api.model
    def _attention_sections(self):
        """Receiving's section of the shared bubble: failed, duplicate and
        possibly lost imports (receiving AC-09), as the bubble always counted."""
        section = self._attention_section(
            "failed_imports",
            FAILED_IMPORTS_GROUPS,
            self.env["goodahead.invoice.import"]._attention_failed_imports_section,
        )
        return super()._attention_sections() + [section]
