# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Incoming units through the shared unit codes.

A structured source's lines are read through ``uom.uom.rec20_code`` (the code
the Administrator edits for both directions); a code no unit carries falls
back to the company's default unit for unmapped incoming codes, or to no unit,
with a note on the bill — never a wrong unit silently. AI-read lines keep the
ERP's own unit lookup. Every name and code here is invented.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_einvoice_base.lib.finvoice import parse, to_invoice_data, values

from ..lib.import_source import SourceReader
from .common import ensure_purchase_journal

UNIT_CONTEXT_KEY = "einvoice_unit_codes"

FINVOICE_ROW = """<?xml version="1.0" encoding="UTF-8"?>
<Finvoice Version="3.0">
  <SellerPartyDetails>
    <SellerPartyIdentifier>0000004-3</SellerPartyIdentifier>
    <SellerOrganisationName>ZZ Unit Vendor Oy</SellerOrganisationName>
  </SellerPartyDetails>
  <BuyerPartyDetails>
    <BuyerOrganisationName>ZZ Buyer Oy</BuyerOrganisationName>
  </BuyerPartyDetails>
  <InvoiceDetails>
    <InvoiceTypeCode>INV01</InvoiceTypeCode>
    <InvoiceTypeText>LASKU</InvoiceTypeText>
    <OriginCode>Original</OriginCode>
    <InvoiceNumber>ZZ-UNIT-1</InvoiceNumber>
    <InvoiceDate Format="CCYYMMDD">20261125</InvoiceDate>
    <InvoiceTotalVatExcludedAmount AmountCurrencyIdentifier="EUR">30,00</InvoiceTotalVatExcludedAmount>
    <InvoiceTotalVatAmount AmountCurrencyIdentifier="EUR">0,00</InvoiceTotalVatAmount>
    <InvoiceTotalVatIncludedAmount AmountCurrencyIdentifier="EUR">30,00</InvoiceTotalVatIncludedAmount>
  </InvoiceDetails>
  <InvoiceRow>
    <ArticleName>Boxed thing</ArticleName>
    <InvoicedQuantity QuantityUnitCode="laatikko">3</InvoicedQuantity>
    <UnitPriceAmount AmountCurrencyIdentifier="EUR">10,00</UnitPriceAmount>
    <RowVatRatePercent>0</RowVatRatePercent>
    <RowVatAmount AmountCurrencyIdentifier="EUR">0,00</RowVatAmount>
    <RowVatExcludedAmount AmountCurrencyIdentifier="EUR">30,00</RowVatExcludedAmount>
    <RowAmount AmountCurrencyIdentifier="EUR">30,00</RowAmount>
  </InvoiceRow>
</Finvoice>
"""


@tagged("post_install", "-at_install")
class TestUnitMappingLibrary(TransactionCase):
    """The reader's side: one normalisation, and an unmapped code is kept."""

    def test_reader_unit_code_delegates_to_the_shared_table(self):
        self.assertEqual(values.unit_code(None, "kpl"), "C62")
        self.assertEqual(values.unit_code("H87", None), "C62")
        self.assertEqual(values.unit_code(None, "H87"), "C62")
        self.assertEqual(values.unit_code("KGM", "kpl"), "KGM")
        self.assertEqual(values.unit_code(None, "BX"), "BX")
        self.assertIsNone(values.unit_code(None, "laatikko"))

    def test_unmapped_free_code_is_kept_on_the_line(self):
        data = to_invoice_data(parse(FINVOICE_ROW.encode())[0])
        line = data["lines"][0]
        self.assertIsNone(line["unit_code"])
        self.assertEqual(line["unit_code_unmapped"], "laatikko")

    def test_mapped_code_has_no_unmapped_text(self):
        xml = FINVOICE_ROW.replace('QuantityUnitCode="laatikko"', 'QuantityUnitCode="kpl"')
        line = to_invoice_data(parse(xml.encode())[0])["lines"][0]
        self.assertEqual(line["unit_code"], "C62")
        self.assertIsNone(line["unit_code_unmapped"])


