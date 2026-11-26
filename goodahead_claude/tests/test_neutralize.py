# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A neutralized database copy never calls Claude with the live key.

`data/neutralize.sql` runs when a production copy is neutralized for a test
or staging environment. Without it, imports and checks on the copy would
keep sending documents to Anthropic on the production account, and every
administrator of the copy could read the key.
"""

from odoo.modules.neutralize import neutralize_database
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestClaudeNeutralize(TransactionCase):
    def test_neutralize_clears_every_company_key(self):
        companies = self.env.company | self.env["res.company"].create({"name": "ZZ Neutral Oy"})
        companies.sudo().write({"claude_api_key": "zz-test-not-a-real-key"})
        self.env.flush_all()

        neutralize_database(self.env.cr)
        self.env.invalidate_all()

        self.assertEqual(companies.sudo().mapped("claude_api_key"), [False, False])
