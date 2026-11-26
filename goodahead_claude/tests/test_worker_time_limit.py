# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A request must time out before the worker running it is killed.

Odoo kills a worker that runs longer than ``limit_time_real``. A request
that is still waiting then is billed, its answer lost, and the job is run
again. With worker processes the client keeps the request's timeout a
margin below that limit; without them (``workers = 0``) the limit is not
enforced and the timeout is left as asked.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.tools import config

from odoo.addons.goodahead_claude.lib import claude_client as _lib


@tagged("post_install", "-at_install")
class TestWorkerTimeLimit(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().claude_api_key = "zz-test-not-a-real-key"
        cls.company.claude_default_model_id = cls.env["goodahead.claude.model"].create(
            {"name": "ZZ Test", "model_api_id": "zz-test-model", "pdf_supported": True}
        )

    def setUp(self):
        super().setUp()
        self.calls = []

        def _fake(api_key, model, messages, **kwargs):
            self.calls.append(kwargs)
            return {"content": [], "usage": {}}

        fake = patch.object(_lib, "messages_create", _fake)
        fake.start()
        self.addCleanup(fake.stop)

    def _sent_timeout(self, timeout):
        self.env["goodahead.claude.client"].messages_create(
            messages=[], company=self.company, timeout=timeout
        )
        return self.calls[-1]["timeout"]

    def test_with_workers_the_timeout_stays_below_the_time_limit(self):
        with patch.dict(config.options, {"workers": 2, "limit_time_real": 120}):
            sent = self._sent_timeout(300)
        self.assertLess(sent, 120)
        self.assertGreaterEqual(sent, 60)

    def test_a_short_timeout_is_kept(self):
        with patch.dict(config.options, {"workers": 2, "limit_time_real": 120}):
            self.assertEqual(self._sent_timeout(30), 30)

    def test_a_raised_time_limit_lets_a_long_request_through(self):
        with patch.dict(config.options, {"workers": 2, "limit_time_real": 900}):
            self.assertEqual(self._sent_timeout(300), 300)

    def test_without_workers_the_limit_does_not_apply(self):
        with patch.dict(config.options, {"workers": 0, "limit_time_real": 120}):
            self.assertEqual(self._sent_timeout(300), 300)
