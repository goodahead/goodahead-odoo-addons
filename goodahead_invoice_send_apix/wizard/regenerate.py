# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""SCR-13 — "Regenerate and check": the Finvoice message built from the
invoice's current data and its schema check, shown in a dialog with the
message as a download.

🔴 NOTHING IS SENT OR RECORDED. The dialog keeps the message in its own
transient row (no attachment); no attempt, job or APIX call is made.
"""

from odoo import fields, models


class GoodaheadInvoiceSendRegenerate(models.TransientModel):
    _name = "goodahead.invoice.send.regenerate"
    _description = "Regenerated Finvoice message (developer view)"

    move_id = fields.Many2one("account.move", string="Invoice", readonly=True)
    schema_result = fields.Selection(
        [("valid", "Valid"), ("invalid", "Invalid")], readonly=True
    )
    problems = fields.Text(readonly=True)
    units_without_code = fields.Char(string="Units without a code", readonly=True)
    # the download lives in this transient row, never in an attachment
    message_file = fields.Binary(string="Finvoice message", attachment=False, readonly=True)
    message_filename = fields.Char(readonly=True)
