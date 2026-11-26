# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Unit codes in the ERP (send T11; AC-48, AC-50, AC-51; staged migration 04).

``uom.uom.rec20_code`` is the one UN/ECE Rec 20 code of a unit for both
directions, filled for the standard units at install; only accounting
administrators change it or the company's default unit for incoming codes
nobody mapped. Every name and code here is invented.
"""

import importlib.util
import os
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import AccessError, ValidationError  # noqa: E402
from odoo.modules.module import get_manifest  # noqa: E402
from odoo.tests import TransactionCase, new_test_user, tagged  # noqa: E402

from .. import hooks  # noqa: E402
from ..models.uom_uom import UNITS_INVALID_CODE, UNITS_NOT_ALLOWED  # noqa: E402

ADDON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION = os.path.join(ADDON, "migrations", "18.0.1.4.0", "post-fill-unit-codes.py")


@tagged("post_install", "-at_install")
class TestUnitCodes(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.accountant = new_test_user(
            cls.env, login="zz_unit_accountant", groups="account.group_account_invoice"
        )
        # An accounting administrator WITHOUT the settings (system) group: the
        # unit model itself is writable by system administrators only.
        cls.admin = new_test_user(
            cls.env, login="zz_unit_admin", groups="base.group_user,account.group_account_manager"
        )
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.hour = cls.env.ref("uom.product_uom_hour")
        cls.day = cls.env.ref("uom.product_uom_day")
        cls.company = cls.env.company

    def _uom(self, name, code=False):
        return self.env["uom.uom"].create(
            {"name": name, "category_id": self.unit.category_id.id, "uom_type": "bigger", "factor_inv": 1.0, "rec20_code": code}
        )

    # -- install fill (staged 04) ---------------------------------------------

    def test_standard_units_carry_their_code_after_install(self):
        expected = {
            "uom.product_uom_unit": "C62",
            "uom.product_uom_dozen": "DZN",
            "uom.product_uom_hour": "HUR",
            "uom.product_uom_day": "DAY",
            "uom.product_uom_minute": "MIN",
            "uom.product_uom_kgm": "KGM",
            "uom.product_uom_gram": "GRM",
            "uom.product_uom_ton": "TNE",
            "uom.product_uom_meter": "MTR",
            "uom.product_uom_km": "KMT",
            "uom.product_uom_litre": "LTR",
            "uom.product_uom_cubic_meter": "MTQ",
            "uom.product_uom_kwh": "KWH",
        }
        # Odoo 18.0 ships no minute and no kWh unit (19.0 does): they are
        # filled by name when a database has them.
        for xmlid in ("uom.product_uom_minute", "uom.product_uom_kwh"):
            self.assertFalse(self.env.ref(xmlid, raise_if_not_found=False))
            expected.pop(xmlid)
        for xmlid, code in expected.items():
            with self.subTest(unit=xmlid):
                self.assertEqual(self.env.ref(xmlid).rec20_code, code)

    def test_fill_never_overwrites_a_code_already_set(self):
        self.unit.rec20_code = "H87"
        hooks.fill_unit_codes(self.env)
        self.assertEqual(self.unit.rec20_code, "H87")

    def test_fill_by_name_for_units_without_external_id(self):
        month = self.env["uom.uom"].create(
            {"name": "kk", "category_id": self.day.category_id.id, "uom_type": "bigger", "factor_inv": 30.0}
        )
        weeks = self.env["uom.uom"].create(
            {"name": "Weeks", "category_id": self.day.category_id.id, "uom_type": "bigger", "factor_inv": 7.0}
        )
        unknown = self._uom("ZZ crate")
        hooks.fill_unit_codes(self.env)
        self.assertEqual(month.rec20_code, "MON")
        self.assertEqual(weeks.rec20_code, "WEE")
        self.assertFalse(unknown.rec20_code)

    def test_fill_restores_a_cleared_standard_code(self):
        self.hour.rec20_code = False
        hooks.fill_unit_codes(self.env)
        self.assertEqual(self.hour.rec20_code, "HUR")

    def test_upgrade_script_fills_too(self):
        self.assertTrue(os.path.isfile(MIGRATION), MIGRATION)
        spec = importlib.util.spec_from_file_location("post_fill_unit_codes", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.hour.rec20_code = False
        module.migrate(self.env.cr, "18.0.1.3.0")
        self.env.invalidate_all()
        self.assertEqual(self.hour.rec20_code, "HUR")

    def test_hook_is_declared(self):
        manifest = get_manifest("goodahead_einvoice_base")
        self.assertEqual(manifest.get("post_init_hook"), "post_init_hook")

    # -- who may change codes -----------------------------------------

    def test_accountant_cannot_change_a_unit_code(self):
        with self.assertRaises(AccessError) as caught:
            self.hour.with_user(self.accountant).write({"rec20_code": "MIN"})
        self.assertEqual(caught.exception.code, UNITS_NOT_ALLOWED)
        self.assertEqual(self.hour.rec20_code, "HUR")

    def test_accountant_cannot_create_a_unit_with_a_code(self):
        with self.assertRaises(AccessError) as caught:
            self.env["uom.uom"].with_user(self.accountant).create(
                {"name": "ZZ box", "category_id": self.unit.category_id.id, "uom_type": "bigger", "factor_inv": 1.0,
                 "rec20_code": "ZZ1"}
            )
        self.assertEqual(caught.exception.code, UNITS_NOT_ALLOWED)

    def test_accounting_administrator_changes_a_unit_code(self):
        box = self._uom("ZZ box")
        box.with_user(self.admin).write({"rec20_code": "ZZ1"})
        self.assertEqual(box.rec20_code, "ZZ1")

    def test_accountant_cannot_set_the_default_unit(self):
        with self.assertRaises(AccessError) as caught:
            self.company.with_user(self.accountant).write({"einvoice_default_uom_id": self.unit.id})
        self.assertEqual(caught.exception.code, UNITS_NOT_ALLOWED)

    def test_accounting_administrator_sets_the_default_unit(self):
        self.company.with_user(self.admin).write({"einvoice_default_uom_id": self.unit.id})
        self.assertEqual(self.company.einvoice_default_uom_id, self.unit)

    def test_everyone_reads_the_code(self):
        self.assertEqual(self.hour.with_user(self.accountant).rec20_code, "HUR")

    # -- validation -------------------------------------------------------------

    def test_invalid_codes_are_refused(self):
        box = self._uom("ZZ box")
        for code in ("c62", "C", "ABCD", "C-2", "C 6"):
            with self.subTest(code=code):
                with self.assertRaises(ValidationError) as caught:
                    box.write({"rec20_code": code})
                self.assertEqual(caught.exception.code, UNITS_INVALID_CODE)
                self.assertIn("2–3 upper-case letters or digits", str(caught.exception))

    def test_no_code_is_allowed(self):
        box = self._uom("ZZ box", "ZZ1")
        box.write({"rec20_code": False})
        self.assertFalse(box.rec20_code)

    # -- outgoing -------------------------------------------------

    def test_outgoing_code_and_readable_name(self):
        self.assertEqual(self.unit._einvoice_outgoing(), ("C62", "kpl"))
        self.assertEqual(self.hour._einvoice_outgoing(), ("HUR", "h"))

    def test_outgoing_unit_without_code_sends_its_name_cut_to_14(self):
        crate = self._uom("ZZ wooden crate large")
        self.assertEqual(crate._einvoice_outgoing(), (None, "ZZ wooden crat"))

    def test_outgoing_uses_the_changed_code(self):
        crate = self._uom("ZZ crate")
        crate.with_user(self.admin).write({"rec20_code": "HUR"})
        self.assertEqual(crate._einvoice_outgoing(), ("HUR", "h"))

    def test_outgoing_without_unit(self):
        self.assertEqual(self.env["uom.uom"]._einvoice_outgoing(), (None, ""))

    # -- incoming ----------------------------------------------------------

    def test_incoming_code_finds_its_unit(self):
        Uom = self.env["uom.uom"]
        self.assertEqual(Uom._einvoice_unit_for_code("HUR", self.company), (self.hour, None))

    def test_incoming_prefers_the_shipped_unit_when_two_share_a_code(self):
        self._uom("ZZ piece", "C62")
        unit, note = self.env["uom.uom"]._einvoice_unit_for_code("C62", self.company)
        self.assertEqual(unit, self.unit)
        self.assertIsNone(note)

    def test_incoming_reads_a_changed_code_as_that_unit(self):
        crate = self._uom("ZZ crate")
        crate.with_user(self.admin).write({"rec20_code": "ZZC"})
        unit, note = self.env["uom.uom"]._einvoice_unit_for_code("ZZC", self.company)
        self.assertEqual(unit, crate)
        self.assertIsNone(note)

    def test_unmapped_code_falls_back_to_the_default_unit_with_a_note(self):
        self.company.einvoice_default_uom_id = self.unit
        unit, note = self.env["uom.uom"]._einvoice_unit_for_code("ZZX", self.company)
        self.assertEqual(unit, self.unit)
        self.assertIn("ZZX", note)
        self.assertIn(self.unit.name, note)
        self.assertIn("not mapped", note)

    def test_unmapped_code_without_default_gives_no_unit_with_a_note(self):
        self.company.einvoice_default_uom_id = False
        unit, note = self.env["uom.uom"]._einvoice_unit_for_code("ZZX", self.company)
        self.assertFalse(unit)
        self.assertIn("ZZX", note)
        self.assertIn("no unit", note)

    def test_default_unit_is_per_company(self):
        other = self.env["res.company"].create({"name": "ZZ Other Company"})
        self.company.einvoice_default_uom_id = self.unit
        unit, _note = self.env["uom.uom"]._einvoice_unit_for_code("ZZX", other)
        self.assertFalse(unit)

    def test_no_code_is_no_unit_and_no_note(self):
        self.company.einvoice_default_uom_id = self.unit
        self.assertEqual(
            self.env["uom.uom"]._einvoice_unit_for_code(None, self.company), (self.env["uom.uom"], None)
        )
