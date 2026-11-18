# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import http
from odoo.http import request


class FailedImportsController(http.Controller):

    @http.route(
        "/goodahead_invoice_ai_import/failed_imports",
        type="json",
        auth="user",
        readonly=True,
    )
    def get_failed_imports(self):
        """Return the count and latest failed AI invoice imports for the systray."""
        if not request.env["ai.invoice.import"]._ai_user_works_imports():
            return {"count": 0, "imports": []}

        AiImport = request.env["ai.invoice.import"]
        # Scope to the user's active companies so the bubble never leaks failed
        # imports from other companies in a multi-company database.
        domain = [
            ("state", "=", "failed"),
            ("company_id", "in", request.env.companies.ids),
        ]
        count = AiImport.search_count(domain)
        records = AiImport.search(domain, limit=10, order="write_date desc")

        imports = []
        for rec in records:
            error = rec.error_message or ""
            imports.append(
                {
                    "id": rec.id,
                    "name": rec.name or "",
                    "error": error if len(error) <= 120 else error[:117] + "...",
                }
            )

        return {"count": count, "imports": imports}
