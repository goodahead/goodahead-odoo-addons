# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the Finvoice 3.0 writer for invoices.

Runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_einvoice_base/tests -t goodahead

and inside the ERP's test runner. Every values dict under ``data/values_*.json``
is invented.
"""

import copy
import glob
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


def _fixture_paths():
    return sorted(glob.glob(os.path.join(DATA_DIR, "values_*.json")))


def _values(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def _root(xml_bytes):
    return etree.fromstring(xml_bytes)


def _rows(xml_bytes):
    return _root(xml_bytes).findall("InvoiceRow")


@tagged("post_install", "-at_install")
class TestWriterSchemaAndRoundTrip(_TestCase):
    def test_every_fixture_is_schema_valid(self):
        self.assertGreaterEqual(len(_fixture_paths()), 5)
        for path in _fixture_paths():
            with self.subTest(fixture=os.path.basename(path)):
                with open(path, encoding="utf-8") as handle:
                    xml = finvoice.write(json.load(handle))
                self.assertIsInstance(xml, bytes)
                self.assertEqual(finvoice.validate_schema(xml), [])

    def test_round_trip_through_the_reader(self):
        for path in _fixture_paths():
            with open(path, encoding="utf-8") as handle:
                values = json.load(handle)
            with self.subTest(fixture=os.path.basename(path)):
                data = finvoice.to_invoice_data(finvoice.parse(finvoice.write(values))[0])
                invoice = values["invoice"]
                totals = invoice["totals"]
                rounding = totals["rounding"]
                self.assertEqual(data["type"], "Invoice")
                self.assertEqual(data["invoice_number"], invoice["number"])
                self.assertEqual(data["currency"], "EUR")
                self.assertEqual(data["payment_reference"], invoice["payment_reference"])
                self.assertEqual(str(data["due_date"]), invoice["due_date"])
                self.assertAlmostEqual(data["totals"]["tax_inclusive"], totals["total"] + rounding)
                self.assertAlmostEqual(data["totals"]["tax_exclusive"], totals["untaxed"] + rounding)
                self.assertEqual(data["seller"]["name"], values["seller"]["name"])
                self.assertEqual(data["seller"]["iban"], values["seller"]["iban"])
                self.assertEqual(data["buyer"]["name"], values["buyer"]["name"])
                lines = [line for line in data["lines"] if line["name"] != "Rounding"]
                self.assertEqual(len(lines), len(values["lines"]))
                for got, want in zip(lines, values["lines"]):
                    self.assertEqual(got["name"], want["name"])
                    self.assertEqual(got["product_code"], want["code"])
                    self.assertAlmostEqual(got["quantity"], want["quantity"])
                    self.assertAlmostEqual(got["line_total"], want["untaxed"])
                    self.assertAlmostEqual(got["tax_percent"], want["tax_percent"])
                    self.assertAlmostEqual(
                        got["price_unit"], want["price_unit"] / want["base_quantity"]
                    )
                    if want["rec20_code"]:
                        self.assertEqual(got["unit_code"], want["rec20_code"])

    def test_output_is_deterministic(self):
        values = _values("values_three_rates.json")
        self.assertEqual(finvoice.write(values), finvoice.write(copy.deepcopy(values)))

    def test_writer_does_not_change_the_values(self):
        values = _values("values_three_rates.json")
        before = copy.deepcopy(values)
        finvoice.write(values)
        self.assertEqual(values, before)


@tagged("post_install", "-at_install")
class TestWriterContent(_TestCase):
    def test_message_frame_and_receiver(self):
        root = _root(finvoice.write(_values("values_one_line.json")))
        self.assertEqual(root.tag, "Finvoice")
        self.assertEqual(root.get("Version"), "3.0")
        frame = root.find("MessageTransmissionDetails")
        self.assertEqual(frame.findtext("MessageSenderDetails/FromIdentifier"), "003700000035")
        self.assertEqual(frame.findtext("MessageSenderDetails/FromIntermediator"), "APIX")
        self.assertEqual(frame.findtext("MessageReceiverDetails/ToIdentifier"), "003700000019")
        self.assertEqual(frame.findtext("MessageReceiverDetails/ToIntermediator"), "EXAMPLEOP")
        self.assertEqual(frame.findtext("MessageDetails/MessageIdentifier"), "101-1")
        self.assertEqual(
            frame.findtext("MessageDetails/MessageTimeStamp"), "2026-11-25T10:00:00+02:00"
        )

    def test_utf8_declaration(self):
        xml = finvoice.write(_values("values_one_line.json"))
        declarations = (
            b"<?xml version='1.0' encoding='UTF-8'?>",
            b'<?xml version="1.0" encoding="UTF-8"?>',
        )
        self.assertTrue(xml.startswith(declarations))

    def test_pdf_image_is_named(self):
        root = _root(finvoice.write(_values("values_one_line.json")))
        self.assertEqual(root.findtext("InvoiceUrlNameText"), "APIX_PDFFILE")
        self.assertEqual(root.findtext("InvoiceUrlText"), "file://invoice.pdf")

    def test_invoice_details(self):
        details = _root(finvoice.write(_values("values_one_line.json"))).find("InvoiceDetails")
        self.assertEqual(details.findtext("InvoiceTypeCode"), "INV01")
        self.assertEqual(details.findtext("OriginCode"), "Original")
        self.assertEqual(details.findtext("InvoiceNumber"), "INV/2026/00001")
        self.assertEqual(details.find("InvoiceDate").text, "20261125")
        self.assertEqual(details.find("InvoiceDate").get("Format"), "CCYYMMDD")
        total = details.find("InvoiceTotalVatIncludedAmount")
        self.assertEqual(total.text, "50,20")
        self.assertEqual(total.get("AmountCurrencyIdentifier"), "EUR")
        self.assertIsNone(details.find("OriginalInvoiceNumber"))
        self.assertIsNone(details.find("InvoiceTotalRoundoffAmount"))
        self.assertEqual(details.findtext("PaymentTermsDetails/InvoiceDueDate"), "20261209")
        self.assertEqual(details.findtext("InvoiceFreeText"), "Kiitos tilauksesta.")

    def test_vat_breakdown(self):
        details = _root(finvoice.write(_values("values_three_rates.json"))).find("InvoiceDetails")
        specs = details.findall("VatSpecificationDetails")
        self.assertEqual([s.findtext("VatRatePercent") for s in specs], ["25,5", "14", "10"])
        self.assertEqual([s.findtext("VatRateAmount") for s in specs], ["38,25", "10,57", "6,00"])
        self.assertEqual([s.findtext("VatCode") for s in specs], ["S", "S", "S"])

    def test_rounding_row(self):
        details = _root(finvoice.write(_values("values_rounding.json"))).find("InvoiceDetails")
        self.assertEqual(details.findtext("InvoiceTotalRoundoffAmount"), "0,02")

    def test_payment_details(self):
        epi = _root(finvoice.write(_values("values_three_rates.json"))).find("EpiDetails")
        self.assertEqual(epi.findtext("EpiIdentificationDetails/EpiDate"), "20261125")
        bfi = epi.find("EpiPartyDetails/EpiBfiPartyDetails/EpiBfiIdentifier")
        self.assertEqual((bfi.text, bfi.get("IdentificationSchemeName")), ("EXMPFIHH", "BIC"))
        account = epi.find("EpiPartyDetails/EpiBeneficiaryPartyDetails/EpiAccountID")
        self.assertEqual((account.text, account.get("IdentificationSchemeName")),
                         ("FI1612345600000002", "IBAN"))
        instruction = epi.find("EpiPaymentInstructionDetails")
        reference = instruction.find("EpiRemittanceInfoIdentifier")
        self.assertEqual((reference.text, reference.get("IdentificationSchemeName")),
                         ("RF471234567890", "ISO"))
        self.assertEqual(instruction.findtext("EpiInstructedAmount"), "340,22")
        self.assertEqual(instruction.find("EpiCharge").get("ChargeOption"), "SLEV")
        self.assertEqual(instruction.findtext("EpiDateOptionDate"), "20261225")

    def test_national_reference_uses_spy(self):
        epi = _root(finvoice.write(_values("values_one_line.json"))).find("EpiDetails")
        reference = epi.find("EpiPaymentInstructionDetails/EpiRemittanceInfoIdentifier")
        self.assertEqual(reference.get("IdentificationSchemeName"), "SPY")

    def test_seller_account_and_parties(self):
        root = _root(finvoice.write(_values("values_one_line.json")))
        seller = root.find("SellerPartyDetails")
        self.assertEqual(seller.findtext("SellerPartyIdentifier"), "0000003-5")
        self.assertEqual(seller.findtext("SellerOrganisationTaxCode"), "FI00000035")
        self.assertEqual(seller.findtext("SellerPostalAddressDetails/CountryCode"), "FI")
        account = root.find("SellerInformationDetails/SellerAccountDetails")
        self.assertEqual(account.findtext("SellerAccountID"), "FI1612345600000002")
        self.assertEqual(account.findtext("SellerBic"), "EXMPFIHH")
        buyer = root.find("BuyerPartyDetails")
        self.assertEqual(buyer.findtext("BuyerOrganisationTaxCode"), "FI00000019")
        self.assertEqual(root.findtext("BuyerContactPersonName"), "Erkki Esimerkki")
        self.assertEqual(
            root.findtext("BuyerCommunicationDetails/BuyerEmailaddressIdentifier"),
            "ostoreskontra@example.com",
        )

    def test_missing_buyer_vat_code_is_omitted(self):
        root = _root(finvoice.write(_values("values_three_rates.json")))
        self.assertIsNone(root.find("BuyerPartyDetails/BuyerOrganisationTaxCode"))
        self.assertIsNone(root.find("BuyerContactPersonName"))
        self.assertIsNone(root.find("BuyerCommunicationDetails"))


@tagged("post_install", "-at_install")
class TestWriterRowsAndUnits(_TestCase):
    """AC-48 real quantities and units, AC-49 a unit without a code never blocks."""

    def test_row_with_unit_code_and_readable_name(self):
        rows = _rows(finvoice.write(_values("values_three_rates.json")))
        quantity = rows[0].find("InvoicedQuantity")
        self.assertEqual(quantity.text, "1,5")
        self.assertEqual(quantity.get("QuantityUnitCodeUN"), "HUR")
        self.assertEqual(quantity.get("QuantityUnitCode"), "h")
        self.assertEqual(rows[1].find("InvoicedQuantity").get("QuantityUnitCodeUN"), "KGM")
        self.assertEqual(rows[1].find("InvoicedQuantity").text, "2,5")
        self.assertEqual(rows[1].findtext("RowFreeText"), "Medium roast")
        self.assertEqual(rows[0].findtext("ArticleIdentifier"), "ZZ-WORK")
        self.assertEqual(rows[0].findtext("ArticleName"), "Consulting")
        self.assertEqual(rows[0].findtext("UnitPriceAmount"), "100,00")
        self.assertEqual(rows[0].findtext("RowVatRatePercent"), "25,5")
        self.assertEqual(rows[0].findtext("RowVatAmount"), "38,25")
        self.assertEqual(rows[0].findtext("RowVatExcludedAmount"), "150,00")
        self.assertEqual(rows[0].findtext("RowAmount"), "188,25")

    def test_base_quantity_one_is_omitted(self):
        for row in _rows(finvoice.write(_values("values_three_rates.json"))):
            self.assertIsNone(row.find("UnitPriceBaseQuantity"))

    def test_price_per_several_units_states_the_base_quantity(self):
        row = _rows(finvoice.write(_values("values_per_hundred.json")))[0]
        self.assertEqual(row.findtext("InvoicedQuantity"), "500")
        self.assertEqual(row.findtext("UnitPriceAmount"), "12,50")
        base = row.find("UnitPriceBaseQuantity")
        self.assertIsNotNone(base)
        self.assertEqual(base.text, "100")
        self.assertEqual(base.get("QuantityUnitCodeUN"), "C62")

    def test_unit_without_code_sends_the_name_only(self):
        rows = _rows(finvoice.write(_values("values_unit_without_code.json")))
        for row in rows:
            quantity = row.find("InvoicedQuantity")
            self.assertNotIn("QuantityUnitCodeUN", quantity.attrib)
        self.assertEqual(rows[1].find("InvoicedQuantity").get("QuantityUnitCode"), "erä")

    def test_long_unit_name_is_cut_to_14_characters(self):
        rows = _rows(finvoice.write(_values("values_unit_without_code.json")))
        name = rows[0].find("InvoicedQuantity").get("QuantityUnitCode")
        self.assertEqual(name, "lahjapakkausla")
        self.assertEqual(len(name), 14)

    def test_line_without_code_has_no_article_identifier(self):
        rows = _rows(finvoice.write(_values("values_three_rates.json")))
        self.assertIsNone(rows[3].find("ArticleIdentifier"))

    def test_optional_row_vat_code(self):
        values = _values("values_one_line.json")
        values["lines"][0]["tax_code"] = "S"
        row = _rows(finvoice.write(values))[0]
        self.assertEqual(row.findtext("RowVatCode"), "S")
        self.assertEqual(finvoice.validate_schema(finvoice.write(values)), [])


@tagged("post_install", "-at_install")
class TestWriterFormatting(_TestCase):
    def test_amount(self):
        self.assertEqual(writer.format_amount(40), "40,00")
        self.assertEqual(writer.format_amount(10.2), "10,20")
        self.assertEqual(writer.format_amount(-1234.567), "-1234,57")
        self.assertEqual(writer.format_amount("0"), "0,00")

    def test_unit_price_keeps_up_to_five_decimals(self):
        self.assertEqual(writer.format_unit_price(12.5), "12,50")
        self.assertEqual(writer.format_unit_price(0.12345), "0,12345")
        self.assertEqual(writer.format_unit_price(0.123456), "0,12346")

    def test_quantity_and_percent(self):
        self.assertEqual(writer.format_quantity(500.0), "500")
        self.assertEqual(writer.format_quantity(1.5), "1,5")
        self.assertEqual(writer.format_percent(25.5), "25,5")
        self.assertEqual(writer.format_percent(24.0), "24")
        self.assertEqual(writer.format_percent(0), "0")

    def test_date(self):
        import datetime

        self.assertEqual(writer.format_date("2026-11-25"), "20261125")
        self.assertEqual(writer.format_date(datetime.date(2026, 1, 2)), "20260102")
