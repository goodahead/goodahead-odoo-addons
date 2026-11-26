# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the shared pure-Python Finvoice library.

Runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_einvoice_base/tests -t goodahead

and inside the ERP's test runner. Every fixture under ``data/`` is invented.
"""

import glob
import json
import os
import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib import finvoice

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib")


def _raw(name):
    with open(os.path.join(DATA_DIR, name), "rb") as handle:
        return handle.read()


def _fixtures():
    return sorted(glob.glob(os.path.join(DATA_DIR, "*.xml")))


@tagged("post_install", "-at_install")
class TestFinvoiceReaderInBase(_TestCase):
    def test_every_fixture_parses_and_maps(self):
        self.assertGreaterEqual(len(_fixtures()), 15)
        for path in _fixtures():
            with self.subTest(fixture=os.path.basename(path)):
                with open(path, "rb") as handle:
                    messages = finvoice.parse(handle.read())
                self.assertTrue(messages)
                data = finvoice.to_invoice_data(messages[0])
                self.assertTrue(data.get("invoice_number"))

    def test_schema_ships_with_the_base(self):
        self.assertTrue(os.path.isfile(finvoice.XSD_PATH))
        self.assertTrue(finvoice.XSD_PATH.startswith(LIB_DIR))


@tagged("post_install", "-at_install")
class TestValidateSchema(_TestCase):
    def test_every_3_0_fixture_is_schema_valid(self):
        checked = 0
        for path in _fixtures():
            with open(path, "rb") as handle:
                raw = handle.read()
            if b'Version="3.0"' not in raw:
                continue
            with self.subTest(fixture=os.path.basename(path)):
                self.assertEqual(finvoice.validate_schema(raw), [])
                self.assertEqual(finvoice.validate_schema_text(raw), [])
                checked += 1
        self.assertGreaterEqual(checked, 11)

    def test_problem_names_the_element_path(self):
        raw = _raw("fi30_simple_invoice.xml").replace(
            b"<BuyerOrganisationName>Example Buyer Oy</BuyerOrganisationName>",
            b"<BuyerOrganisationName>Example Buyer Oy</BuyerOrganisationName>"
            b"<BuyerUnknownElement>x</BuyerUnknownElement>",
        )
        problems = finvoice.validate_schema(raw)
        self.assertTrue(problems)
        problem = problems[0]
        self.assertEqual(set(problem), {"path", "line", "message"})
        self.assertTrue(problem["path"].startswith("/Finvoice/BuyerPartyDetails"), problem)
        self.assertIsInstance(problem["line"], int)
        self.assertIn("BuyerUnknownElement", problem["message"])

    def test_text_form_keeps_the_readers_shape(self):
        raw = _raw("fi30_simple_invoice.xml").replace(b"<InvoiceNumber>10045</InvoiceNumber>", b"")
        problems = finvoice.validate_schema_text(raw)
        self.assertTrue(problems)
        self.assertTrue(all(isinstance(p, str) for p in problems))
        self.assertTrue(all(p.startswith("line ") for p in problems))

    def test_other_versions_are_not_schema_checked(self):
        self.assertEqual(finvoice.validate_schema(_raw("fi201_invoice.xml")), [])


@tagged("post_install", "-at_install")
class TestLibraryIsPure(_TestCase):
    def test_no_erp_import_in_lib(self):
        for path in glob.glob(os.path.join(LIB_DIR, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotRegex(source, r"(?m)^\s*(from|import)\s+odoo\b", path)

    def test_license_header_everywhere(self):
        package = os.path.dirname(LIB_DIR)
        for path in glob.glob(os.path.join(package, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as handle:
                head = handle.read(200)
            self.assertTrue(head.startswith("# Copyright 2026 Goodahead Oy\n# License LGPL-3.0"), path)


@tagged("post_install", "-at_install")
class TestOldImportPathStillWorks(_TestCase):
    """The receiving add-on's import path re-exports the base (shim)."""

    def test_shim_reexports_the_same_objects(self):
        try:
            from odoo.addons.goodahead_invoice_import_finvoice.lib import (
                finvoice as old,
            )
        except ImportError:
            try:
                from goodahead_invoice_import_finvoice.lib import finvoice as old
            except ImportError:
                self.skipTest("receiving's Finvoice add-on is not on the path")
        self.assertIs(old.FinvoiceError, finvoice.FinvoiceError)
        self.assertIs(old.parse, finvoice.parse)
        self.assertIs(old.values, finvoice.values)
        self.assertEqual(old.XSD_PATH, finvoice.XSD_PATH)


