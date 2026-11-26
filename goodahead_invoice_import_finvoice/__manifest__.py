# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Invoice Import: Finvoice",
    "summary": "Import Finvoice e-invoices uploaded or received by email as "
    "draft vendor bills, without any AI reading",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "Finvoice 2.0, 2.01 and 3.0 files by upload or email become "
    "draft vendor bills through the invoice import core, with strict vendor "
    "matching and no AI. See README.md.",
    "version": "18.0.1.6.2",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    "depends": [
        "goodahead_einvoice_base",
        "goodahead_invoice_import",
    ],
    "external_dependencies": {
        "python": ["lxml"],
    },
    "data": [],
    "installable": True,
    "auto_install": False,
    "application": False,
}
