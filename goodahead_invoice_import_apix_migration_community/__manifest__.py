# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Invoice Import — APIX migration from the community modules",
    "summary": "Migration source for the community APIX modules: detect their "
    "credentials, pause and resume them per company",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "The community APIX modules (connector_apix) as a migration "
    "source of the APIX migration tool. Installs itself where the APIX "
    "receiving add-on and the community connector are both installed. "
    "See README.md.",
    "version": "18.0.1.0.4",
    # Our own code, LGPL-3 like every Goodahead e-invoicing add-on. It still
    # depends on the community connector, which keeps its own AGPL-3 licence.
    # The platform add-ons never depend on this one.
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    # The receiving add-on is listed although the migration tool depends on
    # it: Odoo accepts only direct dependencies as auto_install triggers.
    "depends": [
        "goodahead_invoice_import_apix",
        "goodahead_invoice_import_apix_migration",
        "connector_apix",
    ],
    "data": [
        "views/apix_backend_views.xml",
    ],
    # Installed by itself wherever the receiving add-on and the community
    # connector are both installed, and pulls the migration tool in with it:
    # a database that still has the community modules then goes live only
    # through the tool, which pauses them. Uninstalled with the community
    # modules once a database no longer uses them.
    "auto_install": ["goodahead_invoice_import_apix", "connector_apix"],
    "installable": True,
    "application": False,
}
