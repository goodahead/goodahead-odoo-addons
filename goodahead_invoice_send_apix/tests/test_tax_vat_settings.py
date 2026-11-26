# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The VAT settings of a tax (finvoice content T08; AC-03, AC-04, AC-06,
AC-07, AC-26), on the Finnish chart's own taxes. Every name is invented."""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestTaxVatSettings(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("fi")
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.company_b = cls.setup_other_company(name="ZZ Second Company Oy")["company"]
        cls.admin = new_test_user(
            cls.env, login="zz_vat_admin", groups="account.group_account_manager",
            company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)],
        )
        cls.accountant = new_test_user(
            cls.env, login="zz_vat_accountant", groups="account.group_account_invoice",
            company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)],
        )

    def _tax(self, xmlid, company=None):
        company = company or self.company
        return self.env["account.chart.template"].with_company(company).ref(xmlid)

    def _new_sale_tax(self, amount, **vals):
        return self.env["account.tax"].create({
            "name": f"ZZ {amount} % {len(vals)}",
            "amount": amount,
            "type_tax_use": "sale",
            "company_id": self.company.id,
            **vals,
        })

    # ------------------------------------------------------------------
    # AC-04 — defaults without touching taxes
    # ------------------------------------------------------------------

    def test_finnish_chart_defaults(self):
        cases = {
            "tax_eu_sales_goods_0": ("K", "default"),
            "tax_eu_sales_service_0": ("AE", "default"),
            "tax_construct_sales_0": ("AE", "default"),
            "tax_dom_sales_goods_0": ("unclassified", "unclassified"),
            "tax_dom_sales_goods_24": ("S", "default"),
        }
        for xmlid, expected in cases.items():
            with self.subTest(tax=xmlid):
                tax = self._tax(xmlid)
                self.assertFalse(tax.ubl_cii_tax_category_code)
                self.assertEqual((tax.einvoice_vat_category, tax.einvoice_vat_category_origin),
                                 expected)

    def test_report_formulas(self):
        country, formulas = self._tax("tax_eu_sales_goods_0")._einvoice_report_formulas()
        self.assertEqual(country, "FI")
        self.assertEqual(formulas, {"fi_311"})

    def test_reason_default_wording_while_empty(self):
        tax = self._tax("tax_eu_sales_service_0")
        self.assertFalse(tax.einvoice_vat_reason)
        self.assertEqual(tax.einvoice_vat_reason_default,
                         "Käännetty verovelvollisuus\nReverse charge")
        self.assertEqual(tax._einvoice_vat(), {
            "category": "AE", "origin": "default",
            "reason_lines": ["Käännetty verovelvollisuus", "Reverse charge"],
            "exemption_code": "VATEX-EU-AE",
        })
        self.assertEqual(self._tax("tax_dom_sales_goods_24").einvoice_vat_reason_default, "")

    def test_unclassified_states_nothing(self):
        self.assertEqual(self._tax("tax_dom_sales_goods_0")._einvoice_vat(), {
            "category": None, "origin": "unclassified", "reason_lines": [],
            "exemption_code": None,
        })

    def test_search_unclassified(self):
        Tax = self.env["account.tax"]
        unclassified = Tax.search([("einvoice_vat_category", "=", "unclassified"),
                                   ("company_id", "=", self.company.id)])
        self.assertIn(self._tax("tax_dom_sales_goods_0"), unclassified)
        self.assertNotIn(self._tax("tax_eu_sales_goods_0"), unclassified)
        reverse = Tax.search([("einvoice_vat_category", "in", ["AE"]),
                              ("company_id", "=", self.company.id)])
        self.assertIn(self._tax("tax_eu_sales_service_0"), reverse)

    def test_install_wrote_no_category_or_reason(self):
        self.env.cr.execute(
            "SELECT count(*) FROM account_tax WHERE company_id = %s AND "
            "(ubl_cii_tax_category_code IS NOT NULL OR einvoice_vat_reason IS NOT NULL)",
            [self.company.id],
        )
        self.assertEqual(self.env.cr.fetchone()[0], 0)

    # ------------------------------------------------------------------
    # AC-03 — set explicitly, tracked
    # ------------------------------------------------------------------

    def test_administrator_sets_category_code_and_reason(self):
        self.env.cr.flush()  # end the chart's pre-commit: only later writes are tracked
        tax = self._tax("tax_dom_sales_goods_0").with_user(self.admin).with_context(
            tracking_disable=False, mail_notrack=False)
        tax.write({
            "ubl_cii_tax_category_code": "G",
            "ubl_cii_tax_exemption_reason_code": "VATEX-EU-G",
            "einvoice_vat_reason": "Export outside the EU",
        })
        tax.env.flush_all()
        tax.env.cr.flush()  # tracking is written at pre-commit
        tax.invalidate_recordset(["message_ids"])
        self.assertEqual((tax.einvoice_vat_category, tax.einvoice_vat_category_origin),
                         ("G", "set"))
        self.assertEqual(tax._einvoice_vat()["reason_lines"], ["Export outside the EU"])
        tracked = tax.sudo().message_ids.tracking_value_ids.mapped("field_id.name")
        for name in ("ubl_cii_tax_category_code", "ubl_cii_tax_exemption_reason_code",
                     "einvoice_vat_reason"):
            self.assertIn(name, tracked)
        self.assertEqual(tax.sudo().message_ids[:1].author_id, self.admin.partner_id)

    def test_clearing_brings_back_the_default(self):
        tax = self._tax("tax_eu_sales_goods_0")
        tax.ubl_cii_tax_category_code = "AE"
        self.assertEqual(tax.einvoice_vat_category, "AE")
        tax.ubl_cii_tax_category_code = False
        self.assertEqual((tax.einvoice_vat_category, tax.einvoice_vat_category_origin),
                         ("K", "default"))

    def test_set_exemption_code_wins_over_the_default_code(self):
        tax = self._tax("tax_dom_sales_goods_0")
        tax.write({"ubl_cii_tax_category_code": "E",
                   "ubl_cii_tax_exemption_reason_code": "VATEX-EU-132-1A"})
        self.assertEqual(tax._einvoice_vat()["exemption_code"], "VATEX-EU-132-1A")
        self.assertEqual(tax._einvoice_vat()["reason_lines"],
                         ["Arvonlisäverosta vapautettu myynti", "VAT exempt supply"])

    # ------------------------------------------------------------------
    # AC-06 — inconsistent settings refused
    # ------------------------------------------------------------------

    def test_no_vat_category_on_a_taxed_rate(self):
        with self.assertRaisesRegex(ValidationError, "above zero"):
            self._tax("tax_dom_sales_goods_24").ubl_cii_tax_category_code = "AE"

    def test_standard_on_zero(self):
        with self.assertRaisesRegex(ValidationError, "0 %"):
            self._tax("tax_dom_sales_goods_0").ubl_cii_tax_category_code = "S"

    def test_rate_change_against_a_set_category(self):
        tax = self._tax("tax_eu_sales_service_0")
        tax.ubl_cii_tax_category_code = "AE"
        with self.assertRaisesRegex(ValidationError, "above zero"):
            tax.amount = 24.0

    def test_code_of_another_category(self):
        tax = self._tax("tax_eu_sales_service_0")
        with self.assertRaisesRegex(ValidationError, "VATEX-EU-IC"):
            tax.write({"ubl_cii_tax_category_code": "AE",
                       "ubl_cii_tax_exemption_reason_code": "VATEX-EU-IC"})

    def test_reason_too_long(self):
        tax = self._tax("tax_eu_sales_service_0")
        with self.assertRaisesRegex(ValidationError, "3 lines of 70 characters"):
            tax.einvoice_vat_reason = "x" * 71
        with self.assertRaisesRegex(ValidationError, "3 lines of 70 characters"):
            tax.einvoice_vat_reason = "a\nb\nc\nd"

    def test_purchase_taxes_are_not_checked(self):
        tax = self.env["account.tax"].create({
            "name": "ZZ purchase 21", "amount": 21.0, "type_tax_use": "purchase",
            "company_id": self.company.id, "ubl_cii_tax_category_code": "AE",
        })
        self.assertEqual(tax.ubl_cii_tax_category_code, "AE")

    def test_install_mode_skips_the_check(self):
        tax = self._new_sale_tax(24.0)
        tax.with_context(install_mode=True).ubl_cii_tax_category_code = "AE"
        self.assertEqual(tax.ubl_cii_tax_category_code, "AE")

    def test_localisation_export_article_code_fits_export(self):
        # A localisation ships export taxes with the directive's art. 146 code.
        tax = self._new_sale_tax(0.0, ubl_cii_tax_category_code="G",
                                 ubl_cii_tax_exemption_reason_code="VATEX-EU-146-1E")
        self.assertEqual(tax._einvoice_vat()["exemption_code"], "VATEX-EU-146-1E")

    # ------------------------------------------------------------------
    # AC-07 — Administrator setting
    # ------------------------------------------------------------------

    def test_accountant_cannot_change_vat_settings(self):
        tax = self._tax("tax_dom_sales_goods_0").with_user(self.accountant)
        self.assertEqual(tax.einvoice_vat_category, "unclassified")
        for vals in ({"ubl_cii_tax_category_code": "G"},
                     {"ubl_cii_tax_exemption_reason_code": "VATEX-EU-G"},
                     {"einvoice_vat_reason": "Export"}):
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                tax.write(vals)

    # ------------------------------------------------------------------
    # AC-26 — each company's own taxes
    # ------------------------------------------------------------------

    def test_companies_keep_their_own_settings(self):
        tax_a = self._tax("tax_dom_sales_goods_0")
        tax_b = self._tax("tax_dom_sales_goods_0", self.company_b)
        self.assertNotEqual(tax_a, tax_b)
        tax_a.ubl_cii_tax_category_code = "G"
        tax_b.write({"ubl_cii_tax_category_code": "E", "einvoice_vat_reason": "ZZ exempt"})
        self.assertEqual(tax_a._einvoice_vat()["category"], "G")
        self.assertEqual(tax_b._einvoice_vat()["reason_lines"], ["ZZ exempt"])

    def test_administrator_of_one_company_does_not_see_the_other(self):
        Tax = self.env["account.tax"].with_user(self.admin)
        self.assertFalse(Tax.search([("company_id", "=", self.company_b.id)]))
        tax_b = self._tax("tax_dom_sales_goods_0", self.company_b)
        with self.assertRaises(AccessError):
            tax_b.with_user(self.admin).write({"einvoice_vat_reason": "ZZ"})
