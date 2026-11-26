# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The tax list and form show the VAT category, its origin and the reason
(finvoice content T09; AC-03, AC-04, AC-07; SCR-01, SCR-02)."""

import ast
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree

from odoo.tests import Form, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

FILTER = "einvoice_unclassified"


@tagged("post_install", "-at_install")
class TestTaxViews(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("fi")
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.accountant = new_test_user(
            cls.env, login="zz_tax_view_accountant", groups="account.group_account_invoice",
            company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)],
        )

    def _tax(self, xmlid):
        return self.env["account.chart.template"].with_company(self.company).ref(xmlid)

    def _arch(self, xmlid, view_type):
        arch = self.env["account.tax"].get_view(self.env.ref(xmlid).id, view_type)["arch"]
        return etree.fromstring(arch)

    def test_list_shows_category_and_origin_badge(self):
        arch = self._arch("account.view_tax_tree", "list")
        self.assertTrue(arch.xpath("//field[@name='einvoice_vat_category']"))
        badge = arch.xpath("//field[@name='einvoice_vat_category_origin']")
        self.assertTrue(badge)
        self.assertEqual(badge[0].get("widget"), "badge")

    def test_filter_lists_the_unclassified_zero_percent_sales_taxes(self):
        arch = self._arch("account.view_account_tax_search", "search")
        node = arch.xpath(f"//filter[@name='{FILTER}']")
        self.assertTrue(node)
        domain = ast.literal_eval(node[0].get("domain"))
        found = self.env["account.tax"].search(
            domain + [("company_id", "=", self.company.id)])
        self.assertIn(self._tax("tax_dom_sales_goods_0"), found)
        for xmlid in ("tax_eu_sales_goods_0", "tax_eu_sales_service_0",
                      "tax_dom_sales_goods_24", "tax_dom_purchase_0"):
            self.assertNotIn(self._tax(xmlid), found, xmlid)
        self._tax("tax_dom_sales_goods_0").ubl_cii_tax_category_code = "G"
        self._tax("tax_dom_sales_goods_0").invalidate_recordset()
        self.assertNotIn(self._tax("tax_dom_sales_goods_0"), self.env["account.tax"].search(
            domain + [("company_id", "=", self.company.id)]))

    def test_form_has_the_category_reason_and_banner(self):
        arch = self._arch("account.view_tax_form", "form")
        self.assertTrue(arch.xpath("//field[@name='einvoice_vat_category']"))
        self.assertTrue(arch.xpath("//field[@name='einvoice_vat_category_origin']"))
        reason = arch.xpath("//field[@name='einvoice_vat_reason']")
        self.assertTrue(reason)
        self.assertIn("einvoice_vat_reason_default", reason[0].get("options"))
        banners = arch.xpath("//div[contains(@class, 'alert-warning')]")
        self.assertTrue(any("no VAT category" in "".join(b.itertext()) for b in banners))

    def test_form_states(self):
        unclassified = Form(self._tax("tax_dom_sales_goods_0"))
        self.assertEqual(unclassified.einvoice_vat_category_origin, "unclassified")
        default = Form(self._tax("tax_eu_sales_service_0"))
        self.assertEqual((default.einvoice_vat_category, default.einvoice_vat_category_origin),
                         ("AE", "default"))
        self.assertEqual(default.einvoice_vat_reason_default,
                         "Käännetty verovelvollisuus\nReverse charge")
        with Form(self._tax("tax_dom_sales_goods_0")) as form:
            form.ubl_cii_tax_category_code = "G"
            self.assertEqual((form.einvoice_vat_category, form.einvoice_vat_category_origin),
                             ("G", "set"))
            form.einvoice_vat_reason = "Export"
        self.assertEqual(self._tax("tax_dom_sales_goods_0").einvoice_vat_reason, "Export")

    def test_accountant_sees_the_category_read_only(self):
        tax = self._tax("tax_dom_sales_goods_0").with_user(self.accountant)
        self.assertEqual(tax.einvoice_vat_category, "unclassified")
        view = self.env["account.tax"].with_user(self.accountant).get_view(
            self.env.ref("account.view_tax_form").id, "form")
        self.assertIn("einvoice_vat_category", view["arch"])
        self.assertFalse(self.env["account.tax"].with_user(self.accountant).has_access("write"))
