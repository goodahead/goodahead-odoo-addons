# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""VAT categories, reasons and the standard codes in the invoice values
(finvoice content T11; AC-01, AC-02, AC-05, AC-20, AC-26, AC-28). Every
message built here is checked against the Finvoice 3.0 schema."""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.goodahead_einvoice_base.lib import finvoice
from odoo.addons.goodahead_einvoice_base.lib.finvoice import vat

from .common import SendApixCommon

AE_LINES = list(vat.DEFAULT_REASONS["AE"])


@tagged("post_install", "-at_install")
class TestFinvoiceValuesVat(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.s24 = cls._make_tax(cls.company, 24.0, "S")
        cls.s14 = cls._make_tax(cls.company, 14.0, "S")
        cls.ae0 = cls._make_tax(cls.company, 0.0, "AE")
        cls.k0 = cls._make_tax(cls.company, 0.0, "K")
        cls.g0 = cls._make_tax(cls.company, 0.0, "G")
        cls.unclassified0 = cls._make_tax(cls.company, 0.0)
        cls.e0_a = cls._make_tax(cls.company, 0.0, "E", reason="Exempt A")
        cls.e0_b = cls._make_tax(cls.company, 0.0, "E", reason="Exempt B",
                                 code="VATEX-EU-132-1B")
        cls.eu_customer = cls._make_customer(
            "ZZ EU Customer AB", "003700000027", vat="SE556677889901",
            country_id=cls.env.ref("base.se").id, street="Exempelgatan 3", zip="11122",
            city="Stockholm")

    def _line(self, price, *taxes, quantity=1):
        return {"name": "ZZ line", "quantity": quantity, "price_unit": price,
                "tax_ids": [(6, 0, [tax.id for tax in taxes])]}

    def _euro(self):
        """AC-20 speaks of euro invoices; the test chart's currency may differ."""
        euro = self.env.ref("base.EUR")
        if self.company.currency_id == euro:
            return euro
        return self.setup_other_currency("EUR")

    def _values(self, move):
        values = move._apix_finvoice_values()
        self.assertEqual(finvoice.validate_schema(finvoice.write(values)), [])
        return values

    def _sums(self, move, values):
        self.assertAlmostEqual(sum(t["base"] for t in values["taxes"]), move.amount_untaxed)
        self.assertAlmostEqual(sum(t["amount"] for t in values["taxes"]), move.amount_tax)

    # -- AC-01 ---------------------------------------------------------------------

    def test_reverse_charge(self):
        move = self._make_invoice(self.eu_customer, [self._line(120.0, self.ae0, quantity=5)])
        values = self._values(move)
        self.assertEqual(values["taxes"], [{
            "percent": 0.0, "base": 600.0, "amount": 0.0, "code": "AE",
            "reason_lines": AE_LINES, "exemption_code": "VATEX-EU-AE",
        }])
        self.assertEqual([line["tax_code"] for line in values["lines"]], ["AE"])
        self.assertEqual(values["buyer"]["vat"], "SE556677889901")
        root = finvoice.parse(finvoice.write(values))[0].element
        self.assertEqual(root.findtext("InvoiceDetails/VatSpecificationDetails/VatCode"), "AE")

    def test_administrators_reason_and_code_are_sent(self):
        self.ae0.write({"einvoice_vat_reason": "Reverse charge, VAT Act 8 c §"})
        move = self._make_invoice(self.eu_customer, [self._line(100.0, self.ae0)])
        entry = self._values(move)["taxes"][0]
        self.assertEqual(entry["reason_lines"], ["Reverse charge, VAT Act 8 c §"])

    # -- AC-02 ---------------------------------------------------------------------

    def test_several_categories_at_the_same_rate(self):
        move = self._make_invoice(lines=[
            self._line(100.0, self.s24), self._line(120.0, self.k0), self._line(80.0, self.g0),
        ])
        values = self._values(move)
        self.assertEqual([(t["percent"], t["code"], t["base"]) for t in values["taxes"]],
                         [(24.0, "S", 100.0), (0.0, "K", 120.0), (0.0, "G", 80.0)])
        self.assertEqual(values["taxes"][0]["reason_lines"], [])
        self.assertEqual(values["taxes"][1]["exemption_code"], "VATEX-EU-IC")
        self.assertEqual(values["taxes"][2]["exemption_code"], "VATEX-EU-G")
        self._sums(move, values)

    # -- AC-05 ---------------------------------------------------------------------

    def test_unclassified_zero_tax_states_nothing(self):
        move = self._make_invoice(lines=[self._line(100.0, self.s24),
                                         self._line(50.0, self.unclassified0)])
        values = self._values(move)
        unclassified = values["taxes"][1]
        self.assertEqual((unclassified["percent"], unclassified["code"],
                          unclassified["reason_lines"], unclassified["exemption_code"]),
                         (0.0, None, [], None))
        self.assertIsNone(values["lines"][1]["tax_code"])
        self._sums(move, values)

    # -- AC-28 ---------------------------------------------------------------------

    def test_lines_without_one_clear_category(self):
        move = self._make_invoice(lines=[
            self._line(10.0),                          # no tax
            self._line(100.0, self.s24, self.ae0),    # mixed: the 24 % tax's S
            self._line(20.0, self.k0, self.g0),       # tie at 0 %: none
            self._line(30.0, self.k0),
        ])
        values = self._values(move)
        self.assertEqual([line["tax_code"] for line in values["lines"]], [None, "S", None, "K"])
        self.assertEqual([(t["percent"], t["code"], t["base"]) for t in values["taxes"]],
                         [(0.0, None, 30.0), (24.0, "S", 100.0), (0.0, "K", 30.0)])
        self._sums(move, values)

    def test_shared_entry_takes_the_reason_of_the_larger_base(self):
        move = self._make_invoice(lines=[self._line(100.0, self.e0_a),
                                         self._line(300.0, self.e0_b)])
        entry = self._values(move)["taxes"][0]
        self.assertEqual((entry["code"], entry["base"]), ("E", 400.0))
        self.assertEqual((entry["reason_lines"], entry["exemption_code"]),
                         (["Exempt B"], "VATEX-EU-132-1B"))

    def test_combined_taxes_share_their_vat_by_entry(self):
        move = self._make_invoice(lines=[self._line(100.0, self.s24),
                                         self._line(300.0, self.s24, self.s14)])
        values = self._values(move)
        self.assertEqual([(t["percent"], t["base"]) for t in values["taxes"]],
                         [(24.0, 100.0), (38.0, 300.0)])
        self._sums(move, values)

    # -- AC-20 ---------------------------------------------------------------------

    def test_standard_codes_of_an_invoice(self):
        move = self._make_invoice(lines=[self._line(100.0, self.s24), self._line(50.0, self.k0)],
                                  currency_id=self._euro().id)
        values = self._values(move)
        self.assertEqual(values["invoice"]["type_code_un"], "380")
        self.assertEqual(values["epi"]["payment_means_code"], "58")
        self.assertAlmostEqual(values["invoice"]["totals"]["rows"],
                               sum(line["untaxed"] for line in values["lines"]))
        self.assertAlmostEqual(values["invoice"]["totals"]["rows"], move.amount_untaxed)

    def test_standard_codes_of_a_credit_note(self):
        move = self._make_invoice(lines=[self._line(100.0, self.s24)], move_type="out_refund",
                                  currency_id=self._euro().id)
        values = self._values(move)
        self.assertEqual(values["invoice"]["type_code_un"], "381")
        self.assertEqual(values["epi"]["payment_means_code"], "58")

    def test_another_currency_states_a_plain_credit_transfer(self):
        sek = self.setup_other_currency("SEK")
        move = self._make_invoice(lines=[self._line(100.0, self.s24)], currency_id=sek.id)
        self.assertEqual(self._values(move)["epi"]["payment_means_code"], "30")

    # -- AC-26 ---------------------------------------------------------------------

    def test_second_company_uses_its_own_taxes(self):
        b_zero = self._make_tax(self.company_b, 0.0, "G", reason="ZZ second company export")
        move = self._make_invoice(lines=[self._line(100.0, b_zero)], company=self.company_b)
        values = move.with_user(self.accountant).with_company(self.company)._apix_finvoice_values()
        self.assertEqual(finvoice.validate_schema(finvoice.write(values)), [])
        self.assertEqual(values["taxes"][0]["code"], "G")
        self.assertEqual(values["taxes"][0]["reason_lines"], ["ZZ second company export"])

    # -- review F3: settings the e-invoice cannot hold ----------------------

    def test_a_code_over_20_characters_is_refused_on_a_sales_tax(self):
        with self.assertRaisesRegex(ValidationError, "20"):
            self._make_tax(self.company, 0.0, "E", code="VATEX-FR-298SEXDECIESA")

    def test_unchecked_settings_the_schema_cannot_take_are_left_out_with_a_warning(self):
        """A tax written past the check (install mode) still sends a valid
        message: the code is left out, at most 3 reason lines of 70 go, and
        readiness names the tax."""
        reason = "\n".join(["Reason one", "x" * 75, "Reason two", "Reason three", "Reason four"])
        tax = self.env["account.tax"].with_context(install_mode=True).create({
            "name": "ZZ 0 % E unchecked", "amount": 0.0, "amount_type": "percent",
            "type_tax_use": "sale", "company_id": self.company.id,
            "ubl_cii_tax_category_code": "E",
            "ubl_cii_tax_exemption_reason_code": "VATEX-FR-298SEXDECIESA",
            "einvoice_vat_reason": reason,
        })
        move = self._make_invoice(lines=[self._line(100.0, tax)])
        entry = self._values(move)["taxes"][0]
        self.assertEqual(entry["code"], "E")
        self.assertIsNone(entry["exemption_code"])
        self.assertEqual(entry["reason_lines"], ["Reason one", "Reason two", "Reason three"])
        items = {item["key"]: item for item in move._apix_readiness("einvoice")["items"]}
        self.assertIn("vat_settings_left_out", items)
        item = items["vat_settings_left_out"]
        self.assertEqual((item["severity"], item["fix_at"]), ("warn", "taxes"))
        self.assertIn("ZZ 0 % E unchecked", item["message"])

    def test_fitting_settings_give_no_warning(self):
        move = self._make_invoice(lines=[self._line(100.0, self.e0_b)])
        keys = [item["key"] for item in move._apix_readiness("einvoice")["items"]]
        self.assertNotIn("vat_settings_left_out", keys)
