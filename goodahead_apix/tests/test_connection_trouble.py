# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The sending interface fields and the connection-trouble attention section.


Administrators see a company's refused APIX connection in the shared
e-invoicing attention bubble; Accountants are not shown the connection trouble
as such and cannot read the interface fields. Every name and id is invented.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the client tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import AccessError  # noqa: E402
from odoo.tests import TransactionCase, new_test_user, tagged  # noqa: E402


@tagged("post_install", "-at_install")
class TestConnectionTrouble(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "ZZ Trouble Second Oy"})
        cls.company_c = cls.env["res.company"].create({"name": "ZZ Trouble Third Oy"})
        both = [(6, 0, (cls.company | cls.company_b).ids)]
        cls.admin = new_test_user(
            cls.env,
            login="zz_trouble_admin",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=both,
        )
        cls.accountant = new_test_user(
            cls.env,
            login="zz_trouble_accountant",
            groups="account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=both,
        )
        Connection = cls.env["goodahead.apix.connection"].sudo()
        Connection.search([]).unlink()
        cls.conn_a = Connection.create({"company_id": cls.company.id, "business_id": "0000001-9"})
        cls.conn_b = Connection.create({"company_id": cls.company_b.id, "business_id": "0000002-7"})
        cls.conn_c = Connection.create({"company_id": cls.company_c.id, "business_id": "0000003-5"})

    def _reject(self, connection, message="APIX refused the transfer credentials."):
        connection.sudo().write({"status": "rejected", "status_message": message})

    def _section(self, user):
        data = self.env["goodahead.einvoice.attention"].with_user(user)._attention_data()
        return next((s for s in data["sections"] if s["key"] == "connection_trouble"), None)

    # -- interface fields ------------------------------------------------

    def test_interface_fields_default(self):
        connection = self.conn_a.with_user(self.admin)
        self.assertEqual(connection.interface_name, "Standard")
        self.assertEqual(connection.interface_version, "1.0")
        field = self.env["goodahead.apix.connection"]._fields["interface_name"]
        self.assertTrue(field.required)
        self.assertEqual(field.groups, "account.group_account_manager")
        self.assertEqual(
            self.env["goodahead.apix.connection"]._fields["interface_version"].groups,
            "account.group_account_manager",
        )

    def test_administrator_changes_the_interface(self):
        self.conn_a.with_user(self.admin).write(
            {"interface_name": "ZZ Interface", "interface_version": "2.1"}
        )
        self.assertEqual(self.conn_a.interface_name, "ZZ Interface")
        self.assertEqual(self.conn_a.interface_version, "2.1")

    def test_accountant_cannot_read_the_interface_fields(self):
        connection = self.conn_a.with_user(self.accountant)
        self.assertEqual(connection.status, "unchecked")  # still sees the status
        with self.assertRaises(AccessError):
            connection.read(["interface_name"])
        with self.assertRaises(AccessError):
            connection.read(["interface_version"])

    def test_interface_fields_on_the_form_for_administrators_only(self):
        view = self.env.ref("goodahead_apix.goodahead_apix_connection_view_form")
        for user, expected in ((self.admin, True), (self.accountant, False)):
            with self.subTest(user=user.login):
                arch = self.env["goodahead.apix.connection"].with_user(user).get_views(
                    [(view.id, "form")]
                )["views"]["form"]["arch"]
                self.assertEqual('name="interface_name"' in arch, expected)
                self.assertEqual('name="interface_version"' in arch, expected)

    # -- connection trouble ----------------------------------------------

    def test_no_trouble_no_section(self):
        self.assertIsNone(self._section(self.admin))

    def test_unchecked_and_working_are_not_trouble(self):
        self.conn_a.sudo().write({"status": "working"})
        self.assertIsNone(self._section(self.admin))

    def test_administrator_sees_a_rejected_connection(self):
        self._reject(self.conn_a)
        section = self._section(self.admin)
        self.assertIsNotNone(section)
        self.assertEqual(section["count"], 1)
        self.assertEqual(section["groups"], "account.group_account_manager")
        self.assertEqual(section["label"], "APIX connection not working")
        item = section["items"][0]
        self.assertEqual(item["id"], self.conn_a.id)
        self.assertEqual(item["title"], self.company.name)
        self.assertEqual(item["subtitle"], "APIX refused the transfer credentials.")
        self.assertEqual(section["item_action"]["res_model"], "goodahead.apix.connection")
        self.assertEqual(section["action"]["res_model"], "goodahead.apix.connection")

    def test_only_connections_of_allowed_companies_count(self):
        self._reject(self.conn_b)
        self._reject(self.conn_c)  # the administrator is not allowed in company C
        section = self._section(self.admin)
        self.assertEqual(section["count"], 1)
        self.assertEqual([item["id"] for item in section["items"]], [self.conn_b.id])

    def test_accountant_is_not_shown_the_connection_trouble(self):
        self._reject(self.conn_a)
        self.assertIsNone(self._section(self.accountant))

    def test_domain_hook(self):
        domain = self.env["goodahead.apix.connection"]._attention_connection_domain()
        self._reject(self.conn_b)
        found = self.env["goodahead.apix.connection"].sudo().search(domain)
        self.assertEqual(found, self.conn_b)
