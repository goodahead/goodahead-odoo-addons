# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "E-invoicing Base",
    "summary": "Shared base of the Goodahead e-invoicing add-ons: the pure-Python "
    "Finvoice library, its schema, unit codes and the shared attention bubble",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "The Finvoice 3.0 schema, the pure-Python Finvoice library, "
    "the UN/ECE Rec 20 unit codes and the systray attention bubble shared by "
    "receiving and sending e-invoices. See README.md.",
    "version": "18.0.1.9.4",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    "depends": [
        "account",
    ],
    "external_dependencies": {
        "python": ["lxml"],
    },
    "data": [
        "views/uom_rec20_views.xml",
        "views/account_move_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "goodahead_einvoice_base/static/src/**/*.js",
            "goodahead_einvoice_base/static/src/**/*.xml",
        ],
        "web.assets_unit_tests": [
            "goodahead_einvoice_base/static/tests/**/*.test.js",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
    "auto_install": False,
    "application": False,
}
