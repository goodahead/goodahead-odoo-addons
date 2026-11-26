# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the new Finvoice content the writer states (finvoice content T05;
AC-01, AC-14, AC-16, AC-17, AC-19, AC-20, AC-25).

Runs without the ERP (``python -I -m unittest discover -s
goodahead/goodahead_einvoice_base/tests -t goodahead``) and inside the ERP's
test runner. Every ``data/content_*.json`` is invented.
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


def _content_paths():
    return sorted(glob.glob(os.path.join(DATA_DIR, "content_*.json")))


def _values(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def _root(values):
    return etree.fromstring(finvoice.write(values))


def _children(element):
    return [child.tag for child in element]


@tagged("post_install", "-at_install")
class TestContentFixtures(_TestCase):
    def test_every_content_fixture_is_schema_valid(self):
        self.assertGreaterEqual(len(_content_paths()), 7)
        for path in _content_paths():
            with self.subTest(fixture=os.path.basename(path)):
                with open(path, encoding="utf-8") as handle:
                    xml = finvoice.write(json.load(handle))
                self.assertEqual(finvoice.validate_schema(xml), [])

    def test_existing_fixtures_write_as_before(self):
        # The new keys are optional: an existing values dict writes none of
        # the new elements.
        root = _root(_values("values_three_rates.json"))
        details = root.find("InvoiceDetails")
        for tag in ("InvoiceTypeCodeUN", "SellerReferenceIdentifier", "OrderIdentifier",
                    "BuyerReferenceIdentifier", "RowsTotalVatExcludedAmount"):
            self.assertIsNone(details.find(tag), tag)
        self.assertIsNone(root.find("DeliveryPartyDetails"))
        self.assertIsNone(root.find("EpiDetails/EpiPaymentInstructionDetails/EpiPaymentMeansCode"))
        for row in root.findall("InvoiceRow"):
            self.assertIsNone(row.find("EanCode"))
            self.assertIsNone(row.find("RowDiscountPercent"))


@tagged("post_install", "-at_install")
class TestVatBreakdownContent(_TestCase):
    def test_reverse_charge_entry(self):
        root = _root(_values("content_reverse_charge.json"))
        spec = root.find("InvoiceDetails/VatSpecificationDetails")
        self.assertEqual(_children(spec), [
            "VatBaseAmount", "VatRatePercent", "VatCode", "VatRateAmount",
            "VatFreeText", "VatFreeText", "VatExemptionReasonCode",
        ])
        self.assertEqual(spec.findtext("VatRatePercent"), "0")
        self.assertEqual(spec.findtext("VatCode"), "AE")
        self.assertEqual(spec.findtext("VatRateAmount"), "0,00")
        self.assertEqual([t.text for t in spec.findall("VatFreeText")],
                         ["Käännetty verovelvollisuus", "Reverse charge"])
        self.assertEqual(spec.findtext("VatExemptionReasonCode"), "VATEX-EU-AE")
        self.assertEqual(root.findtext("InvoiceRow/RowVatCode"), "AE")
        self.assertEqual(root.findtext("BuyerPartyDetails/BuyerOrganisationTaxCode"),
                         "SE000000000101")

    def test_three_entries_for_three_categories(self):
        details = _root(_values("content_categories.json")).find("InvoiceDetails")
        specs = details.findall("VatSpecificationDetails")
        self.assertEqual([(s.findtext("VatRatePercent"), s.findtext("VatCode")) for s in specs],
                         [("25,5", "S"), ("0", "K"), ("0", "G")])
        self.assertEqual(specs[0].findall("VatFreeText"), [])
        self.assertIsNone(specs[0].find("VatExemptionReasonCode"))
        self.assertEqual(len(specs[1].findall("VatFreeText")), 2)

    def test_entry_without_category_states_nothing(self):
        details = _root(_values("content_text_rows.json")).find("InvoiceDetails")
        unclassified = details.findall("VatSpecificationDetails")[1]
        self.assertEqual(_children(unclassified),
                         ["VatBaseAmount", "VatRatePercent", "VatRateAmount"])

    def test_entry_without_category_ignores_stray_reasons(self):
        values = _values("content_text_rows.json")
        values["taxes"][1]["reason_lines"] = ["Stray"]
        values["taxes"][1]["exemption_code"] = "VATEX-EU-AE"
        unclassified = _root(values).findall("InvoiceDetails/VatSpecificationDetails")[1]
        self.assertEqual(_children(unclassified),
                         ["VatBaseAmount", "VatRatePercent", "VatRateAmount"])

    def test_three_reason_lines_of_70(self):
        spec = _root(_values("content_edges.json")).find("InvoiceDetails/VatSpecificationDetails")
        self.assertEqual([len(t.text) for t in spec.findall("VatFreeText")], [70, 70, 70])
        self.assertEqual(spec.findtext("VatExemptionReasonCode"), "VATEX-EU-132-1A")


@tagged("post_install", "-at_install")
class TestStandardCodes(_TestCase):
    def test_invoice(self):
        root = _root(_values("content_categories.json"))
        details = root.find("InvoiceDetails")
        tags = _children(details)
        self.assertEqual(tags[:3], ["InvoiceTypeCode", "InvoiceTypeCodeUN", "InvoiceTypeText"])
        self.assertEqual(details.findtext("InvoiceTypeCodeUN"), "380")
        self.assertEqual(details.findtext("RowsTotalVatExcludedAmount"), "300,00")
        self.assertEqual(details.findtext("InvoiceTotalVatExcludedAmount"), "300,00")
        self.assertLess(tags.index("RowsTotalVatExcludedAmount"),
                        tags.index("InvoiceTotalVatExcludedAmount"))
        instruction = root.find("EpiDetails/EpiPaymentInstructionDetails")
        self.assertEqual(_children(instruction)[-2:], ["EpiDateOptionDate", "EpiPaymentMeansCode"])
        self.assertEqual(instruction.findtext("EpiPaymentMeansCode"), "58")

    def test_credit_note(self):
        root = _root(_values("content_credit_note.json"))
        details = root.find("InvoiceDetails")
        self.assertEqual(details.findtext("InvoiceTypeCode"), "INV02")
        self.assertEqual(details.findtext("InvoiceTypeCodeUN"), "381")
        self.assertEqual(details.findtext("RowsTotalVatExcludedAmount"), "-80,00")

    def test_payment_means_code(self):
        self.assertEqual(writer.payment_means_code("EUR"), "58")
        self.assertEqual(writer.payment_means_code("eur"), "58")
        self.assertEqual(writer.payment_means_code("SEK"), "30")
        self.assertEqual(writer.payment_means_code(None), "30")
        root = _root(_values("content_edges.json"))
        self.assertEqual(root.findtext("EpiDetails/EpiPaymentInstructionDetails/"
                                       "EpiPaymentMeansCode"), "30")

    def test_type_code_un(self):
        self.assertEqual(writer.INVOICE_TYPES_UN, {"invoice": "380", "credit_note": "381"})

    def test_no_conformance_marker(self):
        for path in _content_paths():
            with open(path, encoding="utf-8") as handle:
                xml = finvoice.write(json.load(handle))
            with self.subTest(fixture=os.path.basename(path)):
                self.assertNotIn(b"SpecificationIdentifier", xml)
                self.assertNotIn(b"urn:cen.eu:en16931", xml)


@tagged("post_install", "-at_install")
class TestReferencesAndDelivery(_TestCase):
    def test_references_in_their_places(self):
        details = _root(_values("content_delivery_references.json")).find("InvoiceDetails")
        tags = _children(details)
        self.assertEqual(details.findtext("SellerReferenceIdentifier"), "S" * 70)
        self.assertEqual(details.findtext("OrderIdentifier"), "P" * 70)
        self.assertEqual(details.findtext("BuyerReferenceIdentifier"), "B" * 70)
        order = [tags.index(tag) for tag in (
            "InvoiceDate", "SellerReferenceIdentifier", "OrderIdentifier",
            "BuyerReferenceIdentifier", "RowsTotalVatExcludedAmount")]
        self.assertEqual(order, sorted(order))

    def test_identifiers_are_never_cut(self):
        values = _values("content_delivery_references.json")
        values["invoice"]["order_number"] = "P" * 71
        xml = finvoice.write(values)
        self.assertEqual(etree.fromstring(xml).findtext("InvoiceDetails/OrderIdentifier"),
                         "P" * 71)
        problems = finvoice.validate_schema(xml)
        self.assertTrue(problems)
        self.assertEqual(finvoice.explain(problems[0]), "the customer reference on the invoice")

    def test_delivery_party(self):
        root = _root(_values("content_delivery_references.json"))
        tags = _children(root)
        self.assertEqual(tags.index("DeliveryPartyDetails"),
                         tags.index("BuyerCommunicationDetails") + 1)
        party = root.find("DeliveryPartyDetails")
        self.assertEqual(party.findtext("DeliveryOrganisationName"),
                         "Example Delivery Site With A Rather")
        self.assertLessEqual(len(party.findtext("DeliveryOrganisationName")), 35)
        address = party.find("DeliveryPostalAddressDetails")
        self.assertEqual(_children(address), [
            "DeliveryStreetName", "DeliveryTownName", "DeliveryPostCodeIdentifier",
            "CountryCode"])
        self.assertEqual(address.findtext("DeliveryStreetName"), "Toimituskatu 5")
        self.assertEqual(address.findtext("DeliveryTownName"), "Turku")
        self.assertEqual(address.findtext("DeliveryPostCodeIdentifier"), "20100")
        self.assertEqual(root.findtext("BuyerPartyDetails/BuyerOrganisationName"),
                         "Example Buyer Oy")

    def test_delivery_second_street_line(self):
        """review F6: ``delivery.street2`` is the second DeliveryStreetName,
        cut to 35 like the first."""
        values = _values("content_delivery_references.json")
        values["delivery"]["street2"] = "Rakennus C, lastauslaituri 4 " + "x" * 20
        root = _root(values)
        streets = root.findall(
            "DeliveryPartyDetails/DeliveryPostalAddressDetails/DeliveryStreetName")
        self.assertEqual(len(streets), 2)
        self.assertEqual(streets[0].text, "Toimituskatu 5")
        self.assertEqual(len(streets[1].text), 35)
        self.assertTrue(streets[1].text.startswith("Rakennus C"))
        self.assertEqual(finvoice.validate_schema(etree.tostring(root)), [])

    def test_delivery_party_is_after_the_buyer_without_communication(self):
        values = _values("content_delivery_references.json")
        values["buyer"]["email"] = None
        root = _root(values)
        tags = _children(root)
        self.assertEqual(tags.index("DeliveryPartyDetails"),
                         tags.index("BuyerContactPersonName") + 1)
        self.assertEqual(finvoice.validate_schema(etree.tostring(root)), [])


@tagged("post_install", "-at_install")
class TestRowsContent(_TestCase):
    def test_text_rows_in_place(self):
        root = _root(_values("content_text_rows.json"))
        rows = root.findall("InvoiceRow")
        self.assertEqual(len(rows), 4)
        self.assertEqual(_children(rows[0]), ["RowFreeText"])
        self.assertEqual(rows[0].findtext("RowFreeText"), "Section A: deliveries")
        self.assertEqual(rows[1].findtext("ArticleName"), "Standard goods")
        self.assertEqual(set(_children(rows[2])), {"RowFreeText"})
        texts = [t.text for t in rows[2].findall("RowFreeText")]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text) <= 512 for text in texts))
        self.assertEqual(" ".join(texts).split(),
                         [f"note-word-{i:03d}" for i in range(80)])

    def test_text_row_from_a_plain_text(self):
        values = _values("content_text_rows.json")
        values["lines"][0] = {"kind": "text", "description": "  Section B  "}
        row = _root(values).find("InvoiceRow")
        self.assertEqual([t.text for t in row.findall("RowFreeText")], ["Section B"])

    def test_text_rows_change_no_total(self):
        details = _root(_values("content_text_rows.json")).find("InvoiceDetails")
        self.assertEqual(details.findtext("RowsTotalVatExcludedAmount"), "110,00")
        self.assertEqual(len(details.findall("VatSpecificationDetails")), 2)

    def test_discount_row(self):
        row = _root(_values("content_discounts.json")).find("InvoiceRow")
        tags = _children(row)
        self.assertEqual(row.findtext("UnitPriceAmount"), "100,00")
        self.assertEqual(row.findtext("RowDiscountPercent"), "15")
        self.assertEqual(row.findtext("RowDiscountAmount"), "150,00")
        self.assertEqual(row.find("RowDiscountAmount").get("AmountCurrencyIdentifier"), "EUR")
        self.assertEqual(row.findtext("RowVatExcludedAmount"), "850,00")
        self.assertLess(tags.index("RowDiscountAmount"), tags.index("RowVatRatePercent"))
        self.assertLess(tags.index("RowDiscountPercent"), tags.index("RowDiscountAmount"))

    def test_hundred_percent_and_per_hundred(self):
        rows = _root(_values("content_discounts.json")).findall("InvoiceRow")
        self.assertEqual(rows[1].findtext("RowDiscountPercent"), "100")
        self.assertEqual(rows[1].findtext("RowDiscountAmount"), "50,00")
        self.assertEqual(rows[1].findtext("RowVatExcludedAmount"), "0,00")
        self.assertEqual(rows[2].findtext("UnitPriceBaseQuantity"), "100")
        self.assertEqual(rows[2].findtext("RowDiscountAmount"), "10,00")
        self.assertIsNone(rows[3].find("RowDiscountPercent"))
        self.assertIsNone(rows[3].find("RowDiscountAmount"))

    def test_credit_note_discount_is_negative(self):
        row = _root(_values("content_credit_note.json")).find("InvoiceRow")
        self.assertEqual(row.findtext("UnitPriceAmount"), "50,00")
        self.assertEqual(row.findtext("RowDiscountPercent"), "20")
        self.assertEqual(row.findtext("RowDiscountAmount"), "-20,00")
        self.assertEqual(row.findtext("RowVatExcludedAmount"), "-80,00")
        self.assertEqual(row.findtext("InvoicedQuantity"), "-2")

    def test_trade_item_number(self):
        rows = _root(_values("content_delivery_references.json")).findall("InvoiceRow")
        ean = rows[0].find("EanCode")
        self.assertEqual((ean.text, ean.get("SchemeID")), ("4006381333931", "0160"))
        tags = _children(rows[0])
        self.assertEqual(tags.index("EanCode"), tags.index("ArticleName") + 1)
        self.assertIsNone(rows[1].find("EanCode"))

    def test_row_vat_code(self):
        rows = _root(_values("content_categories.json")).findall("InvoiceRow")
        self.assertEqual([r.findtext("RowVatCode") for r in rows], ["S", "K", "G"])
        unclassified = _root(_values("content_text_rows.json")).findall("InvoiceRow")[3]
        self.assertIsNone(unclassified.find("RowVatCode"))

    def test_writer_does_not_change_the_values(self):
        for path in _content_paths():
            with open(path, encoding="utf-8") as handle:
                values = json.load(handle)
            before = copy.deepcopy(values)
            finvoice.write(values)
            with self.subTest(fixture=os.path.basename(path)):
                self.assertEqual(values, before)


