# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Invoice Import — APIX receiving",
    "summary": "Fetch e-invoice packages from APIX for every company, "
    "each company in its own background job",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "Fetches Finvoice e-invoice packages from APIX per company, "
    "imports each as a draft vendor bill and marks it received only after the "
    "bill is saved. Moving a company over from an earlier APIX connector is "
    "the separate add-on goodahead_invoice_import_apix_migration. "
    "See README.md.",
    "version": "19.0.1.10.5",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    "depends": [
        "goodahead_invoice_import",
        "goodahead_apix",
        "goodahead_invoice_import_finvoice",
        "queue_job",
    ],
    # docs/reference/ holds the APIX and Finvoice source-of-truth documents;
    # it is documentation, not data.
    # The company rule on the import record lives in the core since this
    # add-on's 19.0.1.0.1; upgrading removes the old copy.
    # This add-on knows no other APIX receiver. The migration tool and its
    # source adapters (one add-on per earlier connector) live in
    # goodahead_invoice_import_apix_migration and answer this add-on's hooks
    # `_apix_receiving_via_migration` and `_apix_bill_from_earlier_receiver`.
    "data": [
        "data/queue_job_data.xml",
        "data/ir_cron.xml",
        "views/apix_connection_views.xml",
        "views/goodahead_invoice_import_views.xml",
    ],
    "installable": True,
    "application": False,
}
