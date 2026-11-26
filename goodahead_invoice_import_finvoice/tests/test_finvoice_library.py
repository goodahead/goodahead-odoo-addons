# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Unit tests for the pure-Python Finvoice library.

Runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_invoice_import_finvoice/tests -t goodahead

and, once the add-on has a manifest, inside the ERP's test runner as well:
the ERP base class is used when it can be imported, plain ``unittest`` when not.
Every fixture under ``data/`` is invented.
"""

import glob
import os
import re
import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib import finvoice
from ..lib.finvoice import values
from ..lib.finvoice.errors import (
    MISSING_FIELD,
    NOT_AN_INVOICE,
    UNREADABLE,
    UNSUPPORTED_VERSION,
    FinvoiceError,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib")


def _raw(name):
    with open(os.path.join(DATA_DIR, name), "rb") as handle:
        return handle.read()


def _one(name_or_bytes):
    raw = _raw(name_or_bytes) if isinstance(name_or_bytes, str) else name_or_bytes
    messages = finvoice.parse(raw)
    assert len(messages) == 1, messages
    return finvoice.to_invoice_data(messages[0])


def _edit(name, old, new):
    raw = _raw(name)
    assert old.encode() in raw, (name, old)
    return raw.replace(old.encode(), new.encode())


@tagged("post_install", "-at_install")
class TestFinvoiceParse(_TestCase):
    def test_single_message_version(self):
        messages = finvoice.parse(_raw("fi30_simple_invoice.xml"))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].version, "3.0")
        self.assertTrue(messages[0].xml.startswith(b"<?xml"))

    def test_two_messages_with_soap_frames(self):
        # guideline §17: SOAP frame + message, repeated, in one file.
        messages = finvoice.parse(_raw("fi30_two_messages.xml"))
        self.assertEqual([m.index for m in messages], [0, 1])
        numbers = [finvoice.to_invoice_data(m)["invoice_number"] for m in messages]
        self.assertEqual(numbers, ["B-1", "B-2"])

    def test_message_inside_a_soap_body(self):
        inner = _raw("fi30_simple_invoice.xml").split(b"?>", 2)[2]
        wrapped = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
            b"<SOAP-ENV:Body>" + inner + b"</SOAP-ENV:Body></SOAP-ENV:Envelope>"
        )
        self.assertEqual(_one(wrapped)["invoice_number"], "10045")

    def test_versions_2_0_and_2_01_are_accepted(self):
        self.assertEqual(finvoice.parse(_raw("fi20_invoice.xml"))[0].version, "2.0")
        self.assertEqual(finvoice.parse(_raw("fi201_invoice.xml"))[0].version, "2.01")

    def test_version_1_3_is_accepted(self):
        """AC-33: operators still deliver 1.3; its elements read like 2.0's."""
        data = _one("fi13_invoice.xml")
        self.assertEqual(data["source_meta"]["version"], "1.3")
        self.assertEqual(data["invoice_number"], "13-1")
        self.assertEqual(data["due_date"], "2026-10-15")
        self.assertEqual([line["line_total"] for line in data["lines"]], [60.0, 15.0],
                         "the row totals are already net of the discount")
        self.assertEqual(data["totals"]["line_extension"], 75.0)
        self.assertEqual(data["totals"]["payable"], 94.13)

    def test_vat_rate_from_the_header_when_nothing_else_states_it(self):
        """AC-34: no row rate, no row VAT, no breakdown: the header's VAT and
        VAT-exclusive totals make exactly 25.5 %."""
        data = _one("fi13_invoice.xml")
        self.assertEqual({line["tax_percent"] for line in data["lines"]}, {25.5})
        self.assertEqual(
            [(s["tax_percent"], s["taxable_amount"], s["tax_amount"]) for s in data["tax_subtotals"]],
            [(25.5, 75.0, 19.13)],
        )
        self.assertTrue(any("header" in w for w in data["source_meta"]["warnings"]))

    def test_header_totals_that_make_no_vat_rate_leave_the_rows_without_one(self):
        """AC-34: 18.00 on 75.00 is 24 % to the cent, but 18.50 is no rate."""
        raw = _edit("fi13_invoice.xml", "<InvoiceTotalVatAmount AmountCurrencyIdentifier=\"EUR\">19,13",
                    "<InvoiceTotalVatAmount AmountCurrencyIdentifier=\"EUR\">18,50")
        data = _one(raw)
        self.assertEqual({line["tax_percent"] for line in data["lines"]}, {None})
        self.assertFalse(any("header" in w for w in data["source_meta"]["warnings"]))

    def test_unknown_and_missing_version_are_refused(self):
        for replacement in ('Version="4.0"', 'Version="1.2"', ""):
            raw = _edit("fi30_simple_invoice.xml", 'Version="3.0"', replacement)
            with self.assertRaises(FinvoiceError) as caught:
                finvoice.parse(raw)
            self.assertEqual(caught.exception.code, UNSUPPORTED_VERSION)

    def test_not_xml_is_unreadable(self):
        with self.assertRaises(FinvoiceError) as caught:
            finvoice.parse(_raw("not_xml.xml"))
        self.assertEqual(caught.exception.code, UNREADABLE)

    def test_empty_broken_and_foreign_xml_are_unreadable(self):
        broken = _raw("fi30_simple_invoice.xml")[:-40]
        for raw in (b"", b"   ", broken, b"<Invoice><ID>1</ID></Invoice>"):
            with self.assertRaises(FinvoiceError) as caught:
                finvoice.parse(raw)
            self.assertEqual(caught.exception.code, UNREADABLE, raw[:30])

    def test_iso_8859_15_message(self):
        # guideline §9.3: Finvoice's own character set.
        text = _raw("fi30_simple_invoice.xml").decode("utf-8")
        text = text.replace('encoding="UTF-8"', 'encoding="ISO-8859-15"')
        data = _one(text.encode("iso-8859-15"))
        self.assertEqual(data["lines"][1]["name"], "Asennustyö")

    def test_default_namespace_is_tolerated(self):
        raw = _edit(
            "fi30_simple_invoice.xml",
            '<Finvoice Version="3.0"',
            '<Finvoice xmlns="urn:example:finvoice" Version="3.0"',
        )
        self.assertEqual(_one(raw)["totals"]["payable"], 451.8)

    def test_entities_are_not_expanded(self):
        raw = (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/hosts">]>'
            b'<Finvoice Version="3.0"><InvoiceDetails><InvoiceNumber>&e;</InvoiceNumber>'
            b"</InvoiceDetails></Finvoice>"
        )
        try:
            messages = finvoice.parse(raw)
        except FinvoiceError as error:
            self.assertEqual(error.code, UNREADABLE)
            return
        self.assertNotIn(b"localhost", messages[0].xml)


