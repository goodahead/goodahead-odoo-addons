# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Goodahead Invoice Sending through APIX",
    "summary": "Send customer invoices and credit notes as Finvoice e-invoices "
    "and printed letters through APIX",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "Sends customer invoices and credit notes through the "
    "company's APIX connection as Finvoice 3.0 e-invoices or printed letters. "
    "See README.md.",
    "version": "19.0.1.24.4",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    # Never an AGPL add-on (account_invoice_transmit_method, uom_unece,
    # connector_apix) and never account_edi_finvoice.
    "depends": [
        "account",
        "account_edi_ubl_cii",
        "queue_job",
        "l10n_fi_edicode",
        "goodahead_apix",
        "goodahead_einvoice_base",
    ],
    "external_dependencies": {
        "python": ["lxml"],
    },
    "data": [
        "security/ir.model.access.csv",
        "security/send_security.xml",
        "data/queue_job_data.xml",
        "data/ir_cron.xml",
        "views/apix_connection_views.xml",
        "wizard/send_fix_views.xml",
        "wizard/already_sent_views.xml",
        "wizard/regenerate_views.xml",
        "wizard/send_wizard_views.xml",
        "views/send_attempt_views.xml",
        "views/failed_sends_views.xml",
        "views/account_move_views.xml",
        "views/res_partner_views.xml",
        "views/account_tax_views.xml",
        "views/res_company_views.xml",
        "views/account_move_send_wizard_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "goodahead_invoice_send_apix/static/src/**/*.js",
            "goodahead_invoice_send_apix/static/src/**/*.xml",
        ],
        "web.assets_unit_tests": [
            "goodahead_invoice_send_apix/static/tests/**/*.test.js",
        ],
    },
    # Install: mark documents the ERP had sent and back-fill transmit methods
    # (hooks.py; AC-62, AC-61). Set-based SQL, nothing else produced.
    "post_init_hook": "post_init_hook",
    "installable": True,
    "auto_install": False,
    "application": False,
}
