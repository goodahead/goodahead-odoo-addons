# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The e-invoice image: the ERP's own invoice report (18.0 adapter).

Contract §1.6, ADR-0003: the image is rendered with the report the ERP
prints for the invoice, so where ``goodahead_finnish_invoice`` is installed it
is the Finnish invoice layout (with or without the bank bar code — a property
of that report, AC-44, AC-46). Version-specific: the report action and its
rendering call differ between ERP versions.
"""

import re

from odoo import models

INVOICE_REPORT = "account.account_invoices"


class AccountMove(models.Model):
    _inherit = "account.move"

    def _apix_render_pdf(self):
        """``(filename, bytes)`` of this invoice's printed report."""
        self.ensure_one()
        content, _report_type = self.env["ir.actions.report"]._render_qweb_pdf(
            INVOICE_REPORT, self.ids
        )
        name = re.sub(r"[^\w.-]+", "_", self._get_report_base_filename()).strip("_")
        return f"{name or 'invoice'}.pdf", content