@tagged("post_install", "-at_install")
class TestExplainContent(_TestCase):
    def test_new_paths(self):
        cases = {
            "/Finvoice/DeliveryPartyDetails/DeliveryOrganisationName":
                "the delivery address on the invoice",
            "/Finvoice/DeliveryPartyDetails/DeliveryPostalAddressDetails/DeliveryStreetName":
                "the delivery address on the invoice",
            "/Finvoice/DeliveryPartyDetails/DeliveryPostalAddressDetails/DeliveryTownName":
                "the delivery address on the invoice",
            "/Finvoice/DeliveryPartyDetails/DeliveryPostalAddressDetails/"
            "DeliveryPostCodeIdentifier": "the delivery address on the invoice",
            "/Finvoice/InvoiceDetails/OrderIdentifier": "the customer reference on the invoice",
            "/Finvoice/InvoiceDetails/SellerReferenceIdentifier":
                "the source document on the invoice",
            "/Finvoice/InvoiceDetails/BuyerReferenceIdentifier":
                "the buyer reference on the invoice",
            "/Finvoice/InvoiceDetails/VatSpecificationDetails[2]/VatFreeText[1]":
                "the VAT reason or exemption reason code of the tax",
            "/Finvoice/InvoiceDetails/VatSpecificationDetails/VatExemptionReasonCode":
                "the VAT reason or exemption reason code of the tax",
            "/Finvoice/InvoiceRow[3]/EanCode": "the product's barcode on line 3",
            "/Finvoice/InvoiceRow[2]/RowDiscountPercent": "the discount of line 2",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(finvoice.explain(path), expected)


if __name__ == "__main__":
    unittest.main()
