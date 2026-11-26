# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Invoice Import",
    "summary": "Shared import machinery for vendor bills: import records, "
    "vendor matching, vendor-data suggestions, bank checks and bill decoding",
    # Split out of `goodahead_invoice_ai_import` 19.0.3.33.0 (core/source split): every
    # source of vendor bills (the AI reading of a PDF, structured e-invoices)
    # shares what is here. Keep this manifest short — see the AI add-on's
    # manifest for why an over-long one silently stops being a manifest.
    # A short description stops Odoo from rendering README.md as RST.
    "description": "Shared core of the invoice import platform: import records "
    "and inbox, vendor matching that never creates vendors, vendor data "
    "suggestions, bank checks, the duplicate rule and bill decoding. "
    "See README.md.",
    "version": "19.0.1.12.4",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    "depends": [
        "account",
        "account_edi_ubl_cii",
        "goodahead_einvoice_base",
        "mail",
        "queue_job",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/security.xml",
        "data/expense_category.xml",
        "views/expense_category.xml",
        "views/partner_identifier.xml",
        "views/ai_invoice_import.xml",
        "views/vendor_data_suggestion.xml",
        "wizard/vendor_data_review_views.xml",
        "wizard/import_close_views.xml",
        "views/account_move.xml",
        "views/res_config_settings.xml",
    ],
    "assets": {
        # The systray bubble moved to `goodahead_einvoice_base` (send
        # ADR-0014); this add-on contributes its `failed_imports` section.
        "web.assets_backend": [
            "goodahead_invoice_import/static/src/**/*.scss",
        ],
    },
    # ADR-0002: on a database that already runs the AI import, upgrading that
    # add-on installs this one first and the pre-install hook renames
    # `ai.invoice.import` to `goodahead.invoice.import` in place. See hooks.py.
    "pre_init_hook": "pre_init_hook",
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
    "auto_install": False,
    "application": False,
}
