# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Copy safety of the APIX connection.

A neutralized database copy loses its transfer credentials, so it can neither
receive nor send until an Administrator enters them again. Every sending
surface asks ``_apix_copy_warning()``: a copy holding production credentials
(mirror receiving, or a neutralized database with production credentials
entered again) is warned about, never blocked. Every id and key is invented.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the client tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.addons.goodahead_apix.models.apix_connection import COPY_WARNING  # noqa: E402
from odoo.modules.neutralize import get_neutralization_queries, neutralize_database  # noqa: E402
from odoo.tests import TransactionCase, tagged  # noqa: E402

TRANSFER_ID = "zz000000-0000-4000-8000-000000000001"
TRANSFER_KEY = "zz-secret-transfer-key-0001"


@tagged("post_install", "-at_install")
class TestApixCopySafety(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.Param = cls.env["ir.config_parameter"].sudo()
        cls.Param.set_param("database.is_neutralized", False)

    def _connection(self, name, environment="production", credentials=True):
        company = self.env["res.company"].create({"name": f"ZZ Copy {name} Oy"})
        vals = {"company_id": company.id, "environment": environment, "business_id": "0000001-9"}
        if credentials:
            vals.update(transfer_id=f"{TRANSFER_ID}-{name}", transfer_key=TRANSFER_KEY)
        connection = self.Connection.create(vals)
        connection.write({"status": "working"})
        return connection

    def _neutralized(self):
        self.Param.set_param("database.is_neutralized", True)

    # -- neutralization ---------------------------------------------

    def test_neutralization_clears_credentials_and_resets_status(self):
        connections = self._connection("A") | self._connection("B", environment="test")
        self.env.flush_all()

        neutralize_database(self.env.cr)
        self.env.invalidate_all()

        for connection in connections.sudo():
            with self.subTest(company=connection.company_id.name):
                self.assertFalse(connection.transfer_id)
                self.assertFalse(connection.transfer_key)
                self.assertEqual(connection.status, "unchecked")
                self.assertEqual(
                    connection.status_message, "Credentials cleared by database neutralization"
                )
                # The business id stays: re-entering credentials is enough.
                self.assertEqual(connection.business_id, "0000001-9")

    def test_neutralize_script_is_shipped(self):
        queries = list(get_neutralization_queries(["goodahead_apix"]))
        self.assertEqual(len(queries), 1)
        self.assertIn("transfer_key = NULL", queries[0])

    # -- copy warning -------------------------------------------------

    def test_no_warning_on_an_ordinary_database(self):
        self.assertFalse(self._connection("C")._apix_copy_warning())

    def test_warning_on_a_neutralized_production_copy_with_credentials(self):
        connection = self._connection("D")
        self._neutralized()
        self.assertEqual(connection._apix_copy_warning(), COPY_WARNING)
        self.assertIn("mirror environment holding production credentials", COPY_WARNING)
        self.assertIn("sending reaches real customers", COPY_WARNING)

    def test_no_warning_on_a_neutralized_copy_without_credentials(self):
        connection = self._connection("E", credentials=False)
        self._neutralized()
        self.assertFalse(connection._apix_copy_warning())

    def test_no_warning_on_a_neutralized_copy_with_test_credentials(self):
        connection = self._connection("F", environment="test")
        self._neutralized()
        self.assertFalse(connection._apix_copy_warning())

    def test_warning_after_credentials_entered_again(self):
        connection = self._connection("G")
        self.env.flush_all()
        neutralize_database(self.env.cr)
        self.env.invalidate_all()
        self.assertFalse(connection._apix_copy_warning())
        connection.write({"transfer_id": f"{TRANSFER_ID}-G2", "transfer_key": TRANSFER_KEY})
        self.assertEqual(connection._apix_copy_warning(), COPY_WARNING)

    def test_no_connection_no_warning(self):
        self._neutralized()
        self.assertFalse(self.Connection._apix_copy_warning())

    def test_warning_is_readable_by_billing_users(self):
        connection = self._connection("H")
        self._neutralized()
        user = self.env["res.users"].create(
            {
                "name": "ZZ Billing",
                "login": "zz_copy_billing",
                "groups_id": [(6, 0, [self.env.ref("account.group_account_invoice").id])],
            }
        )
        self.assertEqual(connection.with_user(user)._apix_copy_warning(), COPY_WARNING)

    # -- receiving's mirror answer --------------------------------------------

    def _mirror(self, connection):
        if "receiving_state" not in self.Connection._fields:
            self.skipTest("APIX receiving is not installed")
        connection._apix_set_receiving_state("mirror")

    def test_warning_in_mirror_mode(self):
        connection = self._connection("I")
        self._mirror(connection)
        self.assertEqual(connection._apix_copy_warning(), COPY_WARNING)

    def test_mirror_and_neutralized_give_one_warning(self):
        connection = self._connection("J")
        self._mirror(connection)
        self._neutralized()
        self.assertEqual(connection._apix_copy_warning(), COPY_WARNING)
