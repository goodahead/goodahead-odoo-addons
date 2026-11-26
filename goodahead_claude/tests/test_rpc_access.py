# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The company's Claude key is spent only by server code, never by an RPC.

Every logged-in user, a portal user included, can call any public method of
any model through ``/web/dataset/call_kw``. The client's methods are
therefore not callable that way; the two that act on the company's account
for an administrator (test the connection, sync the models) also check the
caller in Python; and what a caller may ask for (output tokens, timeout) is
clamped on the server. No test here touches the network.
"""

from unittest.mock import patch

from odoo.api import call_kw
from odoo.exceptions import AccessError
from odoo.service.model import get_public_method
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.goodahead_claude.lib import claude_client as _lib

CLIENT = "goodahead.claude.client"
RPC_METHODS = (
    ("messages_create", [], {"messages": [{"role": "user", "content": "Hi"}],
                             "max_tokens": 100000, "timeout": 600}),
    ("extract_text", [{"content": []}], {}),
    ("extract_usage", [{"usage": {}}], {}),
    ("test_connection", [], {}),
    ("sync_models", [], {}),
)


def _rpc(env, model, method, args, kwargs):
    """What `/web/dataset/call_kw` does with a request (18.0: the route checks
    `get_public_method`, then `odoo.api.call_kw` calls)."""
    get_public_method(env[model], method)
    return call_kw(env[model], method, args, kwargs)


class _NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("a goodahead_claude test tried to use requests.%s" % name)


@tagged("post_install", "-at_install")
class TestClaudeClientRpcAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().claude_api_key = "zz-test-not-a-real-key"
        cls.model = cls.env["goodahead.claude.model"].create(
            {"name": "ZZ Test", "model_api_id": "zz-test-model", "pdf_supported": True}
        )
        cls.company.claude_default_model_id = cls.model
        cls.portal = new_test_user(cls.env, login="zz_claude_portal", groups="base.group_portal")
        cls.internal = new_test_user(cls.env, login="zz_claude_internal", groups="base.group_user")
        cls.admin = new_test_user(
            cls.env, login="zz_claude_admin", groups="base.group_user,base.group_system"
        )

    def setUp(self):
        super().setUp()
        self.calls = []
        guard = patch.object(_lib, "requests", _NoNetwork())
        guard.start()
        self.addCleanup(guard.stop)

        def _fake_messages_create(api_key, model, messages, **kwargs):
            self.calls.append(kwargs)
            return {"content": [{"type": "text", "text": "OK"}], "usage": {}}

        fake = patch.object(_lib, "messages_create", _fake_messages_create)
        fake.start()
        self.addCleanup(fake.stop)
        listing = patch.object(_lib, "list_models", lambda api_key: [])
        listing.start()
        self.addCleanup(listing.stop)

    def test_no_client_method_is_callable_over_rpc_by_a_portal_user(self):
        env = self.env(user=self.portal)
        for method, args, kwargs in RPC_METHODS:
            with self.subTest(method=method), self.assertRaises(AccessError):
                _rpc(env, CLIENT, method, args, kwargs)
        self.assertEqual(self.calls, [], "no request may reach the API")

    def test_no_client_method_is_callable_over_rpc_by_an_internal_user(self):
        env = self.env(user=self.internal)
        for method, args, kwargs in RPC_METHODS:
            with self.subTest(method=method), self.assertRaises(AccessError):
                _rpc(env, CLIENT, method, args, kwargs)
        self.assertEqual(self.calls, [])

    def test_not_even_an_administrator_calls_them_over_rpc(self):
        env = self.env(user=self.admin)
        for method, args, kwargs in RPC_METHODS:
            with self.subTest(method=method), self.assertRaises(AccessError):
                _rpc(env, CLIENT, method, args, kwargs)
        self.assertEqual(self.calls, [])

    def test_connection_test_and_model_sync_need_an_administrator(self):
        Client = self.env[CLIENT].with_user(self.internal)
        with self.assertRaises(AccessError):
            Client.test_connection(company=self.company)
        with self.assertRaises(AccessError):
            Client.sync_models(company=self.company)
        self.assertEqual(self.calls, [])
        # The control: an administrator gets through both.
        Admin = self.env[CLIENT].with_user(self.admin)
        self.assertTrue(Admin.test_connection(company=self.company))
        self.assertEqual(Admin.sync_models(company=self.company), 0)
        self.assertEqual(len(self.calls), 1)

    def test_server_code_still_calls_the_api_for_any_user(self):
        """Server code (an import job run as an accountant) keeps working."""
        Client = self.env[CLIENT].with_user(self.internal)
        response = Client.messages_create(messages=[{"role": "user", "content": "Hi"}],
                                          company=self.company)
        self.assertEqual(Client.extract_text(response), "OK")
        self.assertEqual(len(self.calls), 1)

    def test_output_tokens_and_timeout_are_clamped_on_the_server(self):
        Client = self.env[CLIENT]
        Client.messages_create(messages=[], company=self.company,
                               max_tokens=10 ** 9, timeout=10 ** 6)
        sent = self.calls[-1]
        self.assertLessEqual(sent["max_tokens"], Client._max_output_tokens_ceiling())
        self.assertLessEqual(sent["timeout"], Client._timeout_ceiling())
        Client.messages_create(messages=[], company=self.company, max_tokens=-5, timeout=-1)
        sent = self.calls[-1]
        self.assertGreaterEqual(sent["max_tokens"], 1)
        self.assertGreaterEqual(sent["timeout"], 1)
        # An ordinary request is sent as asked.
        Client.messages_create(messages=[], company=self.company, max_tokens=1600, timeout=90)
        self.assertEqual((self.calls[-1]["max_tokens"], self.calls[-1]["timeout"]), (1600, 90))

    def test_the_ceilings_are_system_parameters(self):
        Params = self.env["ir.config_parameter"].sudo()
        Params.set_param("goodahead_claude.max_output_tokens_ceiling", "2000")
        Params.set_param("goodahead_claude.timeout_ceiling", "45")
        self.env[CLIENT].messages_create(messages=[], company=self.company,
                                         max_tokens=50000, timeout=300)
        self.assertEqual((self.calls[-1]["max_tokens"], self.calls[-1]["timeout"]), (2000, 45))
