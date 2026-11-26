# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import http
from odoo.http import request


class EinvoiceAttentionController(http.Controller):
    @http.route(
        "/goodahead_einvoice/attention",
        type="jsonrpc",
        auth="user",
        readonly=True,
    )
    def get_attention(self):
        """Feed the e-invoicing attention bubble: ``{total, sections}`` of the
        sections the user may see, empty ones dropped."""
        return request.env["goodahead.einvoice.attention"]._attention_data()
