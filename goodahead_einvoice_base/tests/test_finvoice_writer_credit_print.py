# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the Finvoice writer for credit notes and printed letters, of the
required-value check and of ``explain``.

Runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_einvoice_base/tests -t goodahead

and inside the ERP's test runner. Every value is invented.
"""

import copy
import json
import os
import unittest

from lxml import etree

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib import finvoice
from ..lib.finvoice import writer

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _values(name="values_one_line.json"):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def _credit_note(original_number="INV/2026/00001", name="values_one_line.json"):
    values = _values(name)
    values["invoice"]["type"] = "credit_note"
    values["invoice"]["number"] = "RINV/2026/00001"
    values["invoice"]["original_number"] = original_number
    return values


def _letter():
    values = _values()
    values["receiver"] = {"edicode": None, "operator_identifier": None}
    return values


def _details(xml_bytes):
    return etree.fromstring(xml_bytes).find("InvoiceDetails")


@tagged("post_install", "-at_install")
class TestCreditNote(_TestCase):
    """AC-44 credit note with its original, AC-60 credit note without one."""

    def test_credit_note_with_original_is_schema_valid(self):
        xml = finvoice.write(_credit_note())
        self.assertEqual(finvoice.validate_schema(xml), [])
        details = _details(xml)
        self.assertEqual(details.findtext("InvoiceTypeCode"), "INV02")
        self.assertEqual(details.findtext("InvoiceTypeText"), "HYVITYSLASKU")
        self.assertEqual(details.findtext("OriginalInvoiceNumber"), "INV/2026/00001")

    def test_credit_note_without_original_has_no_reference(self):
        for original in (None, ""):
            with self.subTest(original=original):
                xml = finvoice.write(_credit_note(original_number=original))
                self.assertEqual(finvoice.validate_schema(xml), [])
                self.assertEqual(_details(xml).findtext("InvoiceTypeCode"), "INV02")
                self.assertIsNone(_details(xml).find("OriginalInvoiceNumber"))

    def test_invoice_never_names_an_original(self):
        values = _values()
        values["invoice"]["original_number"] = "INV/2025/00009"
        self.assertIsNone(_details(finvoice.write(values)).find("OriginalInvoiceNumber"))

    def test_credit_note_amounts_are_negative(self):
        root = etree.fromstring(finvoice.write(_credit_note()))
        details = root.find("InvoiceDetails")
        self.assertEqual(details.findtext("InvoiceTotalVatExcludedAmount"), "-40,00")
        self.assertEqual(details.findtext("InvoiceTotalVatAmount"), "-10,20")
        self.assertEqual(details.findtext("InvoiceTotalVatIncludedAmount"), "-50,20")
        spec = details.find("VatSpecificationDetails")
        self.assertEqual(spec.findtext("VatBaseAmount"), "-40,00")
        self.assertEqual(spec.findtext("VatRateAmount"), "-10,20")
        row = root.find("InvoiceRow")
        self.assertEqual(row.findtext("InvoicedQuantity"), "-2")
        self.assertEqual(row.findtext("UnitPriceAmount"), "20,00")
        self.assertEqual(row.findtext("RowVatAmount"), "-10,20")
        self.assertEqual(row.findtext("RowVatExcludedAmount"), "-40,00")
        self.assertEqual(row.findtext("RowAmount"), "-50,20")
        self.assertEqual(
            root.findtext("EpiDetails/EpiPaymentInstructionDetails/EpiInstructedAmount"),
            "-50,20",
        )

    def test_credit_note_rounding_keeps_the_total_consistent(self):
        values = _credit_note(name="values_rounding.json")
        xml = finvoice.write(values)
        self.assertEqual(finvoice.validate_schema(xml), [])
        rounding = _details(xml).findtext("InvoiceTotalRoundoffAmount")
        self.assertTrue(rounding.startswith("-"), rounding)

    def test_credit_note_reads_back_as_a_credit_note(self):
        values = _credit_note(name="values_three_rates.json")
        data = finvoice.to_invoice_data(finvoice.parse(finvoice.write(values))[0])
        self.assertEqual(data["type"], "CreditNote")
        self.assertEqual(data["source_meta"]["original_invoice_number"], "INV/2026/00001")
        self.assertTrue(data["source_meta"]["sign_normalised"])
        totals = values["invoice"]["totals"]
        self.assertAlmostEqual(
            data["totals"]["tax_inclusive"], totals["total"] + totals["rounding"]
        )

    def test_writer_does_not_change_the_credit_note_values(self):
        values = _credit_note()
        before = copy.deepcopy(values)
        finvoice.write(values)
        self.assertEqual(values, before)


@tagged("post_install", "-at_install")
class TestPrintedLetter(_TestCase):
    """AC-56: a printed letter goes to the print service."""

    def test_letter_goes_to_the_print_service(self):
        xml = finvoice.write(_letter(), receiver="print")
        self.assertEqual(finvoice.validate_schema(xml), [])
        to = etree.fromstring(xml).find("MessageTransmissionDetails/MessageReceiverDetails")
        self.assertEqual(to.findtext("ToIdentifier"), "TULOSTUS")
        self.assertEqual(to.findtext("ToIntermediator"), "APIX")
        self.assertEqual(writer.PRINT_RECEIVER, ("TULOSTUS", "APIX"))

    def test_letter_ignores_an_e_invoice_address(self):
        xml = finvoice.write(_values(), receiver="print")
        to = etree.fromstring(xml).find("MessageTransmissionDetails/MessageReceiverDetails")
        self.assertEqual(to.findtext("ToIdentifier"), "TULOSTUS")

    def test_credit_note_as_a_letter(self):
        values = _credit_note()
        values["receiver"] = {}
        xml = finvoice.write(values, receiver="print")
        self.assertEqual(finvoice.validate_schema(xml), [])

    def test_letter_needs_a_complete_postal_address(self):
        for key in ("street", "zip", "city"):
            values = _letter()
            values["buyer"][key] = ""
            with self.subTest(missing=key):
                with self.assertRaises(finvoice.FinvoiceError) as caught:
                    finvoice.write(values, receiver="print")
                self.assertEqual(caught.exception.code, finvoice.MISSING_FIELD)
                self.assertEqual(caught.exception.field, f"buyer.{key}")

    def test_unknown_receiver_is_refused(self):
        with self.assertRaises(ValueError):
            finvoice.write(_values(), receiver="fax")


@tagged("post_install", "-at_install")
class TestMissingValues(_TestCase):
    """A value the schema requires raises ``finvoice.missing_field``."""

    def _assert_missing(self, values, field, receiver="einvoice"):
        with self.assertRaises(finvoice.FinvoiceError) as caught:
            finvoice.write(values, receiver=receiver)
        self.assertEqual(caught.exception.code, finvoice.MISSING_FIELD)
        self.assertEqual(caught.exception.field, field)
        self.assertIn(field, str(caught.exception))

    def test_missing_buyer_name_raises(self):
        values = _values()
        values["buyer"]["name"] = ""
        self._assert_missing(values, "buyer.name")

    def test_missing_values_raise_with_their_field(self):
        cases = [
            ("seller", "name"),
            ("invoice", "number"),
            ("invoice", "date"),
            ("receiver", "edicode"),
            ("receiver", "operator_identifier"),
            ("message", "identifier"),
            ("message", "sender_edicode"),
            ("epi", "iban"),
            ("epi", "due_date"),
        ]
        for section, key in cases:
            values = _values()
            values[section][key] = None
            with self.subTest(field=f"{section}.{key}"):
                self._assert_missing(values, f"{section}.{key}")

    def test_letter_needs_no_e_invoice_address(self):
        values = _letter()
        self.assertTrue(finvoice.write(values, receiver="print"))


@tagged("post_install", "-at_install")
class TestExplain(_TestCase):
    """AC-04: a schema problem names the invoice field in plain words."""

    def test_every_row_of_the_table(self):
        cases = [
            ("/Finvoice/MessageTransmissionDetails/MessageReceiverDetails/ToIdentifier",
             "the customer's e-invoice address (invoice contact)"),
            ("/Finvoice/MessageTransmissionDetails/MessageReceiverDetails/ToIntermediator",
             "the customer's e-invoice operator (invoice contact)"),
            ("/Finvoice/BuyerPartyDetails/BuyerOrganisationName", "the customer's name"),
            ("/Finvoice/SellerPartyDetails/SellerOrganisationName", "your company's name"),
            ("/Finvoice/SellerPartyDetails/SellerPartyIdentifier",
             "your company's Business ID"),
            ("/Finvoice/EpiDetails/EpiPartyDetails/EpiBeneficiaryPartyDetails/EpiAccountID",
             "your company's bank account IBAN"),
            ("/Finvoice/EpiDetails/EpiPartyDetails/EpiBfiPartyDetails/EpiBfiIdentifier",
             "your company's bank account BIC"),
            ("/Finvoice/EpiDetails/EpiPaymentInstructionDetails/EpiDateOptionDate",
             "the invoice's due date"),
            ("/Finvoice/InvoiceDetails/PaymentTermsDetails/InvoiceDueDate",
             "the invoice's due date"),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(finvoice.explain({"path": path, "line": 1, "message": ""}),
                                 expected)
                self.assertEqual(finvoice.explain(path), expected)

    def test_unit_code_names_the_line(self):
        problem = {
            "path": "/Finvoice/InvoiceRow[2]/InvoicedQuantity",
            "line": 87,
            "message": "Element 'InvoicedQuantity', attribute 'QuantityUnitCodeUN': "
            "'k g' is not a valid value of the atomic type 'unitCodeUN'.",
        }
        self.assertEqual(finvoice.explain(problem), "the unit code of line 2 (Unit codes)")

    def test_unit_code_of_a_single_row(self):
        self.assertEqual(
            finvoice.explain("/Finvoice/InvoiceRow/InvoicedQuantity@QuantityUnitCodeUN"),
            "the unit code of line 1 (Unit codes)",
        )

    def test_unit_code_from_a_real_schema_problem(self):
        values = _values("values_three_rates.json")
        xml = finvoice.write(values).replace(b'UN="KGM"', b'UN="k g m x"')
        problems = finvoice.validate_schema(xml)
        self.assertEqual(len(problems), 1)
        self.assertEqual(finvoice.explain(problems[0]), "the unit code of line 2 (Unit codes)")

    def test_buyer_name_from_a_real_schema_problem(self):
        values = _values()
        values["buyer"]["name"] = "X"  # one character: the schema wants two
        problems = finvoice.validate_schema(finvoice.write(values))
        self.assertEqual([finvoice.explain(p) for p in problems], ["the customer's name"])

    def test_unknown_path_falls_back_to_the_element_name(self):
        self.assertEqual(
            finvoice.explain("/Finvoice/InvoiceDetails/AgreementIdentifier"), "AgreementIdentifier"
        )
        self.assertEqual(finvoice.explain("/Finvoice/InvoiceRow[3]/ArticleXyz"), "ArticleXyz")
        self.assertEqual(finvoice.explain({"path": "", "line": 0, "message": "x"}), "")

    def test_function_is_exported(self):
        from ..lib.finvoice.explain import explain as explain_function

        self.assertIs(finvoice.explain, explain_function)