@tagged("post_install", "-at_install")
class TestUnitMappingImport(TransactionCase):
    """The import side: a structured source's units through the shared codes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.journal = ensure_purchase_journal(cls.env, cls.company)
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.box = cls.env["uom.uom"].create(
            {"name": "ZZ box", "category_id": cls.unit.category_id.id, "uom_type": "bigger", "factor_inv": 1.0}
        )
        cls.box.rec20_code = "ZZB"  # the Administrator's own code (superuser here)

    def _extracted(self, unit_code=None, unmapped=None):
        line = {
            "name": "Boxed thing",
            "quantity": 3.0,
            "unit_code": unit_code,
            "price_unit": 10.0,
            "line_total": 30.0,
            "tax_percent": 0.0,
            "tax_amount": 0.0,
            "tax_category": "Z",
        }
        if unmapped is not None:
            line["unit_code_unmapped"] = unmapped
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-UNIT-1",
            "issue_date": "2026-11-25",
            "currency": "EUR",
            "seller": {"name": "ZZ Unit Vendor Oy", "country_code": "FI"},
            "buyer": {"name": self.company.name, "country_code": "FI"},
            "lines": [line],
            "tax_subtotals": [
                {"tax_percent": 0.0, "taxable_amount": 30.0, "tax_amount": 0.0,
                 "tax_category": "Z"},
            ],
            "totals": {"line_extension": 30.0, "tax_exclusive": 30.0,
                       "tax_inclusive": 30.0, "payable": 30.0},
        }

    def _decode(self, extracted, structured=True):
        AccountMove = self.env["account.move"]
        if structured:
            AccountMove = AccountMove.with_context(**{UNIT_CONTEXT_KEY: True})
        xml_bytes = AccountMove._ai_generate_ubl_from_extracted(extracted, self.company)
        move = AccountMove.create({"move_type": "in_invoice", "journal_id": self.journal.id})
        error = AccountMove._ai_decode_ubl_into_move(move, xml_bytes, extracted)
        self.assertFalse(error, error)
        line = move.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        self.assertEqual(len(line), 1)
        return move, line

    def _notes(self, move):
        return " ".join(move.message_ids.mapped("body"))

    def test_changed_code_is_read_as_that_unit(self):
        move, line = self._decode(self._extracted(unit_code="ZZB"))
        self.assertEqual(line.product_uom_id, self.box)
        self.assertNotIn("is not mapped", self._notes(move))

    def test_standard_code_is_read_as_the_standard_unit(self):
        _move, line = self._decode(self._extracted(unit_code="C62"))
        self.assertEqual(line.product_uom_id, self.unit)

    def test_changed_standard_code_wins_over_the_erps_own_table(self):
        # the Administrator moved "HUR" to the box: the import follows the code
        self.env.ref("uom.product_uom_hour").rec20_code = "ZZH"
        self.box.rec20_code = "HUR"
        _move, line = self._decode(self._extracted(unit_code="HUR"))
        self.assertEqual(line.product_uom_id, self.box)

    def test_unmapped_code_falls_back_to_the_default_unit_with_a_note(self):
        self.company.einvoice_default_uom_id = self.box
        move, line = self._decode(self._extracted(unmapped="laatikko"))
        self.assertEqual(line.product_uom_id, self.box)
        self.assertIn("Unit code laatikko is not mapped", self._notes(move))
        self.assertIn("ZZ box used", self._notes(move))

    def test_unmapped_code_without_default_gets_no_unit_with_a_note(self):
        self.company.einvoice_default_uom_id = False
        move, line = self._decode(self._extracted(unmapped="laatikko"))
        self.assertFalse(line.product_uom_id)
        self.assertIn("Unit code laatikko is not mapped", self._notes(move))

    def test_valid_code_nobody_carries_falls_back_too(self):
        self.company.einvoice_default_uom_id = False
        move, line = self._decode(self._extracted(unit_code="XZZ"))
        self.assertFalse(line.product_uom_id)
        self.assertIn("Unit code XZZ is not mapped", self._notes(move))

    def test_ai_read_lines_keep_the_erps_own_lookup(self):
        self.company.einvoice_default_uom_id = self.box
        move, line = self._decode(self._extracted(unit_code="C62"), structured=False)
        self.assertEqual(line.product_uom_id, self.unit)
        move, line = self._decode(self._extracted(unit_code="ZZB"), structured=False)
        self.assertNotEqual(line.product_uom_id, self.box)
        self.assertNotIn("is not mapped", self._notes(move))

    def test_structured_reader_decodes_with_the_shared_codes(self):
        record = self.env["goodahead.invoice.import"].new({"company_id": self.company.id})
        structured = SourceReader(key="zz_structured", priority=50, structured=True)
        with patch.object(type(record), "_import_reader_spec", return_value=structured):
            self.assertEqual(record._import_decode_context(), {UNIT_CONTEXT_KEY: True})
        ai = SourceReader(key="zz_ai", priority=60, structured=False)
        with patch.object(type(record), "_import_reader_spec", return_value=ai):
            self.assertEqual(record._import_decode_context(), {})
        with patch.object(type(record), "_import_reader_spec", return_value=None):
            self.assertEqual(record._import_decode_context(), {})
