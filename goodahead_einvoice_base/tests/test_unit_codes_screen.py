# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The unit codes screen.

Administrators edit the UN/ECE Rec 20 code of any unit and the default unit for
unmapped incoming codes; Accountants see the same list read-only. Every name
and code here is invented.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree  # noqa: E402
from odoo.tests import Form, TransactionCase, new_test_user, tagged  # noqa: E402

from ..models.uom_uom import (  # noqa: E402
    UNITS_INVALID_CODE,
    UNITS_NOT_ALLOWED,
    UnitsAccessError,
    UnitsValidationError,
)

MODULE = "goodahead_einvoice_base"


@tagged("post_install", "-at_install")
class TestUnitCodesScreen(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.accountant = new_test_user(
            cls.env, login="zz_scr15_accountant", groups="account.group_account_invoice"
        )
        cls.admin = new_test_user(
            cls.env,
            login="zz_scr15_admin",
            groups="base.group_user,account.group_account_manager",
        )
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.batch = cls.env["uom.uom"].create(
            {"name": "ZZ erä", "category_id": cls.unit.category_id.id, "uom_type": "bigger", "factor_inv": 1.0}
        )
        cls.action = cls.env.ref(f"{MODULE}.action_einvoice_unit_codes")
        cls.list_view = cls.env.ref(f"{MODULE}.uom_rec20_view_list")

    # -- helpers ---------------------------------------------------------------

    def _list_arch(self, user):
        views = self.env["uom.uom"].with_user(user).get_views([(self.list_view.id, "list")])
        return etree.fromstring(views["views"]["list"]["arch"])

    def _search_arch(self, user):
        search_view = self.action.search_view_id
        views = self.env["uom.uom"].with_user(user).get_views([(search_view.id, "search")])
        return etree.fromstring(views["views"]["search"]["arch"])

    # -- menu and window -------------------------------------------------------

    def test_menu_under_accounting_configuration(self):
        menu = self.env.ref(f"{MODULE}.menu_einvoice_unit_codes")
        self.assertEqual(menu.name, "Unit codes")
        self.assertEqual(menu.action, self.action)
        self.assertEqual(menu.parent_id, self.env.ref(f"{MODULE}.menu_einvoice_config"))
        self.assertEqual(menu.parent_id.name, "E-invoicing")
        self.assertEqual(
            menu.parent_id.parent_id, self.env.ref("account.menu_finance_configuration")
        )
        visible = self.env["ir.ui.menu"].with_user(self.admin)._visible_menu_ids()
        self.assertIn(menu.id, visible)

    def test_window_on_units_with_its_own_list(self):
        self.assertEqual(self.action.res_model, "uom.uom")
        self.assertEqual(self.action.view_mode.split(",")[0], "list")
        self.assertEqual(self.action.view_id, self.list_view)
        self.assertTrue(self.action.search_view_id)

    def test_accountant_can_open_the_window(self):
        # reached from the readiness item "Unit codes"
        action = self.env["ir.actions.act_window"].with_user(self.accountant)._for_xml_id(
            f"{MODULE}.action_einvoice_unit_codes"
        )
        self.assertEqual(action["res_model"], "uom.uom")
        units = self.env["uom.uom"].with_user(self.accountant).search_read(
            [("id", "=", self.unit.id)], ["name", "rec20_code", "einvoice_readable_name"]
        )
        self.assertEqual(units[0]["rec20_code"], "C62")
        self.assertEqual(units[0]["einvoice_readable_name"], "kpl")

    # -- default state: Administrator -----------------------------------------

    def test_administrator_gets_an_editable_list(self):
        arch = self._list_arch(self.admin)
        self.assertEqual(arch.get("editable"), "bottom")
        self.assertNotEqual(arch.get("edit"), "0")
        self.assertEqual(arch.get("create"), "0")
        self.assertEqual(arch.get("delete"), "0")
        columns = [field.get("name") for field in arch.iter("field")]
        for name in ("name", "category_id", "rec20_code", "einvoice_readable_name"):
            self.assertIn(name, columns)
        code = next(f for f in arch.iter("field") if f.get("name") == "rec20_code")
        self.assertNotIn(code.get("readonly"), ("1", "True", "true"))
        unit_name = next(f for f in arch.iter("field") if f.get("name") == "name")
        self.assertEqual(unit_name.get("readonly"), "1")

    def test_administrator_changes_a_code(self):
        self.batch.with_user(self.admin).write({"rec20_code": "LO"})
        self.assertEqual(self.batch.rec20_code, "LO")
        # not in the shipped table: the unit's own name is what goes out
        self.assertEqual(self.batch.einvoice_readable_name, "ZZ erä")
        self.assertEqual(self.batch._einvoice_outgoing(), ("LO", "ZZ erä"))

    def test_administrator_sets_the_default_unit_above_the_list(self):
        arch = self._list_arch(self.admin)
        buttons = arch.findall("header/button")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get("name"), "action_einvoice_default_unit")
        self.assertEqual(buttons[0].get("display"), "always")
        self.assertIn("Default unit for unmapped incoming codes", buttons[0].get("string"))

        action = self.env["uom.uom"].with_user(self.admin).action_einvoice_default_unit()
        self.assertEqual(action["res_model"], "res.company")
        self.assertEqual(action["res_id"], self.env.company.id)
        self.assertEqual(action["target"], "new")
        view = self.env["ir.ui.view"].browse(action["views"][0][0])
        self.assertEqual(view, self.env.ref(f"{MODULE}.res_company_view_form_einvoice_default_uom"))

        company = self.env.company.with_user(self.admin)
        with Form(company, view=view) as form:
            self.assertEqual(form.einvoice_default_uom_id, self.env["uom.uom"])
            form.einvoice_default_uom_id = self.batch
        self.assertEqual(self.env.company.einvoice_default_uom_id, self.batch)
        unit, note = self.env["uom.uom"]._einvoice_unit_for_code("ZZX", self.env.company)
        self.assertEqual(unit, self.batch)
        self.assertIn("ZZ erä", note)

    def test_default_unit_button_works_as_the_web_client_calls_it(self):
        """A list header button is called with the selected ids (none here),
        as the web client's call_button does."""
        from odoo.api import call_kw  # 18.0 (19.0: odoo.service.model)

        Uom = self.env["uom.uom"].with_user(self.admin)
        action = call_kw(Uom, "action_einvoice_default_unit", [[]], {})
        self.assertEqual(action["res_model"], "res.company")
        self.assertEqual(action["res_id"], self.env.company.id)

    # -- without code -----------------------------------------------------------

    def test_units_without_code_are_highlighted_and_filtered(self):
        arch = self._list_arch(self.admin)
        self.assertEqual(arch.get("decoration-warning"), "not rec20_code")
        search = self._search_arch(self.admin)
        names = [f.get("name") for f in search.iter("filter")]
        self.assertIn("without_code", names)
        found = self.env["uom.uom"].search([("rec20_code", "=", False)])
        self.assertIn(self.batch, found)
        self.assertNotIn(self.unit, found)

    # -- Accountant: read-only --------------------------------------------------

    def test_accountant_gets_a_read_only_list(self):
        arch = self._list_arch(self.accountant)
        self.assertIsNone(arch.get("editable"))
        self.assertEqual(arch.get("edit"), "0")
        self.assertEqual(arch.get("create"), "0")
        self.assertEqual(arch.findall("header/button"), [])

    def test_accountant_cannot_change_a_code_or_the_default_unit(self):
        with self.assertRaises(UnitsAccessError) as caught:
            self.batch.with_user(self.accountant).write({"rec20_code": "LO"})
        self.assertEqual(caught.exception.code, UNITS_NOT_ALLOWED)
        self.assertFalse(self.batch.rec20_code)
        with self.assertRaises(UnitsAccessError):
            self.env.company.with_user(self.accountant).write(
                {"einvoice_default_uom_id": self.batch.id}
            )
        self.assertFalse(self.env.company.einvoice_default_uom_id)

    # -- validation ---------------------------------------------------------------

    def test_invalid_code_is_refused_with_the_screen_message(self):
        for code in ("kg", "ABCD", "K", "K-G"):
            with self.subTest(code=code):
                with self.assertRaises(UnitsValidationError) as caught, self.env.cr.savepoint():
                    self.batch.with_user(self.admin).write({"rec20_code": code})
                self.assertEqual(caught.exception.code, UNITS_INVALID_CODE)
                self.assertEqual(
                    str(caught.exception.args[0]),
                    "A UN/ECE Rec 20 code has 2–3 upper-case letters or digits.",
                )
        self.batch.invalidate_recordset()
        self.assertFalse(self.batch.rec20_code)
