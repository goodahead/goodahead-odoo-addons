# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the shipped UN/ECE Rec 20 unit table.

The pure part runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_einvoice_base/tests -t goodahead

The standard-table agreement test needs the ERP (it reads the 19.0 table of
``account_edi_ubl_cii`` and resolves external ids in the database).
"""

import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import TransactionCase as _ErpCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase
    _ErpCase = None

    def tagged(*_tags):
        return lambda cls: cls

from ..lib.finvoice import units

# Vendored subset of UN/ECE Recommendation 20 (rev. 17) used by the shipped
# table: code -> common name. Kept here so the check runs offline.
REC20_SUBSET = {
    "C62": "one",
    "H87": "piece",
    "DZN": "dozen",
    "HUR": "hour",
    "DAY": "day",
    "MON": "month",
    "WEE": "week",
    "ANN": "year",
    "MIN": "minute [unit of time]",
    "KGM": "kilogram",
    "GRM": "gram",
    "TNE": "tonne (metric ton)",
    "MTR": "metre",
    "KMT": "kilometre",
    "CMT": "centimetre",
    "MMT": "millimetre",
    "MTK": "square metre",
    "MTQ": "cubic metre",
    "LTR": "litre",
    "KWH": "kilowatt hour",
    "MWH": "megawatt hour (1000 kW.h)",
}

# data-model.md §Unit codes, shipped table: code -> readable Finnish name.
EXPECTED_NAMES = {
    "C62": "kpl",
    "DZN": "tusina",
    "HUR": "h",
    "DAY": "pv",
    "MON": "kk",
    "WEE": "vk",
    "ANN": "v",
    "MIN": "min",
    "KGM": "kg",
    "GRM": "g",
    "TNE": "t",
    "MTR": "m",
    "KMT": "km",
    "CMT": "cm",
    "MMT": "mm",
    "MTK": "m2",
    "MTQ": "m3",
    "LTR": "l",
    "KWH": "kWh",
    "MWH": "MWh",
}


@tagged("post_install", "-at_install")
class TestUnitTable(_TestCase):
    def test_table_has_the_data_model_rows(self):
        self.assertEqual({row.code: row.name for row in units.TABLE}, EXPECTED_NAMES)
        self.assertEqual(len(units.TABLE), len(EXPECTED_NAMES))

    def test_every_code_is_valid_rec20(self):
        for row in units.TABLE:
            with self.subTest(code=row.code):
                self.assertTrue(units.is_valid_rec20(row.code))
                self.assertIn(row.code, REC20_SUBSET)

    def test_every_row_names_a_unit(self):
        for row in units.TABLE:
            with self.subTest(code=row.code):
                self.assertTrue(row.xmlid or row.unit_names, row)
                if row.xmlid:
                    self.assertTrue(row.xmlid.startswith("uom.product_uom_"), row)

    def test_aliases_are_lower_case_and_unique(self):
        seen = {}
        for row in units.TABLE:
            for alias in row.aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(alias, alias.lower())
                    self.assertNotIn(alias, seen, f"{alias} in {seen.get(alias)} and {row.code}")
                    seen[alias] = row.code


@tagged("post_install", "-at_install")
class TestReadableName(_TestCase):
    def test_known_codes(self):
        self.assertEqual(units.readable_name("C62"), "kpl")
        self.assertEqual(units.readable_name("HUR"), "h")
        self.assertEqual(units.readable_name("MTK"), "m2")
        self.assertEqual(units.readable_name("KWH"), "kWh")

    def test_unknown_code_has_no_name(self):
        # AC-49: a unit without a known code goes out with the unit's own name.
        self.assertIsNone(units.readable_name("XYZ"))
        self.assertIsNone(units.readable_name(None))
        self.assertIsNone(units.readable_name(""))


@tagged("post_install", "-at_install")
class TestIsValidRec20(_TestCase):
    def test_shapes(self):
        for code in ("C62", "KG", "H87", "MTK", "4G"):
            with self.subTest(code=code):
                self.assertTrue(units.is_valid_rec20(code))
        for code in ("c62", "XXXX", "K", "", None, "C 6", "kpl", " C62"):
            with self.subTest(code=code):
                self.assertFalse(units.is_valid_rec20(code))


@tagged("post_install", "-at_install")
class TestNormaliseIncoming(_TestCase):
    def test_valid_un_code_wins(self):
        self.assertEqual(units.normalise_incoming("HUR", "kpl"), "HUR")
        self.assertEqual(units.normalise_incoming(" KGM ", None), "KGM")

    def test_aliases_of_the_free_code(self):
        cases = {
            "kpl": "C62", "KPL": "C62", "pcs": "C62", "kappale": "C62",
            "h": "HUR", "tuntia": "HUR", "pv": "DAY", "päivä": "DAY",
            "kk": "MON", "vk": "WEE", "v": "ANN", "vuosi": "ANN",
            "min": "MIN", "kg": "KGM", "g": "GRM", "tn": "TNE",
            "m": "MTR", "km": "KMT", "m²": "MTK", "m3": "MTQ",
            "litra": "LTR", "kwh": "KWH", "MWh": "MWH",
        }
        for free, code in cases.items():
            with self.subTest(free=free):
                self.assertEqual(units.normalise_incoming(None, free), code)

    def test_h87_reads_as_pieces(self):
        self.assertEqual(units.normalise_incoming(None, "H87"), "C62")
        self.assertEqual(units.normalise_incoming("", "h87"), "C62")
        self.assertEqual(units.normalise_incoming("H87", None), "C62")

    def test_invalid_un_code_falls_back_to_aliases(self):
        self.assertEqual(units.normalise_incoming("c62", "kpl"), "C62")
        self.assertEqual(units.normalise_incoming("XXXX", "kg"), "KGM")

    def test_free_code_that_is_a_valid_code_is_kept(self):
        self.assertEqual(units.normalise_incoming(None, "LBR"), "LBR")
        self.assertEqual(units.normalise_incoming("bad", " FOT "), "FOT")

    def test_unknown_is_none(self):
        # AC-51: an unmapped free code never becomes a wrong unit.
        self.assertIsNone(units.normalise_incoming(None, "laatikko"))
        self.assertIsNone(units.normalise_incoming("xx", "kpl/kk"))
        self.assertIsNone(units.normalise_incoming(None, None))
        self.assertIsNone(units.normalise_incoming("", "  "))


if _ErpCase is not None:

    @tagged("post_install", "-at_install")
    class TestStandardTableAgreement(_ErpCase):
        """ADR-0013: the shipped table agrees with the 19.0 standard table."""

        def test_shared_external_ids_agree(self):
            try:
                from odoo.addons.account_edi_ubl_cii.models.account_edi_common import (
                    UOM_TO_UNECE_CODE,
                )
            except ImportError:
                self.skipTest("account_edi_ubl_cii is not available")
            ours = {row.xmlid: row.code for row in units.TABLE if row.xmlid}
            compared = 0
            for xmlid, standard_code in UOM_TO_UNECE_CODE.items():
                if xmlid not in ours:
                    continue
                if not self.env.ref(xmlid, raise_if_not_found=False):
                    continue  # missing on this version: skipped, not an error
                compared += 1
                self.assertEqual(
                    ours[xmlid],
                    standard_code,
                    f"unit {xmlid}: shipped {ours[xmlid]}, standard {standard_code}",
                )
            # Odoo 18.0 ships 13 of these standard units (19.0: 15 or more;
            # no minute or kWh unit, the square metre under another id).
            self.assertGreaterEqual(compared, 13)

        def test_shipped_external_ids_exist_or_are_skipped(self):
            found = [row.xmlid for row in units.TABLE if row.xmlid and self.env.ref(row.xmlid, raise_if_not_found=False)]
            self.assertIn("uom.product_uom_unit", found)
            self.assertIn("uom.product_uom_hour", found)
