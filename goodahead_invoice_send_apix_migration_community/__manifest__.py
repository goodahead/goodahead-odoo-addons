# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Invoice Sending through APIX — migration from the community modules",
    "summary": "Sending source for the community APIX modules: report whether "
    "they still send, hand their customers' e-invoice details, transmit methods "
    "and sent invoices to the migration tool, and stop them per company",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "The community APIX modules (connector_apix) as a sending "
    "source of the APIX migration tool. Installs itself where the APIX sending "
    "add-on and the community connector are both installed. See README.md.",
    "version": "18.0.1.1.4",
    # Our own code, LGPL-3 like every Goodahead e-invoicing add-on. It still
    # depends on the community modules, which keep their own AGPL-3 licence.
    # The platform add-ons never depend on this one.
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    # The sending add-on and the connector are listed although the bridge and
    # the community receiving source pull them in: Odoo accepts only direct
    # dependencies as auto_install triggers. The community receiving source
    # registers the `community` code this adapter answers for.
    "depends": [
        "goodahead_invoice_send_apix",
        "goodahead_invoice_send_apix_migration",
        "goodahead_invoice_import_apix_migration_community",
        "connector_apix",
        "account_edi_finvoice",
        "account_invoice_transmit_method",
    ],
    "data": [],
    "auto_install": ["goodahead_invoice_send_apix", "connector_apix"],
    "installable": True,
    "application": False,
}
