# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Invoice Import — APIX migration tool",
    "summary": "Move a company from an earlier APIX connector to APIX receiving: "
    "carry over, shadow run, switch to live and back",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "The migration tool of the APIX receiving add-on, with a "
    "pluggable migration-source interface: each earlier APIX connector is an "
    "adapter add-on. See README.md.",
    "version": "19.0.1.1.2",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    # Knows no earlier connector itself: the adapters register their source
    # (`_migration_sources`) and auto-install next to their connector, which
    # pulls this add-on in.
    "depends": [
        "goodahead_invoice_import_apix",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/ir_rule.xml",
        "wizard/apix_migration_views.xml",
        "views/apix_connection_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
}
