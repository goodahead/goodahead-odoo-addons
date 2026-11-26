# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Who is told about AI imports: the failed-imports bubble and the menus.

Only Bookkeeper (`group_account_user`) or accounting Administrator
(`group_account_manager`) users are told, not plain Invoicing users. Access
rights are not narrowed: an Invoicing user may still open the import records.
"""

from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestImportVisibility(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.AiImport = cls.env["goodahead.invoice.import"]
        cls.invoicing = new_test_user(
            cls.env,
            login="ai_vis_invoicing",
            groups="base.group_user,account.group_account_invoice",
        )
        cls.bookkeeper = new_test_user(
            cls.env,
            login="ai_vis_bookkeeper",
            groups="base.group_user,account.group_account_user",
        )
        cls.administrator = new_test_user(
            cls.env,
            login="ai_vis_admin",
            groups="base.group_user,account.group_account_manager",
        )

    def test_invoicing_only_is_not_told(self):
        self.assertFalse(self.AiImport._ai_user_works_imports(self.invoicing))
        self.assertFalse(
            self.AiImport.with_user(self.invoicing)._ai_user_works_imports()
        )

    def test_bookkeeper_is_told(self):
        self.assertTrue(self.AiImport._ai_user_works_imports(self.bookkeeper))

    def test_administrator_is_told_without_bookkeeper(self):
        """In core, the accounting Administrator group does not imply Bookkeeper."""
        self.assertTrue(self.AiImport._ai_user_works_imports(self.administrator))

    def test_invoicing_user_can_still_read_imports(self):
        record = self.AiImport.create({"name": "visibility.pdf"})
        self.assertEqual(
            record.with_user(self.invoicing).read(["name"])[0]["name"],
            "visibility.pdf",
        )

    def test_menus_carry_the_same_two_groups(self):
        expected = {
            self.env.ref("account.group_account_user"),
            self.env.ref("account.group_account_manager"),
        }
        for xmlid in (
            "goodahead_invoice_import.menu_ai_invoice_import",
            "goodahead_invoice_import.menu_ai_invoice_import_config",
            "goodahead_invoice_import.menu_vendor_data_suggestion",
        ):
            with self.subTest(menu=xmlid):
                self.assertEqual(set(self.env.ref(xmlid).groups_id), expected)

    def test_invoicing_user_does_not_see_the_menus(self):
        visible = (
            self.env["ir.ui.menu"]
            .with_user(self.invoicing)
            ._visible_menu_ids()
        )
        for xmlid in (
            "goodahead_invoice_import.menu_ai_invoice_import",
            "goodahead_invoice_import.menu_ai_invoice_import_config",
        ):
            with self.subTest(menu=xmlid):
                self.assertNotIn(self.env.ref(xmlid).id, visible)
