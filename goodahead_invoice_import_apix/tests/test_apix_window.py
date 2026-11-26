# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The APIX listing window arithmetic.

Runs two ways:
- without Odoo, as a script with the add-on directory on the path:
  `PYTHONPATH=goodahead_invoice_import_apix python
  goodahead_invoice_import_apix/tests/test_apix_window.py`;
- under Odoo's runner (BaseCase + tags).
"""

import unittest
from datetime import date

if __name__.startswith("odoo.addons."):
    from odoo.tests import BaseCase, tagged

    from odoo.addons.goodahead_invoice_import_apix.lib import apix_window
else:  # plain unittest, no Odoo
    from lib import apix_window

    BaseCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls


@tagged("post_install", "-at_install")
class TestApixWindow(BaseCase):
    def test_window_end_is_first_day_of_month_after_next(self):
        self.assertEqual(apix_window.window_end(date(2026, 9, 1)), date(2026, 11, 1))
        self.assertEqual(apix_window.window_end(date(2026, 9, 30)), date(2026, 11, 1))

    def test_window_end_year_rollover(self):
        self.assertEqual(apix_window.window_end(date(2026, 11, 30)), date(2027, 1, 1))
        self.assertEqual(apix_window.window_end(date(2026, 12, 5)), date(2027, 2, 1))

    def test_warning_boundaries(self):
        dated = date(2026, 9, 15)  # leaves the listing on 2026-11-01
        self.assertFalse(apix_window.is_ageing(dated, date(2026, 10, 24)))  # 8 days before
        self.assertTrue(apix_window.is_ageing(dated, date(2026, 10, 25)))  # 7 days before
        self.assertTrue(apix_window.is_ageing(dated, date(2026, 10, 26)))  # 6 days before
        self.assertTrue(apix_window.is_ageing(dated, date(2026, 11, 3)))  # already gone

    def test_warning_boundaries_over_year_end(self):
        dated = date(2026, 12, 31)  # leaves the listing on 2027-02-01
        self.assertFalse(apix_window.is_ageing(dated, date(2027, 1, 24)))
        self.assertTrue(apix_window.is_ageing(dated, date(2027, 1, 25)))
        self.assertEqual(apix_window.warn_from(dated), date(2027, 1, 25))

    def test_warning_over_short_february(self):
        dated = date(2027, 1, 31)  # leaves the listing on 2027-03-01
        self.assertEqual(apix_window.warn_from(dated), date(2027, 2, 22))
        self.assertFalse(apix_window.is_ageing(dated, date(2027, 2, 21)))
        self.assertTrue(apix_window.is_ageing(dated, date(2027, 2, 22)))

    def test_days_left_and_age(self):
        dated = date(2026, 9, 15)
        self.assertEqual(apix_window.days_left(dated, date(2026, 10, 25)), 7)
        self.assertEqual(apix_window.days_left(dated, date(2026, 11, 2)), -1)
        self.assertEqual(apix_window.age_days(dated, date(2026, 10, 25)), 40)

    def test_no_document_date_never_ageing(self):
        self.assertFalse(apix_window.is_ageing(None, date(2026, 10, 25)))


if __name__ == "__main__":
    unittest.main()
