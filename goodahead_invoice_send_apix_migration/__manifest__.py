# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Invoice Sending through APIX — migration bridge",
    "summary": "Carry customers' e-invoice details, transmit methods and sent "
    "invoices over from an earlier APIX sending setup, and keep two senders "
    "from running at once",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "The sending side of the APIX migration tool: sending "
    "sources, the earlier-senders guard of the sending switch, carry-over, "
    "stop and switch back. See README.md.",
    "version": "18.0.1.3.3",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    # Knows no earlier connector itself: sending adapters implement the
    # `_migration_<code>_sending_*` methods. Never an AGPL
    # add-on here; only an adapter may depend on its connector.
    "depends": [
        "goodahead_invoice_send_apix",
        "goodahead_invoice_import_apix_migration",
    ],
    "data": [
        "views/apix_migration_views.xml",
    ],
    # Wherever sending and the migration tool are both installed, the
    # earlier-senders guard must answer.
    "auto_install": True,
    "installable": True,
    "application": False,
}
