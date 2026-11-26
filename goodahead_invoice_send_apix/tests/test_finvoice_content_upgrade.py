# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Nothing changes until someone sends (finvoice content T16; AC-24).

In the database the suite runs on: install wrote no VAT category, VAT reason
or buyer reference onto any record the tests did not create, and nothing in
the three add-ons' install hooks or migration scripts writes those fields.
The ≥ 1,000-invoice upgrade on a database copy (records compared before and
after ``-u``) is the release check of this feature's implementation log.
"""

import glob
import os
import re
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import TransactionCase, tagged
from odoo.tools import SQL

GOODAHEAD = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADDONS = ("goodahead_einvoice_base", "goodahead_invoice_send_apix", "goodahead_invoice_import")
FIELDS = (
    "einvoice_vat_reason", "ubl_cii_tax_category_code", "ubl_cii_tax_exemption_reason_code",
    "einvoice_buyer_reference", "einvoice_standing_buyer_reference",
    "einvoice_order_reference",
)


@tagged("post_install", "-at_install")
class TestFinvoiceContentUpgradeSafety(TransactionCase):
    def test_install_wrote_no_new_content(self):
        cr = self.env.cr
        checks = (
            ("account_tax", "einvoice_vat_reason"),
            ("account_move", "einvoice_buyer_reference"),
            ("res_partner", "einvoice_standing_buyer_reference"),
        )
        for table, column in checks:
            with self.subTest(table=table):
                cr.execute(SQL("SELECT count(*) FROM %s WHERE %s IS NOT NULL",
                               SQL.identifier(table), SQL.identifier(column)))
                self.assertEqual(cr.fetchone()[0], 0)

    def test_no_hook_or_migration_writes_the_new_fields(self):
        sources = []
        for addon in ADDONS:
            base = os.path.join(GOODAHEAD, addon)
            sources += glob.glob(os.path.join(base, "hooks.py"))
            sources += glob.glob(os.path.join(base, "migrations", "**", "*.py"), recursive=True)
            sources += glob.glob(os.path.join(base, "data", "*.xml"))
        self.assertTrue(sources)
        pattern = re.compile("|".join(FIELDS))
        for path in sources:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            with self.subTest(path=os.path.relpath(path, GOODAHEAD)):
                self.assertIsNone(pattern.search(text))

    def test_new_fields_have_no_column_default(self):
        cr = self.env.cr
        for table, column in (("account_tax", "einvoice_vat_reason"),
                              ("account_move", "einvoice_buyer_reference"),
                              ("res_partner", "einvoice_standing_buyer_reference")):
            with self.subTest(column=column):
                cr.execute(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s", [table, column])
                self.assertIsNone(cr.fetchone()[0])
