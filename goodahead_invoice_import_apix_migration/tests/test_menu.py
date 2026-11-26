# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Where the APIX migration tool menu lives: Accounting ▸ Configuration ▸
E-invoicing, after APIX connection and before Unit codes, accounting
administrators only. Every login is invented."""

from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestApixMigrationMenu(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.menu = cls.env.ref("goodahead_invoice_import_apix_migration.menu_apix_migration")
        cls.config = cls.env.ref("goodahead_einvoice_base.menu_einvoice_config")
        cls.billing = new_test_user(
            cls.env, login="zz_apix_mig_menu_billing",
            groups="base.group_user,account.group_account_invoice",
        )
        cls.admin = new_test_user(
            cls.env, login="zz_apix_mig_menu_admin",
            groups="base.group_user,account.group_account_manager",
        )

    def _visible(self, user):
        return self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()

    def test_menu_under_einvoicing_configuration(self):
        self.assertEqual(self.menu.parent_id, self.config)
        self.assertEqual(self.menu.action, self.env.ref(
            "goodahead_invoice_import_apix_migration.action_apix_migration"))
        self.assertEqual(self.menu.groups_id, self.env.ref("account.group_account_manager"))

    def test_menu_order_connection_tool_unit_codes(self):
        connection = self.env.ref("goodahead_apix.menu_goodahead_apix_connection")
        unit_codes = self.env.ref("goodahead_einvoice_base.menu_einvoice_unit_codes")
        children = self.config.child_id.sorted(lambda menu: (menu.sequence, menu.id))
        ordered = children.filtered(lambda menu: menu in (connection | self.menu | unit_codes))
        self.assertEqual(ordered.ids, [connection.id, self.menu.id, unit_codes.id])

    def test_admin_sees_the_menu(self):
        self.assertIn(self.menu.id, self._visible(self.admin))

    def test_billing_user_does_not_see_the_menu(self):
        self.assertNotIn(self.menu.id, self._visible(self.billing))