# ---------------------------------------------------------------------------
# finvoice content T06: references and row discounts
# ---------------------------------------------------------------------------

SNAPSHOT = "reader_snapshot.json"
NEW_KEYS = ("order_reference", "buyer_reference")
DISCOUNT_KEYS = ("gross_price", "discount_percent", "discount_amount")


def _read_all(name):
    out = []
    for message in finvoice.parse(_raw(name)):
        try:
            out.append({"data": finvoice.to_invoice_data(message)})
        except finvoice.FinvoiceError as error:
            out.append({"error": error.code, "field": error.field, "data": error.data})
    return out


def _plain(value):
    """The snapshot's JSON form (dates and Decimals as strings)."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


@tagged("post_install", "-at_install")
class TestReaderReferencesAndDiscounts(_TestCase):
    def _data(self):
        return finvoice.to_invoice_data(finvoice.parse(_raw("fi30_references_discounts.xml"))[0])

    def test_fixture_is_schema_valid(self):
        self.assertEqual(finvoice.validate_schema(_raw("fi30_references_discounts.xml")), [])

    def test_references(self):
        data = self._data()
        self.assertEqual(data["order_reference"], "PO-ZZ-4711")
        self.assertEqual(data["buyer_reference"], "Test User / cost centre 42")
        self.assertEqual(data["invoice_number"], "ZZ-V-2001")

    def test_message_without_references(self):
        data = finvoice.to_invoice_data(finvoice.parse(_raw("fi30_no_vat.xml"))[0])
        self.assertIsNone(data["order_reference"])
        self.assertIsNone(data["buyer_reference"])

    def test_stated_discount(self):
        line = self._data()["lines"][0]
        self.assertEqual(line["gross_price"], 100.0)
        self.assertEqual(line["discount_percent"], 15.0)
        self.assertEqual(line["discount_amount"], 150.0)
        self.assertEqual(line["line_total"], 850.0)
        self.assertEqual(line["price_unit"], 100.0)

    def test_rounding_off_discount(self):
        line = self._data()["lines"][1]
        self.assertEqual((line["gross_price"], line["discount_percent"], line["discount_amount"]),
                         (9.99, 7.0, 2.11))
        self.assertEqual(line["line_total"], 27.86)

    def test_amount_only_discount(self):
        line = self._data()["lines"][2]
        self.assertEqual(line["gross_price"], 25.0)
        self.assertNotIn("discount_percent", line)
        self.assertEqual(line["discount_amount"], 10.0)

    def test_row_without_discount_has_no_new_keys(self):
        line = self._data()["lines"][3]
        for key in DISCOUNT_KEYS:
            self.assertNotIn(key, line)

    def test_credit_note_discount_follows_the_row_sign(self):
        values_path = os.path.join(DATA_DIR, "content_credit_note.json")
        with open(values_path, encoding="utf-8") as handle:
            values = json.load(handle)
        data = finvoice.to_invoice_data(finvoice.parse(finvoice.write(values))[0])
        self.assertEqual(data["type"], "CreditNote")
        line = data["lines"][0]
        self.assertEqual((line["quantity"], line["price_unit"], line["line_total"]),
                         (2.0, 50.0, 80.0))
        self.assertEqual((line["gross_price"], line["discount_percent"],
                          line["discount_amount"]), (50.0, 20.0, 20.0))

    def test_every_existing_fixture_gives_the_same_invoice_data(self):
        """Apart from the new keys, every existing fixture reads exactly as
        before; the new keys appear only where the message states them."""
        with open(os.path.join(DATA_DIR, SNAPSHOT), encoding="utf-8") as handle:
            snapshot = json.load(handle)
        self.assertGreaterEqual(len(snapshot), 15)
        stated = {}
        for name, expected in snapshot.items():
            with self.subTest(fixture=name):
                got = _read_all(name)
                for entry in got:
                    data = entry.get("data") or {}
                    for key in NEW_KEYS:
                        if data.pop(key, None) is not None:
                            stated.setdefault(name, set()).add(key)
                    for line in data.get("lines") or []:
                        for key in DISCOUNT_KEYS:
                            if line.pop(key, None) is not None:
                                stated.setdefault(name, set()).add(key)
                self.assertEqual(_plain(got), expected)
        self.assertEqual(stated, {
            # fi13: a row with RowDiscountPercent and RowDiscountAmount.
            "fi13_invoice.xml": set(DISCOUNT_KEYS),
            # fi30_simple_invoice: the buyer's order number.
            "fi30_simple_invoice.xml": {"order_reference"},
        })
