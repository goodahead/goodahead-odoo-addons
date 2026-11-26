# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Goodahead Finnish Invoice",
    "summary": "Customer invoice PDF laid out as a Finnish invoice, with the bank bar code",
    "description": "Customer invoice PDF for Finnish companies: invoice details, "
    "amounts with and without VAT, a VAT breakdown, payment details and the "
    "Finnish bank bar code. See README.md.",
    "version": "19.0.1.0.1",
    "category": "Accounting",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "license": "LGPL-3",
    "application": False,
    "installable": True,
    "depends": ["account"],
    "data": [
        "report/paperformat.xml",
        "report/report_layout.xml",
        "report/report_invoice.xml",
        "views/res_company.xml",
    ],
}