@tagged("post_install", "-at_install")
class TestFinvoiceSchema(_TestCase):
    def test_every_3_0_fixture_is_schema_valid(self):
        checked = 0
        for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.xml"))):
            with open(path, "rb") as handle:
                raw = handle.read()
            if b'Version="3.0"' not in raw:
                continue
            with self.subTest(fixture=os.path.basename(path)):
                self.assertEqual(finvoice.validate_schema(raw), [])
                checked += 1
        self.assertGreaterEqual(checked, 11)

    def test_invalid_3_0_message_is_reported(self):
        raw = _edit("fi30_simple_invoice.xml", "<InvoiceNumber>10045</InvoiceNumber>", "")
        problems = finvoice.validate_schema(raw)
        self.assertTrue(problems)
        self.assertTrue(any("InvoiceDate" in p or "InvoiceNumber" in p for p in problems))

    def test_other_versions_are_not_schema_checked(self):
        self.assertEqual(finvoice.validate_schema(_raw("fi201_invoice.xml")), [])

    def test_schema_is_the_reference_copy(self):
        self.assertTrue(os.path.isfile(finvoice.XSD_PATH))


@tagged("post_install", "-at_install")
class TestFinvoiceMapping(_TestCase):
    def assertReconciles(self, data):
        """The core's arithmetic: lines sum to line_extension,
        tax_exclusive + subtotal tax = tax_inclusive, payable = tax_inclusive."""
        totals = data["totals"]
        self.assertAlmostEqual(sum(l["line_total"] for l in data["lines"]),
                               totals["line_extension"], places=2)
        self.assertAlmostEqual(
            totals["tax_exclusive"] + sum(s["tax_amount"] for s in data["tax_subtotals"]),
            totals["tax_inclusive"], places=2)
        self.assertAlmostEqual(totals["payable"], totals["tax_inclusive"], places=2)
        self.assertAlmostEqual(totals["line_extension"], totals["tax_exclusive"], places=2)

    def test_every_supported_fixture_maps_and_reconciles(self):
        for path in sorted(glob.glob(os.path.join(DATA_DIR, "fi[23]*.xml"))):
            with open(path, "rb") as handle:
                raw = handle.read()
            with self.subTest(fixture=os.path.basename(path)):
                for message in finvoice.parse(raw):
                    data = finvoice.to_invoice_data(message)
                    self.assertIn(data["type"], ("Invoice", "CreditNote"))
                    self.assertRegex(data["issue_date"], r"^\d{4}-\d{2}-\d{2}$")
                    self.assertRegex(data["currency"], r"^[A-Z]{3}$")
                    self.assertTrue(data["lines"])
                    for line in data["lines"]:
                        self.assertIn("tax_amount", line)
                        self.assertGreater(line["quantity"], 0)
                    self.assertReconciles(data)
                    self.assertEqual(data["source_meta"]["format"], "finvoice")

    def test_simple_invoice(self):
        data = _one("fi30_simple_invoice.xml")
        self.assertEqual(data["type"], "Invoice")
        self.assertEqual(data["invoice_number"], "10045")
        self.assertEqual(data["issue_date"], "2026-11-15")
        self.assertEqual(data["due_date"], "2026-11-29")
        self.assertEqual(data["currency"], "EUR")
        self.assertEqual(data["payment_reference"], "10001235")
        self.assertEqual(data["note"], "Kiitos tilauksesta.")
        seller = data["seller"]
        self.assertEqual(seller, {
            "name": "Example Supplier Oy",
            "vat": "FI00000035",
            "business_id": "0000003-5",
            "street": "Esimerkkikatu 1",
            "city": "Helsinki",
            "postal_code": "00100",
            "country_code": "FI",
            "phone": "+358 40 0000001",
            "email": "laskutus@example.test",
            "website": "www.example.test",
            "iban": "FI1612345600000002",
            "bic": "EXMPFIHH",
        })
        self.assertEqual(data["buyer"]["name"], "Example Buyer Oy")
        self.assertEqual(data["buyer"]["vat"], "FI00000051")
        first, second = data["lines"]
        self.assertEqual(first["product_code"], "ART-001")
        self.assertEqual(first["name"], "Toimistotuoli")
        self.assertEqual(first["description"], "Väri musta")
        self.assertEqual((first["quantity"], first["unit_code"], first["price_unit"]), (2.0, "C62", 120.0))
        self.assertEqual((first["line_total"], first["tax_percent"], first["tax_amount"]), (240.0, 25.5, 61.2))
        self.assertEqual(first["tax_category"], "S")
        self.assertEqual((second["quantity"], second["unit_code"]), (1.5, "HUR"))
        self.assertEqual(data["tax_subtotals"], [
            {"tax_percent": 25.5, "taxable_amount": 360.0, "tax_amount": 91.8, "tax_category": "S"},
        ])
        self.assertEqual(data["totals"], {
            "line_extension": 360.0, "tax_exclusive": 360.0, "tax_inclusive": 451.8, "payable": 451.8,
        })
        meta = data["source_meta"]
        self.assertEqual((meta["format"], meta["version"]), ("finvoice", "3.0"))
        self.assertIsNone(meta["original_invoice_number"])
        self.assertIsNone(meta["rounding"])

    def test_business_id_in_vat_field(self):
        # AC-02: the Business ID is recognised wherever the sender put it.
        seller = _one("fi30_business_id_in_vat_field.xml")["seller"]
        self.assertEqual(seller["business_id"], "0000001-9")
        self.assertEqual(seller["vat"], "FI00000019")

    def test_iban_falls_back_past_a_failing_checksum(self):
        # The EPI account fails mod-97; the seller account passes, with its BIC.
        seller = _one("fi30_business_id_in_vat_field.xml")["seller"]
        self.assertEqual(seller["iban"], "FI5912345600000004")
        self.assertEqual(seller["bic"], "SELLFIHH")

    def test_no_valid_iban_gives_none(self):
        raw = _edit("fi30_business_id_in_vat_field.xml", "FI5912345600000004", "FI3312345600000005")
        seller = _one(raw)["seller"]
        self.assertIsNone(seller["iban"])
        self.assertEqual(seller["bic"], "EPIBFIHH")

    def test_no_vat_derives_finnish_vat_from_business_id(self):
        data = _one("fi30_no_vat.xml")
        self.assertEqual(data["seller"]["business_id"], "0000007-8")
        self.assertEqual(data["seller"]["vat"], "FI00000078")

    def test_no_vat_derivation_for_a_foreign_seller(self):
        raw = _edit("fi30_no_vat.xml", "<CountryCode>FI</CountryCode>", "<CountryCode>SE</CountryCode>")
        self.assertIsNone(_one(raw)["seller"]["vat"])

    def test_no_vat_derivation_from_an_invalid_business_id(self):
        raw = _edit("fi30_no_vat.xml", "0000007-8", "0000006-1")
        self.assertIsNone(_one(raw)["seller"]["vat"])

    def test_seller_reference_used_only_when_it_is_a_reference_number(self):
        self.assertEqual(_one("fi30_no_vat.xml")["payment_reference"], "30007895")
        raw = _edit("fi30_no_vat.xml", "<SellerReferenceIdentifier>30007895",
                    "<SellerReferenceIdentifier>30007896")
        self.assertIsNone(_one(raw)["payment_reference"])

    def test_multi_rate_vat(self):
        data = _one("fi30_multi_rate.xml")
        rows = [(s["tax_percent"], s["taxable_amount"], s["tax_amount"], s["tax_category"])
                for s in data["tax_subtotals"]]
        self.assertEqual(rows, [
            (10.0, 40.0, 4.0, "S"),
            (14.0, 30.0, 4.2, "S"),
            (25.5, 100.0, 25.5, "S"),
            (0.0, 15.0, 0.0, "E"),
            (0.0, 5.0, 0.0, "Z"),  # rate 0 without a VatCode -> Z
        ])
        self.assertEqual([l["tax_category"] for l in data["lines"]], ["S", "S", "S", "E", "Z"])
        self.assertEqual([l["tax_percent"] for l in data["lines"]], [10.0, 14.0, 25.5, 0.0, 0.0])

    def test_rounding_becomes_a_line(self):
        data = _one("fi30_rounding.xml")
        rounding = data["lines"][-1]
        self.assertEqual((rounding["name"], rounding["line_total"], rounding["tax_percent"]),
                         ("Rounding", 0.01, 0.0))
        self.assertEqual(data["source_meta"]["rounding"], 0.01)
        self.assertEqual(data["totals"], {
            "line_extension": 80.0, "tax_exclusive": 80.0, "tax_inclusive": 100.4, "payable": 100.4,
        })
        self.assertReconciles(data)

    def test_sub_rows_and_free_text_rows(self):
        data = _one("fi30_subrows_freetext.xml")
        # guideline §11: the SubInvoiceRow summary is not a line.
        self.assertEqual([l["name"] for l in data["lines"]], ["Ruuvi", "Mutteri", "Rahti"])
        self.assertEqual(data["lines"][0]["description"], "Sinkitty, M6")
        self.assertEqual(data["lines"][2]["description"], "Toimitus viikolla 48")
        self.assertEqual(data["note"], "Tilauksenne 4500012345")
        self.assertEqual(data["totals"]["line_extension"], 25.0)
        self.assertTrue(any("SubInvoiceRow" in w for w in data["source_meta"]["warnings"]))
        self.assertTrue(data["source_meta"]["amounts_stated"])

    def test_subline_the_stated_total_includes_is_booked(self):
        # AC-28: the rows make 360, the stated total 300 includes the -60
        # credit in the subline, so the subline is a line of its own.
        data = _one("fi30_subrow_adjustment.xml")
        self.assertEqual([l["name"] for l in data["lines"]],
                         ["Toimistotuoli", "Asennustyö", "Ennakkomaksu"])
        credit = data["lines"][2]
        self.assertEqual((credit["quantity"], credit["price_unit"], credit["line_total"],
                          credit["tax_percent"], credit["tax_amount"]),
                         (1.0, -60.0, -60.0, 25.5, -15.3))
        self.assertEqual(credit["description"], "Hyvitys maksetusta ennakosta")
        self.assertTrue(any("booked as lines" in w for w in data["source_meta"]["warnings"]))
        self.assertReconciles(data)

    def test_subline_is_left_out_when_neither_sum_makes_the_total(self):
        # AC-28: neither 360 nor 300 makes a stated 310: the subline is left
        # out as a summary and the bill shows the difference.
        raw = _edit("fi30_subrow_adjustment.xml",
                    '<InvoiceTotalVatExcludedAmount AmountCurrencyIdentifier="EUR">300,00',
                    '<InvoiceTotalVatExcludedAmount AmountCurrencyIdentifier="EUR">310,00')
        data = _one(raw)
        self.assertEqual([l["name"] for l in data["lines"]], ["Toimistotuoli", "Asennustyö"])
        self.assertTrue(any("not booked" in w for w in data["source_meta"]["warnings"]))

    def test_credit_row_with_negative_quantity_and_unsigned_amounts(self):
        # AC-24: a sender writes the credit row's quantity negative and its
        # amounts positive, under a negative document total.
        raw = _edit("fi30_credit_note_negative.xml",
                    '<RowVatExcludedAmount AmountCurrencyIdentifier="EUR">-100,00',
                    '<RowVatExcludedAmount AmountCurrencyIdentifier="EUR">100,00')
        raw = raw.replace(b'<RowVatAmount AmountCurrencyIdentifier="EUR">-25,50',
                          b'<RowVatAmount AmountCurrencyIdentifier="EUR">25,50')
        raw = raw.replace(b'<RowAmount AmountCurrencyIdentifier="EUR">-125,50',
                          b'<RowAmount AmountCurrencyIdentifier="EUR">125,50')
        data = _one(raw)
        self.assertEqual(data["type"], "CreditNote")
        line = data["lines"][0]
        self.assertEqual((line["quantity"], line["price_unit"], line["line_total"], line["tax_amount"]),
                         (2.0, 50.0, 100.0, 25.5))
        self.assertEqual(data["totals"]["line_extension"], 100.0)
        self.assertReconciles(data)

    def test_missing_unit_price_and_missing_quantity(self):
        first, second = _one("fi30_missing_unit_price.xml")["lines"]
        self.assertEqual((first["quantity"], first["unit_code"], first["price_unit"], first["line_total"]),
                         (3.0, "HUR", 90.0, 270.0))
        self.assertEqual((second["quantity"], second["price_unit"], second["line_total"]), (1.0, 12.0, 12.0))

    def test_credit_note_with_negative_amounts(self):
        # AC-24: positive amounts, type CreditNote, sign carried once.
        data = _one("fi30_credit_note_negative.xml")
        self.assertEqual(data["type"], "CreditNote")
        line = data["lines"][0]
        self.assertEqual((line["quantity"], line["price_unit"], line["line_total"], line["tax_amount"]),
                         (2.0, 50.0, 100.0, 25.5))
        self.assertEqual(data["tax_subtotals"][0]["taxable_amount"], 100.0)
        self.assertEqual(data["tax_subtotals"][0]["tax_amount"], 25.5)
        self.assertEqual(data["totals"], {
            "line_extension": 100.0, "tax_exclusive": 100.0, "tax_inclusive": 125.5, "payable": 125.5,
        })
        self.assertEqual(data["source_meta"]["original_invoice_number"], "10045")
        self.assertTrue(data["source_meta"]["sign_normalised"])

    def test_negative_invoice_type_is_still_a_credit_note(self):
        raw = _edit("fi30_credit_note_negative.xml", ">INV02<", ">INV01<")
        data = _one(raw)
        self.assertEqual(data["type"], "CreditNote")
        self.assertEqual(data["totals"]["payable"], 125.5)

    def test_credit_note_with_positive_amounts(self):
        data = _one("fi30_credit_note_positive.xml")
        self.assertEqual(data["type"], "CreditNote")
        self.assertEqual(data["lines"][0]["line_total"], 40.0)
        # EpiInstructedAmount 0,00 on a credit note -> the total (observed).
        self.assertEqual(data["totals"]["payable"], 50.2)
        self.assertFalse(data["source_meta"]["sign_normalised"])
        self.assertEqual(data["source_meta"]["original_invoice_number"], "10046")

    def test_non_eur(self):
        data = _one("fi30_non_eur.xml")
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(len(data["tax_subtotals"]), 1)
        self.assertEqual(data["tax_subtotals"][0]["taxable_amount"], 200.0)
        self.assertEqual(data["totals"]["payable"], 251.0)
        self.assertEqual(len(data["source_meta"]["warnings"]), 2)

    def test_finvoice_2_01(self):
        data = _one("fi201_invoice.xml")
        self.assertEqual(data["source_meta"]["version"], "2.01")
        self.assertEqual(data["seller"]["name"], "Example Hardware Oy")
        self.assertEqual(data["lines"][0]["unit_code"], "C62")
        self.assertEqual(data["totals"]["payable"], 100.4)

    def test_finvoice_2_0_without_currency_or_vat_breakdown(self):
        data = _one("fi20_invoice.xml")
        self.assertEqual(data["currency"], "EUR")  # default when absent
        # guideline §14: the breakdown is built from the rows.
        rows = sorted((s["tax_percent"], s["taxable_amount"], s["tax_amount"]) for s in data["tax_subtotals"])
        self.assertEqual(rows, [(10.0, 30.0, 3.0), (14.0, 10.0, 1.4), (25.5, 100.0, 25.5)])
        self.assertEqual(data["lines"][2]["unit_code"], "DAY")

    def test_missing_invoice_number(self):
        raw = _edit("fi30_simple_invoice.xml", "<InvoiceNumber>10045</InvoiceNumber>",
                    "<InvoiceNumber></InvoiceNumber>")
        with self.assertRaises(FinvoiceError) as caught:
            _one(raw)
        self.assertEqual((caught.exception.code, caught.exception.field), (MISSING_FIELD, "invoice_number"))

    def test_missing_value_still_hands_over_the_rest_of_the_invoice(self):
        """AC-08: the refusal carries everything the message held, with the
        gap left empty, so the Accountant has data to fix rather than none."""
        raw = _edit("fi30_simple_invoice.xml", "<InvoiceNumber>10045</InvoiceNumber>",
                    "<InvoiceNumber></InvoiceNumber>")
        with self.assertRaises(FinvoiceError) as caught:
            _one(raw)
        data = caught.exception.data
        self.assertIsInstance(data, dict)
        self.assertIsNone(data["invoice_number"])
        self.assertEqual(data["issue_date"], "2026-11-15")
        self.assertEqual(data["seller"]["name"], "Example Supplier Oy")
        self.assertEqual([line["line_total"] for line in data["lines"]], [240.0, 120.0])
        self.assertEqual(data["totals"]["tax_inclusive"], 451.8)
        self.assertEqual(data["source_meta"]["format"], "finvoice")
        self.assertIsNone(FinvoiceError(UNREADABLE).data)

    def test_several_gaps_are_all_named(self):
        raw = _edit("fi30_simple_invoice.xml", "<InvoiceNumber>10045</InvoiceNumber>", "")
        raw = raw.replace(
            b"<SellerOrganisationName>Example Supplier Oy</SellerOrganisationName>", b""
        )
        with self.assertRaises(FinvoiceError) as caught:
            _one(raw)
        self.assertEqual(caught.exception.field, "invoice_number")
        self.assertIn("invoice number", caught.exception.message)
        self.assertIn("seller name", caught.exception.message)
        self.assertIsNone(caught.exception.data["seller"]["name"])

    def test_line_extension_is_the_stated_total_not_the_row_sum(self):
        """AC-08: rows adding up to 350 under a stated 360 must not reconcile.
        line_extension is the document's VAT-exclusive total (guideline §14),
        so the core validator compares the rows with the document."""
        raw = _edit(
            "fi30_simple_invoice.xml",
            '<RowVatExcludedAmount AmountCurrencyIdentifier="EUR">240,00',
            '<RowVatExcludedAmount AmountCurrencyIdentifier="EUR">230,00',
        ).replace(b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">120,00',
                  b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">115,00')
        data = _one(raw)
        self.assertEqual(sum(line["line_total"] for line in data["lines"]), 350.0)
        self.assertEqual(data["totals"]["line_extension"], 360.0)
        self.assertEqual(data["totals"]["tax_exclusive"], 360.0)

    def test_missing_seller_name_and_total(self):
        cases = [
            ("<SellerOrganisationName>Example Supplier Oy</SellerOrganisationName>", "", "seller.name"),
            ('<InvoiceTotalVatIncludedAmount AmountCurrencyIdentifier="EUR">451,80</InvoiceTotalVatIncludedAmount>',
             "", "totals.tax_inclusive"),
            ('<InvoiceDate Format="CCYYMMDD">20261115</InvoiceDate>', "", "issue_date"),
        ]
        for old, new, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(FinvoiceError) as caught:
                    _one(_edit("fi30_simple_invoice.xml", old, new))
                self.assertEqual((caught.exception.code, caught.exception.field), (MISSING_FIELD, field))

    def test_no_rows_with_money(self):
        raw = _raw("fi30_no_vat.xml")
        raw = re.sub(rb"<RowVat\w+[^>]*>[^<]*</RowVat\w+>", b"", raw)
        with self.assertRaises(FinvoiceError) as caught:
            _one(raw)
        self.assertEqual((caught.exception.code, caught.exception.field), (MISSING_FIELD, "lines"))

    def test_unreadable_amount_and_date(self):
        for old, new in (("<RowVatExcludedAmount AmountCurrencyIdentifier=\"EUR\">240,00",
                          "<RowVatExcludedAmount AmountCurrencyIdentifier=\"EUR\">240,00 EUR"),
                         ("20261115</InvoiceDate>", "20261315</InvoiceDate>")):
            with self.subTest(new=new):
                with self.assertRaises(FinvoiceError) as caught:
                    _one(_edit("fi30_simple_invoice.xml", old, new))
                self.assertEqual(caught.exception.code, UNREADABLE)

    def test_quotation_is_not_an_invoice(self):
        raw = _edit("fi30_simple_invoice.xml", ">INV01<", ">QUO01<")
        with self.assertRaises(FinvoiceError) as caught:
            _one(raw)
        self.assertEqual(caught.exception.code, NOT_AN_INVOICE)
        self.assertIn("QUO01", caught.exception.message)

    def test_document_level_discount_becomes_a_line(self):
        # guideline §14: rows total - DiscountsTotalVatExcludedAmount = total without VAT.
        raw = _edit(
            "fi30_no_vat.xml",
            '<InvoiceTotalVatExcludedAmount AmountCurrencyIdentifier="EUR">50,00</InvoiceTotalVatExcludedAmount>',
            '<RowsTotalVatExcludedAmount AmountCurrencyIdentifier="EUR">50,00</RowsTotalVatExcludedAmount>'
            '<DiscountsTotalVatExcludedAmount AmountCurrencyIdentifier="EUR">10,00</DiscountsTotalVatExcludedAmount>'
            '<InvoiceTotalVatExcludedAmount AmountCurrencyIdentifier="EUR">40,00</InvoiceTotalVatExcludedAmount>',
        )
        raw = raw.replace(b">12,75<", b">10,20<").replace(b">62,75<", b">50,20<")
        raw = raw.replace(b'<VatBaseAmount AmountCurrencyIdentifier="EUR">50,00',
                          b'<VatBaseAmount AmountCurrencyIdentifier="EUR">40,00')
        raw = raw.replace(
            b"</VatSpecificationDetails>",
            b"</VatSpecificationDetails><DiscountDetails><FreeText>Kampanja</FreeText>"
            b'<Amount AmountCurrencyIdentifier="EUR">10,00</Amount><VatCategoryCode>S</VatCategoryCode>'
            b"<VatRatePercent>25,5</VatRatePercent></DiscountDetails>",
        )
        # The row keeps its own VAT (12,75) while the document VAT is 10,20.
        raw = raw.replace(b'<RowVatAmount AmountCurrencyIdentifier="EUR">10,20', b'<RowVatAmount AmountCurrencyIdentifier="EUR">12,75')
        self.assertEqual(finvoice.validate_schema(raw), [])
        data = _one(raw)
        discount = data["lines"][-1]
        self.assertEqual((discount["name"], discount["line_total"], discount["tax_percent"]),
                         ("Kampanja", -10.0, 25.5))
        self.assertEqual(data["totals"]["line_extension"], 40.0)
        self.assertReconciles(data)


@tagged("post_install", "-at_install")
class TestFinvoiceValues(_TestCase):
    def test_decimal_comma_and_dot(self):
        self.assertEqual(str(values.parse_decimal("1234,50", "x")), "1234.50")
        self.assertEqual(str(values.parse_decimal("-0.5", "x")), "-0.5")
        self.assertIsNone(values.parse_decimal("  ", "x"))
        with self.assertRaises(FinvoiceError):
            values.parse_decimal("1.234,50", "x")

    def test_dates(self):
        self.assertEqual(values.parse_date("20261231", "d"), "2026-12-31")
        self.assertEqual(values.parse_date("2026-12-31", "d"), "2026-12-31")
        with self.assertRaises(FinvoiceError):
            values.parse_date("20260230", "d")

    def test_business_id(self):
        self.assertTrue(values.looks_like_business_id("0000003-5"))
        self.assertFalse(values.looks_like_business_id("FI00000035"))
        self.assertTrue(values.business_id_checksum_ok("0000003-5"))
        self.assertFalse(values.business_id_checksum_ok("0000004-1"))
        self.assertEqual(values.vat_from_business_id("0000003-5"), "FI00000035")

    def test_iban_and_references(self):
        self.assertTrue(values.iban_is_valid(values.normalise_account("fi21 7990 0012 3456 78")))
        self.assertFalse(values.iban_is_valid("FI8712345600000003"))
        self.assertTrue(values.payment_reference_is_valid("10001235"))
        self.assertFalse(values.payment_reference_is_valid("10001236"))
        self.assertTrue(values.payment_reference_is_valid("RF661234567"))
        self.assertFalse(values.payment_reference_is_valid("RF671234567"))
        self.assertFalse(values.payment_reference_is_valid("SO-7781"))

    def test_unit_codes(self):
        self.assertEqual(values.unit_code("KGM", "kpl"), "KGM")
        self.assertEqual(values.unit_code(None, "kpl"), "C62")
        self.assertEqual(values.unit_code(None, "BX"), "BX")
        self.assertIsNone(values.unit_code(None, "laatikko"))

    def test_tax_categories(self):
        from decimal import Decimal

        cases = [
            ("S", Decimal("25.5"), "S"), ("AE", Decimal(0), "AE"), ("K", Decimal(0), "AE"),
            ("G", Decimal(0), "Z"), ("O", Decimal(0), "E"), (None, Decimal(0), "Z"),
            (None, Decimal(14), "S"), (None, None, None),
        ]
        for code, rate, expected in cases:
            self.assertEqual(finvoice.tax_category(code, rate), expected, (code, rate))


@tagged("post_install", "-at_install")
class TestFinvoiceLibraryIsPure(_TestCase):
    def test_no_erp_import_in_lib(self):
        # ADR-0004: the library imports nothing from the ERP.
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
