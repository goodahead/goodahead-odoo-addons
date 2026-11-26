# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Where the APIX connection menu lives: Accounting ▸ Configuration ▸
E-invoicing, accounting administrators only. Every login is invented."""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the client tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import TransactionCase, new_test_user, tagged  # noqa: E402


@tagged("post_install", "-at_install")
class TestApixConnectionMenu(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.menu = cls.env.ref("goodahead_apix.menu_goodahead_apix_connection")
        cls.config = cls.env.ref("goodahead_einvoice_base.menu_einvoice_config")
        cls.billing = new_test_user(
            cls.env, login="zz_apix_menu_billing",
            groups="base.group_user,account.group_account_invoice",
        )
        cls.admin = new_test_user(
            cls.env, login="zz_apix_menu_admin",
            groups="base.group_user,account.group_account_manager",
        )

    def _visible(self, user):
        return self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()

    def test_menu_under_einvoicing_configuration(self):
        self.assertEqual(self.menu.parent_id, self.config)
        self.assertEqual(self.config.parent_id, self.env.ref("account.menu_finance_configuration"))
        self.assertEqual(self.menu.action, self.env.ref("goodahead_apix.goodahead_apix_connection_action"))
        self.assertEqual(self.menu.group_ids, self.env.ref("account.group_account_manager"))

    def test_menu_before_unit_codes(self):
        unit_codes = self.env.ref("goodahead_einvoice_base.menu_einvoice_unit_codes")
        self.assertLess(self.menu.sequence, unit_codes.sequence)

    def test_admin_sees_the_menu(self):
        self.assertIn(self.menu.id, self._visible(self.admin))

    def test_billing_user_does_not_see_the_menu(self):
        self.assertNotIn(self.menu.id, self._visible(self.billing))
