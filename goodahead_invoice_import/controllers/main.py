# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import http
from odoo.http import request


class FailedImportsController(http.Controller):

    @http.route(
        "/goodahead_invoice_import/failed_imports",
        type="json",
        auth="user",
        readonly=True,
    )
    def get_failed_imports(self):
        """Count + latest import records that need an Accountant — failed,
        duplicate and possibly lost — of the user's active companies.
        The route keeps its name from the AI import.

        Kept unchanged for one release: the systray bubble moved to
        `goodahead_einvoice_base` and reads `/goodahead_einvoice/attention`,
        where this add-on contributes the same data as its `failed_imports`
        section. Visible to the people who work the imports — see
        `goodahead.invoice.import._ai_user_works_imports`.
        """
        return request.env["goodahead.invoice.import"]._systray_attention_data()
