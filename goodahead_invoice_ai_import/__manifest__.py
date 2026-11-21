# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "AI Invoice Import",
    "summary": "Import vendor bills from PDF using Claude AI and Peppol BIS 3.0",
    # Set so Odoo does not render README.md as reStructuredText on the Apps page.
    "description": (
        "Turns vendor bill PDFs, uploaded or received by email, into draft vendor "
        "bills. Claude reads the PDF; the result is checked and imported through "
        "Odoo's own UBL importer. Features and setup: see README.md in the module."
    ),
    "version": "18.0.3.34.0",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    "depends": [
        "account",
        "account_edi_ubl_cii",
        "mail",
        "queue_job",
        "goodahead_claude",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/queue_job_channel.xml",
        "data/expense_category.xml",
        "data/prompt_profile.xml",
        "views/expense_category.xml",
        "views/prompt_profile.xml",
        "views/partner_identifier.xml",
        "views/ai_invoice_import.xml",
        "views/vendor_data_suggestion.xml",
        "wizard/vendor_data_review_views.xml",
        "views/account_move.xml",
        "views/res_config_settings.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "goodahead_invoice_ai_import/static/src/**/*.js",
            "goodahead_invoice_ai_import/static/src/**/*.xml",
            "goodahead_invoice_ai_import/static/src/**/*.scss",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
    "auto_install": False,
    "application": False,
}
