# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Failed sends in the shared attention bubble, the failed-sends list and
"Mark handled".

Outcomes are written through the ledger as a send writes them; no APIX
call. Two companies, invented data only.
"""

import ast
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from .common import SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
ATTENTION = "goodahead.einvoice.attention"
MARK = "goodahead.invoice.send.mark.handled"


@tagged("post_install", "-at_install")
class TestFailedSends(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.accountant_a = new_test_user(
            cls.env, login="zz_failed_a_only", groups="account.group_account_invoice",
            company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)],
        )

    def _send_outcome(self, move, state, reason="[send.refused] ZZ invalid address"):
        Attempt = self.env[ATTEMPT]
        attempt = Attempt._ledger_claim(move, "einvoice", "button", self.accountant)
        values = {"reason": reason} if state in ("refused", "failed") else {"apix_batch_id": "ZZB"}
        return self._row(Attempt._ledger_finish(attempt, state, values))

    def _section(self, user=None, companies=None):
        user = user or self.accountant
        companies = companies or (self.company | self.company_b)
        data = self.env[ATTENTION].with_user(user).with_context(
            allowed_company_ids=companies.ids
        )._attention_data()
        return next((s for s in data["sections"] if s["key"] == "failed_sends"), None)

    def _count(self, user=None, companies=None):
        section = self._section(user, companies)
        return section["count"] if section else 0

    def _mark(self, attempt, reason, user=None):
        user = user or self.accountant
        action = attempt.with_user(user).action_open_mark_handled()
        self.assertEqual(action["res_model"], MARK)
        wizard = self.env[MARK].with_user(user).with_context(action["context"]).create(
            {"reason": reason}
        )
        return wizard.action_mark_handled()

    # -- the bubble ----------------------------------------------------------------

    def test_refused_and_failed_are_counted_and_open_the_delivery_tab(self):
        refused_move, failed_move = self._make_invoice(), self._make_invoice()
        self._send_outcome(refused_move, "refused")
        self._send_outcome(failed_move, "failed", "[send.no_answer] ZZ no answer")
        section = self._section()
        self.assertEqual(section["count"], 2)
        self.assertEqual(section["groups"], "account.group_account_invoice")
        self.assertEqual({item["id"] for item in section["items"]},
                         {refused_move.id, failed_move.id})
        titles = {item["title"] for item in section["items"]}
        self.assertEqual(titles, {refused_move.name, failed_move.name})
        self.assertEqual(section["item_action"]["res_model"], "account.move")
        self.assertTrue(section["item_action"]["context"]["goodahead_delivery_tab"])
        self.assertEqual(section["action"]["res_model"], ATTEMPT)

    def test_background_send_failures_count_too(self):
        move = self._make_invoice()
        Attempt = self.env[ATTEMPT]
        attempt_id = Attempt._ledger()._ledger_insert(
            "queued", move, "einvoice", "mass_background", self.accountant, "zz-run", "2026-11-25 10:00:00"
        )
        Attempt._ledger_start(Attempt.browse(attempt_id))
        Attempt._ledger_finish(Attempt.browse(attempt_id), "failed", {"reason": "[send.unreachable] ZZ"})
        self.assertEqual(self._count(), 1)

    def test_a_later_success_clears(self):
        move = self._make_invoice()
        self._send_outcome(move, "refused")
        self.assertEqual(self._count(), 1)
        self._send_outcome(move, "accepted")
        self.assertEqual(self._count(), 0)

    def test_a_download_after_a_failure_keeps_it_failed(self):
        """Review R4, AC-33: a download never counts, so it neither clears
        the failed send nor becomes the invoice's latest send."""
        move = self._make_invoice()
        refused = self._send_outcome(move, "refused")
        Attempt = self.env[ATTEMPT]
        download = self._row(Attempt._ledger_record(move, "download", "downloaded", "send_print",
                                                    self.accountant, {}))
        refused.invalidate_recordset()
        self.assertTrue(refused.is_latest)
        self.assertTrue(refused.needs_attention)
        self.assertFalse(download.is_latest)
        self.assertFalse(download.needs_attention)
        self.assertEqual(self._count(), 1)
        self.assertIn(move.id, {item["id"] for item in self._section()["items"]})
        failed_list = Attempt.with_user(self.accountant).search([("needs_attention", "=", True)])
        self.assertEqual(failed_list, refused)
        move.invalidate_recordset()
        self.assertEqual(move.send_state, "refused")
        self.assertEqual(Attempt.search_count([("move_id", "=", move.id), ("is_latest", "=", True)]),
                         1, "one latest send per invoice")
        # a counting outcome (an email) still takes over and clears it
        Attempt._ledger_record(move, "email", "sent", "send_print", self.accountant, {})
        self.assertEqual(self._count(), 0)

    def test_delivery_tab_opens_first_from_the_bubble(self):
        arch = self.env["account.move"].with_user(self.accountant).with_context(
            goodahead_delivery_tab=True
        ).get_views([(False, "form")])["views"]["form"]["arch"]
        page = etree.fromstring(arch).xpath("//page[@name='apix_delivery']")[0]
        self.assertEqual(page.get("autofocus"), "autofocus")
        plain = self.env["account.move"].with_user(self.accountant).get_views(
            [(False, "form")])["views"]["form"]["arch"]
        page = etree.fromstring(plain).xpath("//page[@name='apix_delivery']")[0]
        self.assertFalse(page.get("autofocus"))

    # -- Mark handled -------------------------------------------------------------------

    def test_marked_handled_leaves_and_a_later_failure_brings_it_back(self):
        move = self._make_invoice()
        attempt = self._send_outcome(move, "refused")
        self._mark(attempt, "Customer pays from the emailed PDF")
        attempt.invalidate_recordset()
        self.assertEqual(self._count(), 0)
        self.assertFalse(attempt.needs_attention)
        self.assertEqual(attempt.handled_by_id, self.accountant)
        self.assertEqual(attempt.handled_reason, "Customer pays from the emailed PDF")
        self.assertTrue(attempt.handled_date)
        self._send_outcome(move, "failed", "[send.no_answer] ZZ")
        self.assertEqual(self._count(), 1)

    def test_handled_then_success_stays_off(self):
        move = self._make_invoice()
        self._mark(self._send_outcome(move, "refused"), "ZZ handled")
        self._send_outcome(move, "accepted")
        self.assertEqual(self._count(), 0)

    def test_reason_required(self):
        attempt = self._send_outcome(self._make_invoice(), "refused")
        with self.assertRaises(UserError) as caught:
            attempt.with_user(self.accountant)._mark_handled("   ")
        self.assertEqual(caught.exception.code, "send.handled_reason_required")
        attempt.invalidate_recordset()
        self.assertTrue(attempt.needs_attention)

    def test_not_the_latest_is_refused(self):
        move = self._make_invoice()
        first = self._send_outcome(move, "refused")
        self._send_outcome(move, "failed", "[send.no_answer] ZZ")
        with self.assertRaises(UserError) as caught:
            first.with_user(self.accountant)._mark_handled("ZZ too late")
        self.assertEqual(caught.exception.code, "send.not_latest")

    def test_mark_handled_is_the_only_user_write(self):
        attempt = self._send_outcome(self._make_invoice(), "refused")
        with self.assertRaises(Exception):  # noqa: B017 - send.history_readonly
            attempt.with_user(self.accountant).write({"needs_attention": False})

    # -- two companies and who sees what ----------------------------------------

    def test_only_own_companies_are_counted_and_listed(self):
        self._send_outcome(self._make_invoice(), "refused")
        self._send_outcome(self._make_invoice(company=self.company_b), "refused")
        self.assertEqual(self._count(), 2)
        self.assertEqual(self._count(self.accountant_a, self.company), 1)
        action = self.env.ref("goodahead_invoice_send_apix.action_failed_sends")
        listed = self.env[ATTEMPT].with_user(self.accountant_a).with_context(
            allowed_company_ids=self.company.ids
        ).search(ast.literal_eval(action.domain or "[]"))
        self.assertEqual(listed.company_id, self.company)

    def test_accountant_sees_the_failed_send_not_the_connection_trouble(self):
        """AC-06."""
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        connection.sudo().write({"status": "rejected"})
        self._send_outcome(self._make_invoice(), "failed", "[send.credentials_refused] ZZ")
        data = self.env[ATTENTION].with_user(self.accountant).with_context(
            allowed_company_ids=self.company.ids
        )._attention_data()
        keys = [section["key"] for section in data["sections"]]
        self.assertIn("failed_sends", keys)
        self.assertNotIn("connection_trouble", keys)
