# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class GoodaheadInvoiceImportClose(models.TransientModel):
    """The "Close without bill" dialog: one required reason."""

    _name = "goodahead.invoice.import.close"
    _description = "Close Invoice Import Without Bill"

    import_ids = fields.Many2many(
        "goodahead.invoice.import",
        string="Imports",
        required=True,
    )
    reason = fields.Text(required=True)
    is_apix = fields.Boolean(compute="_compute_is_apix")

    @api.depends("import_ids")
    def _compute_is_apix(self):
        # The APIX note of SCR-08 ("APIX will be told it was received") shows
        # once a source called `apix` exists; the core has none.
        for wizard in self:
            wizard.is_apix = any(
                record.source == "apix" for record in wizard.import_ids
            )

    def action_confirm(self):
        self.ensure_one()
        if not (self.reason or "").strip():
            raise UserError(_("Say why the invoice is closed without a bill."))
        self.import_ids.action_close_without_bill(self.reason)
        return {"type": "ir.actions.act_window_close"}
