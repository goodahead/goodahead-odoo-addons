# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Sending's section of the shared e-invoicing attention bubble (contract
§8.1, ADR-0014): failed sends — invoices whose latest attempt was refused or
failed and is not handled — counted per allowed company for Accountants.
Connection trouble itself is the APIX add-on's section,
shown to Administrators only.
"""

from odoo import api, models

FAILED_SENDS_GROUPS = "account.group_account_invoice"


class EinvoiceAttention(models.AbstractModel):
    _inherit = "goodahead.einvoice.attention"

    @api.model
    def _attention_sections(self):
        section = self._attention_section(
            "failed_sends",
            FAILED_SENDS_GROUPS,
            self.env["goodahead.invoice.send.attempt"]._attention_failed_sends_section,
        )
        return super()._attention_sections() + [section]
