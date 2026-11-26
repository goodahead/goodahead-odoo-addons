# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""APIX connection add-on: AC-12, AC-13, AC-14, AC-16, AC-16b.

No network: `ApixClient.company_info` / `retrieve_transfer_id` are patched.
All data is invented (example.test logins, made-up Business IDs).
"""

import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the client tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from psycopg2 import IntegrityError  # noqa: E402

from odoo.exceptions import AccessError, ValidationError  # noqa: E402
from odoo.tests import TransactionCase, new_test_user, tagged  # noqa: E402
from odoo.tools import mute_logger  # noqa: E402

from odoo.addons.goodahead_apix.lib.apix_client import (  # noqa: E402
    ApixClient,
    ApixRejected,
    ApixServiceError,
    ApixUnreachable,
    password_hash,
)

PASSWORD = "Sup3r-Secret-Web-Pa55word"
TRANSFER_ID = "0a0a0a0a-1b1b-4c2c-8d3d-000000000001"
TRANSFER_KEY = "0f0f0f0f-2e2e-4d4d-9c9c-000000000001"
BUSINESS_ID = "1234567-8"


def company_answer(business_id=BUSINESS_ID, name="ZZ Example Company Oy"):
    return {
        "status": "OK",
        "status_code": "5000",
        "free_text": "",
        "free_texts": {},
        "groups": [{"y-tunnus": business_id, "Name": name}, {"AddressType": "Billing"}],
    }


@tagged("post_install", "-at_install")
class TestApixConnection(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "Second Test Company Oy"})
        cls.admin = new_test_user(
            cls.env,
            login="apix_admin",
            email="apix.admin@example.test",
            groups="account.group_account_manager,base.group_user",
            company_ids=[(6, 0, (cls.company | cls.company_b).ids)],
        )
        cls.billing = new_test_user(
            cls.env,
            login="apix_billing",
            email="apix.billing@example.test",
            groups="account.group_account_invoice,base.group_user",
        )
        cls.Connection = cls.env["goodahead.apix.connection"]

    def _connection(self, company=None, **vals):
        values = {
            "company_id": (company or self.company).id,
            "environment": "test",
            "business_id": BUSINESS_ID,
            "transfer_id": TRANSFER_ID,
            "transfer_key": TRANSFER_KEY,
        }
        values.update(vals)
        return self.Connection.with_user(self.admin).create(values)

    # ------------------------------------------------------------------
    # AC-12 — fetch from APIX login
    # ------------------------------------------------------------------

    def test_ac12_fetch_stores_credentials_and_never_the_password(self):
        wizard = (
            self.env["goodahead.apix.fetch.credentials"]
            .with_user(self.admin)
            .create(
                {
                    "company_id": self.company.id,
                    "environment": "test",
                    "email": "apix.admin@example.test",
                    "password": PASSWORD,
                    "business_id": BUSINESS_ID,
                }
            )
        )
        # The dialog is saved before its button runs: the clear password must
        # never be written, only its hash.
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT password, password_sha256 FROM goodahead_apix_fetch_credentials WHERE id = %s", (wizard.id,)
        )
        stored_password, stored_hash = self.env.cr.fetchone()
        self.assertFalse(stored_password)
        self.assertEqual(stored_hash, password_hash(PASSWORD))

        answer = {"TransferID": TRANSFER_ID, "TransferKey": TRANSFER_KEY, "UniqueCompanyID": "x"}
        with (
            patch.object(ApixClient, "retrieve_transfer_id", return_value=answer) as retrieve,
            patch.object(ApixClient, "company_info", return_value=company_answer()),
        ):
            wizard.action_fetch()

        retrieve.assert_called_once_with(
            BUSINESS_ID, "apix.admin@example.test", password_sha256=password_hash(PASSWORD)
        )
        self.assertFalse(wizard.sudo().password_sha256)
        connection = self.Connection.with_user(self.admin).search([("company_id", "=", self.company.id)])
        self.assertEqual(len(connection), 1)
        self.assertEqual(connection.transfer_id, TRANSFER_ID)
        self.assertEqual(connection.transfer_key, TRANSFER_KEY)
        self.assertEqual(connection.business_id, BUSINESS_ID)
        self.assertEqual(connection.status, "working")
        self.assertIn("ZZ Example Company Oy", connection.status_message)

        # The password is gone from the dialog, in cache and in the database.
        self.assertFalse(wizard.sudo().password or wizard.sudo().password_sha256)
        self.env.flush_all()
        self.env.cr.execute("SELECT count(*) FROM goodahead_apix_fetch_credentials WHERE password IS NOT NULL")
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        # ...and in no stored char/text field of the connection or the wizard.
        for record in (connection.sudo(), wizard.sudo()):
            for name, field in record._fields.items():
                if field.type in ("char", "text", "html") and field.store:
                    self.assertNotIn(PASSWORD, str(record[name] or ""), f"{record._name}.{name}")
        self.assertNotIn("password", self.Connection._fields)
        # Nothing secret went to the chatter.
        bodies = " ".join(str(m.body) for m in connection.sudo().message_ids)
        tracked = " ".join(
            str(v.old_value_char) + str(v.new_value_char)
            for v in connection.sudo().message_ids.tracking_value_ids
        )
        for secret in (PASSWORD, TRANSFER_KEY):
            self.assertNotIn(secret, bodies)
            self.assertNotIn(secret, tracked)

    def test_ac12_fetch_rejected_login_shows_reason_and_clears_password(self):
        wizard = (
            self.env["goodahead.apix.fetch.credentials"]
            .with_user(self.admin)
            .create(
                {
                    "company_id": self.company.id,
                    "email": "apix.admin@example.test",
                    "password": PASSWORD,
                    "business_id": BUSINESS_ID,
                }
            )
        )
        with patch.object(
            ApixClient,
            "retrieve_transfer_id",
            side_effect=ApixRejected("RetrieveTransferID: Authentication failed", status_code="4701"),
        ):
            action = wizard.action_fetch()
        self.assertEqual(action["res_model"], "goodahead.apix.fetch.credentials")
        self.assertIn("Authentication failed", wizard.error_message)
        self.assertFalse(wizard.sudo().password or wizard.sudo().password_sha256)
        self.assertFalse(self.Connection.search([("company_id", "=", self.company.id)]))

    def test_fetch_unexpected_error_reports_and_still_clears_the_hash(self):
        wizard = (
            self.env["goodahead.apix.fetch.credentials"]
            .with_user(self.admin)
            .create(
                {
                    "company_id": self.company.id,
                    "email": "apix.admin@example.test",
                    "password": PASSWORD,
                    "business_id": BUSINESS_ID,
                }
            )
        )
        with (
            patch.object(ApixClient, "retrieve_transfer_id", side_effect=RuntimeError("zz boom")),
            mute_logger("odoo.addons.goodahead_apix.wizard.apix_fetch_credentials"),
        ):
            action = wizard.action_fetch()
        self.assertEqual(action["res_model"], "goodahead.apix.fetch.credentials")
        self.assertIn("unexpected error", wizard.error_message)
        self.assertNotIn("zz boom", wizard.error_message, "a plain reason, not the exception")
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT password_sha256 FROM goodahead_apix_fetch_credentials WHERE id = %s", (wizard.id,)
        )
        self.assertFalse(self.env.cr.fetchone()[0])
        self.assertFalse(self.Connection.sudo().search([("company_id", "=", self.company.id)]))

    def test_fetch_error_after_the_answer_rolls_back_the_connection(self):
        wizard = (
            self.env["goodahead.apix.fetch.credentials"]
            .with_user(self.admin)
            .create(
                {
                    "company_id": self.company.id,
                    "email": "apix.admin@example.test",
                    "password": PASSWORD,
                    "business_id": BUSINESS_ID,
                }
            )
        )
        answer = {"TransferID": TRANSFER_ID, "TransferKey": TRANSFER_KEY}
        with (
            patch.object(ApixClient, "retrieve_transfer_id", return_value=answer),
            patch.object(ApixClient, "company_info", side_effect=KeyError("zz")),
            mute_logger("odoo.addons.goodahead_apix.wizard.apix_fetch_credentials"),
        ):
            action = wizard.action_fetch()
        self.assertEqual(action["res_model"], "goodahead.apix.fetch.credentials")
        self.assertFalse(wizard.sudo().password_sha256)
        self.assertFalse(self.Connection.sudo().search([("company_id", "=", self.company.id)]))

    def test_ac12_company_business_id_must_match_apix(self):
        """N5: the company's own Business ID, when set, must be the one APIX
        reports — whatever was typed on the connection."""
        self.company.company_registry = "7654321-0"
        connection = self._connection()
        with patch.object(ApixClient, "company_info", return_value=company_answer()):
            connection.action_check_connection()
        self.assertEqual(connection.status, "rejected")
        self.assertIn("7654321-0", connection.status_message)
        self.assertIn(self.company.name, connection.status_message)
        self.assertFalse(self.Connection._get_working_connection(self.company))

        self.company.company_registry = "FI12345678"
        with patch.object(ApixClient, "company_info", return_value=company_answer("1234567-8")):
            connection.action_check_connection()
        self.assertEqual(connection.status, "working")

    def test_ac13_service_error_is_not_a_rejection(self):
        connection = self._connection()
        with patch.object(
            ApixClient,
            "company_info",
            side_effect=ApixServiceError("RetrieveCompanyInformation: Internal error (StatusCode 9099)"),
        ):
            connection.action_check_connection()
        self.assertEqual(connection.status, "unchecked")
        self.assertIn("Internal error", connection.status_message)

    def test_credentials_are_never_exported(self):
        connection = self._connection()
        exporter = new_test_user(
            self.env,
            login="apix_exporter",
            email="apix.exporter@example.test",
            groups="account.group_account_manager,base.group_user,base.group_allow_export",
        )
        as_exporter = connection.with_user(exporter)
        fields_info = self.Connection.with_user(exporter).fields_get(["transfer_id", "transfer_key"])
        self.assertFalse(fields_info["transfer_id"]["exportable"])
        self.assertFalse(fields_info["transfer_key"]["exportable"])
        for names in (["transfer_key"], ["business_id", "transfer_id"], ["transfer_key/id"]):
            with self.assertRaises(AccessError):
                as_exporter.export_data(names)
        self.assertEqual(as_exporter.export_data(["business_id"])["datas"], [[BUSINESS_ID]])

    def test_ac12_business_id_mismatch_is_rejected(self):
        connection = self._connection()
        with patch.object(ApixClient, "company_info", return_value=company_answer("7654321-0", "Other Oy")):
            connection.action_check_connection()
        self.assertEqual(connection.status, "rejected")
        self.assertIn("7654321-0", connection.status_message)
        self.assertIn(BUSINESS_ID, connection.status_message)
        self.assertFalse(self.Connection._get_working_connection(self.company))

    def test_ac12_business_id_formats_compare_equal(self):
        connection = self._connection(business_id="FI12345678")
        with patch.object(ApixClient, "company_info", return_value=company_answer("1234567-8")):
            connection.action_check_connection()
        self.assertEqual(connection.status, "working")
        self.assertTrue(connection.last_check_date)
        self.assertEqual(self.Connection._get_working_connection(self.company), connection)

    # ------------------------------------------------------------------
    # AC-13 — rejected credentials
    # ------------------------------------------------------------------

    def test_ac13_rejected_credentials(self):
        connection = self._connection()
        with patch.object(
            ApixClient,
            "company_info",
            side_effect=ApixRejected("RetrieveCompanyInformation: Invalid digest", status_code="4701"),
        ):
            connection.action_check_connection()
        self.assertEqual(connection.status, "rejected")
        self.assertIn("Invalid digest", connection.status_message)
        self.assertNotIn(TRANSFER_KEY, connection.status_message)
        self.assertFalse(self.Connection._get_working_connection(self.company))

    def test_ac13_unreachable_is_not_working(self):
        connection = self._connection()
        with patch.object(ApixClient, "company_info", side_effect=ApixUnreachable("APIX could not be reached")):
            connection.action_check_connection()
        self.assertNotEqual(connection.status, "working")
        self.assertIn("could not be reached", connection.status_message)

    def test_ac13_changed_credentials_stop_fetching_until_checked(self):
        connection = self._connection()
        with patch.object(ApixClient, "company_info", return_value=company_answer()):
            connection.action_check_connection()
        self.assertTrue(self.Connection._get_working_connection(self.company))
        connection.transfer_key = "bbbbbbb2-cccc-4ddd-8eee-ffffffffffff"
        self.assertEqual(connection.status, "unchecked")
        self.assertFalse(self.Connection._get_working_connection(self.company))

    # ------------------------------------------------------------------
    # AC-14 — billing users see the status only
    # ------------------------------------------------------------------

    def test_ac14_billing_user_sees_status_not_credentials(self):
        connection = self._connection()
        with patch.object(ApixClient, "company_info", return_value=company_answer()):
            connection.action_check_connection()
        as_billing = connection.with_user(self.billing)
        as_billing.invalidate_recordset()
        values = as_billing.read(["status", "status_message", "business_id"])[0]
        self.assertEqual(values["status"], "working")
        with self.assertRaises(AccessError):
            as_billing.read(["transfer_key"])
        with self.assertRaises(AccessError):
            as_billing.read(["transfer_id"])
        with self.assertRaises(AccessError):
            as_billing.write({"transfer_key": "stolen"})
        with self.assertRaises(AccessError):
            as_billing.write({"business_id": "7654321-0"})
        with self.assertRaises(AccessError):
            self.env["goodahead.apix.fetch.credentials"].with_user(self.billing).create(
                {"company_id": self.company.id}
            )
        # Credential fields are not even offered to a billing user.
        fields_info = self.Connection.with_user(self.billing).fields_get()
        self.assertNotIn("transfer_key", fields_info)
        self.assertNotIn("transfer_id", fields_info)
        self.assertIn("status", fields_info)

    def test_transfer_key_not_copied(self):
        field = self.Connection._fields["transfer_key"]
        self.assertFalse(field.copy)
        self.assertFalse(self.Connection._fields["transfer_id"].copy)

    # ------------------------------------------------------------------
    # AC-16 — one APIX account, one company
    # ------------------------------------------------------------------

    def test_ac16_same_credentials_for_second_company_refused(self):
        self._connection()
        with self.assertRaises(ValidationError) as caught:
            self._connection(company=self.company_b, business_id="7654321-0")
        self.assertIn(self.company.name, str(caught.exception))

    def test_ac16_refused_on_write_too(self):
        self._connection()
        other = self._connection(company=self.company_b, business_id="7654321-0", transfer_id="other-id")
        with self.assertRaises(ValidationError) as caught:
            other.write({"transfer_id": TRANSFER_ID})
        self.assertIn(self.company.name, str(caught.exception))

    def test_ac16_fetch_for_second_company_refused_naming_first(self):
        self._connection()
        wizard = (
            self.env["goodahead.apix.fetch.credentials"]
            .with_user(self.admin)
            .with_company(self.company_b)
            .create(
                {
                    "company_id": self.company_b.id,
                    "environment": "test",
                    "email": "apix.admin@example.test",
                    "password": PASSWORD,
                    "business_id": BUSINESS_ID,
                }
            )
        )
        answer = {"TransferID": TRANSFER_ID, "TransferKey": TRANSFER_KEY}
        with patch.object(ApixClient, "retrieve_transfer_id", return_value=answer):
            wizard.action_fetch()
        self.assertIn(self.company.name, wizard.error_message)
        self.assertFalse(self.Connection.sudo().search([("company_id", "=", self.company_b.id)]))
        self.assertFalse(wizard.sudo().password or wizard.sudo().password_sha256)

    def test_ac16_same_transfer_id_in_other_environment_allowed(self):
        self._connection()
        other = self._connection(company=self.company_b, environment="production")
        self.assertEqual(other.environment, "production")

    def test_one_connection_per_company(self):
        self._connection()
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self._connection(transfer_id="another-id")
            self.env.flush_all()

    # ------------------------------------------------------------------
    # AC-16b — the connection stands on its own
    # ------------------------------------------------------------------

    def test_ac16b_connection_independent_of_receiving(self):
        # Nothing from the receiving add-on is needed to set up, check and
        # hand out a working connection.
        connection = self._connection()
        with patch.object(ApixClient, "company_info", return_value=company_answer()):
            connection.action_check_connection()
        self.assertEqual(self.Connection._get_working_connection(self.company), connection)
        self.assertEqual(connection._client().environment, "test")

    def test_settings_show_connection_status(self):
        connection = self._connection()
        settings = self.env["res.config.settings"].create({})
        self.assertEqual(settings.apix_connection_id, connection)
        self.assertEqual(settings.apix_status, "unchecked")
        action = settings.action_open_apix_connection()
        self.assertEqual(action["res_id"], connection.id)
