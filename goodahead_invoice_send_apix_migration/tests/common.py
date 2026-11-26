# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Shared set-up of the sending bridge's tests.

Runs without any real earlier connector: ``fake_sending_source`` registers a
test-only migration source on the connection model (patching
``_migration_sources`` and adding ``_migration_<code>_sending_*`` mocks), so
the tests behave the same whether or not an adapter is installed. No network,
no APIX call. Invented data only.
"""

from unittest.mock import patch

from odoo.tests import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

CONNECTION = "goodahead.apix.connection"
FAKE = "zzfake"
SENDING_METHODS = ("sending_status", "sending_read", "sending_stop", "sending_resume")


class SendingSourceMixin:
    def _patch_connection(self, name, **kwargs):
        patcher = patch.object(type(self.env[CONNECTION]), name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def fake_sending_source(self, code=FAKE, status=("sending", "it still sends"), values=None,
                            methods=SENDING_METHODS, extra_codes=()):
        """Register ``code`` as the one installed migration source (plus
        ``extra_codes`` without any method). ``status`` answers
        ``sending_status``, ``values`` answers ``sending_read``; only
        ``methods`` exist. Returns ``{method: mock}``."""
        self._patch_connection("_migration_sources", return_value=[code, *extra_codes])
        self._patch_connection(
            "_migration_source_label",
            side_effect=lambda source: f"ZZ Earlier Connector ({source})",
        )
        answers = {
            "sending_status": {"return_value": status},
            "sending_read": {"return_value": values or {}},
            "sending_stop": {"return_value": True},
            "sending_resume": {"return_value": True},
        }
        return {
            method: self._patch_connection(
                f"_migration_{code}_{method}", create=True, **answers[method]
            )
            for method in methods
        }


class SendingMigrationCase(SendingSourceMixin, AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.admin = new_test_user(
            cls.env,
            login="zz_sendmig_admin",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        cls.accountant = new_test_user(
            cls.env,
            login="zz_sendmig_accountant",
            groups="base.group_user,account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        Connection = cls.env[CONNECTION].sudo()
        cls.connection = Connection.search([("company_id", "=", cls.company.id)], limit=1)
        if not cls.connection:
            cls.connection = Connection.create(
                {"company_id": cls.company.id, "business_id": "0000001-9"}
            )
        cls.connection.write({"status": "working"})
        cls.operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Operator", "identifier": "ZZOPER"}
        )
        cls.customer = cls.env["res.partner"].create({"name": "ZZ Migrated Customer Oy"})
        cls.other = cls.env["res.partner"].create({"name": "ZZ Unlisted Customer Oy"})

    def as_admin(self):
        return self.env[CONNECTION].with_user(self.admin)
