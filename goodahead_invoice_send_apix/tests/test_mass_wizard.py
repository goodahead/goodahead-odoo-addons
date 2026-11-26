# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The mass sending wizard: classify per company and transmit method, then
send now or in the background (send T34; AC-11 – AC-16, AC-40, AC-54,
AC-62; contract §3.1, §3.2; SCR-06 review / queued; sad.md Flow C, Flow G).

Nothing is sent here: the wizard queues; the jobs are T30's. Two companies,
invented data only.
"""

import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import UserError
from odoo.tests import Form, new_test_user, tagged

from .common import SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
WIZARD = "goodahead.invoice.send.wizard"
FIX = "goodahead.invoice.send.fix"


@tagged("post_install", "-at_install")
class TestMassWizard(SendApixCommon):
    def setUp(self):
        super().setUp()
        Connection = self.env["goodahead.apix.connection"]
        self.connection = Connection._apix_for_company(self.company)
        self.connection_b = Connection._apix_for_company(self.company_b)

    def _wizard(self, moves, user=None, companies=None):
        user = user or self.accountant
        companies = companies or (self.company | self.company_b)
        return self.env[WIZARD].with_user(user).with_context(
            active_model="account.move", active_ids=moves.ids,
            allowed_company_ids=companies.ids,
        ).create({})

    def _line(self, wizard, move):
        return wizard.line_ids.filtered(lambda line: line.move_id == move)

    def _no_address_customer(self):
        return self._make_customer("ZZ No Address Oy", business_id="0000004-3",
                                   invoice_sending_method="apix")

    # -- classification ---------------------------------------------------------

    def test_every_category(self):
        ready = self._make_invoice()
        not_ready = self._make_invoice(self._no_address_customer())
        already = self._make_invoice()
        self.env[ATTEMPT]._ledger_record(already, "email", "sent", "erp_send", self.accountant, {})
        in_progress = self._make_invoice()
        self.env[ATTEMPT]._ledger_claim(in_progress, "einvoice", "button", self.accountant)
        draft = self._make_invoice(post=False)
        skipped = self._make_invoice()
        Move = type(self.env["account.move"])
        original = Move._apix_channel

        def channel(move):
            return "download" if move.id == skipped.id else original(move)

        with patch.object(Move, "_apix_channel", channel):
            wizard = self._wizard(ready | not_ready | already | in_progress | draft | skipped)
        expected = {
            ready: ("ready", True),
            not_ready: ("not_ready", False),
            already: ("already_sent", False),
            in_progress: ("in_progress", False),
            draft: ("not_posted", False),
            skipped: ("skipped", False),
        }
        for move, (category, include) in expected.items():
            with self.subTest(category=category):
                line = self._line(wizard, move)
                self.assertEqual((line.category, line.include), (category, include))
        self.assertIn("e-invoice address", self._line(wizard, not_ready).missing)
        self.assertIn(self.accountant.name, self._line(wizard, already).sent_info)
        self.assertIn(self.accountant.name, self._line(wizard, in_progress).missing)
        self.assertEqual(wizard.included_count, 1)
        self.assertEqual(wizard.state, "review")

    def test_sent_before_the_add_on_is_left_out(self):
        """AC-62."""
        move = self._make_invoice()
        self.env.cr.execute(
            "UPDATE account_move SET sent_before_install = TRUE WHERE id = %s", (move.id,)
        )
        move.invalidate_recordset()
        line = self._line(self._wizard(move), move)
        self.assertEqual(line.category, "already_sent")
        self.assertFalse(line.include)
        self.assertIn("channel unknown", line.sent_info)

    def test_include_already_sent_after_the_warning(self):
        """AC-14: per line or all."""
        first, second = self._make_invoice(), self._make_invoice()
        for move in first | second:
            self.env[ATTEMPT]._ledger_record(move, "email", "sent", "erp_send",
                                             self.accountant, {})
        wizard = self._wizard(first | second)
        self.assertTrue(wizard.has_already_sent)
        wizard.action_include_already_sent(line_ids=self._line(wizard, first).ids)
        self.assertTrue(self._line(wizard, first).include)
        self.assertFalse(self._line(wizard, second).include)
        wizard.action_include_already_sent()
        self.assertTrue(self._line(wizard, second).include)
        wizard.action_send_background()
        attempts = self.env[ATTEMPT].search([("move_id", "in", (first | second).ids),
                                             ("state", "=", "queued")])
        self.assertEqual(len(attempts), 2, "sent again only after being included")

    def test_already_sent_line_cannot_be_ticked_in_the_list(self):
        """Review R6, AC-14: the list's include toggle is read-only on an
        already-sent line; only "Include" (with its warning) includes it."""
        ready, already = self._make_invoice(), self._make_invoice()
        self.env[ATTEMPT]._ledger_record(already, "email", "sent", "erp_send",
                                         self.accountant, {})
        wizard = self._wizard(ready | already)
        ids = wizard.line_ids.ids
        with Form(wizard) as form:
            with form.line_ids.edit(ids.index(self._line(wizard, already).id)) as line, \
                    self.assertRaises(AssertionError):
                line.include = True
            with form.line_ids.edit(ids.index(self._line(wizard, ready).id)) as line:
                line.include = False
        self.assertFalse(self._line(wizard, already).include)
        self.assertFalse(self._line(wizard, ready).include, "a ready line is still toggled")

    # -- two companies ------------------------------------------------

    def test_company_with_sending_off(self):
        """AC-15, AC-16: B is off — its e-invoice is not sendable, its email
        goes out; rows carry their company and channel."""
        self.connection_b.write({"sending_enabled": False})
        a_einvoice = self._make_invoice()
        a_letter = self._make_invoice(transmit_method="letter")
        b_einvoice = self._make_invoice(company=self.company_b)
        b_email = self._make_invoice(company=self.company_b, transmit_method="email")
        wizard = self._wizard(a_einvoice | a_letter | b_einvoice | b_email)
        self.assertEqual(self._line(wizard, b_einvoice).category, "sending_off")
        self.assertFalse(self._line(wizard, b_einvoice).include)
        self.assertEqual(self._line(wizard, b_email).category, "ready")
        self.assertEqual(self._line(wizard, b_email).channel, "email")
        self.assertEqual(self._line(wizard, a_letter).channel, "letter")
        self.assertEqual(self._line(wizard, b_email).company_id, self.company_b)
        action = wizard.action_send_background()
        queued = self.env[ATTEMPT].search([("run_uuid", "=", wizard.run_uuid)])
        self.assertEqual(queued.move_id, a_einvoice | a_letter | b_email)
        for attempt in queued.sudo():
            self.assertEqual(attempt.queue_job_id.company_id, attempt.move_id.company_id)
        self.assertIn("3", action["params"]["message"])

    def test_other_company_invoices_never_reach_the_wizard(self):
        """AC-54: an Accountant of company A only."""
        user_a = new_test_user(
            self.env, login="zz_mass_a_only", groups="account.group_account_invoice",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )
        move_a = self._make_invoice()
        move_b = self._make_invoice(company=self.company_b)
        wizard = self._wizard(move_a | move_b, user=user_a, companies=self.company)
        self.assertEqual(wizard.line_ids.move_id, move_a)
        wizard.action_send_background()
        self.assertFalse(self.env[ATTEMPT].search([("move_id", "=", move_b.id)]))

    def test_copy_warning_per_company(self):
        """AC-40."""
        with patch.object(type(self.env["goodahead.apix.connection"]), "_apix_copy_warning",
                          lambda connection: "ZZ copy warning"):
            wizard = self._wizard(self._make_invoice() | self._make_invoice(company=self.company_b))
            warning = wizard.copy_warning
        self.assertIn(self.company.name, warning)
        self.assertIn(self.company_b.name, warning)
        self.assertIn("ZZ copy warning", warning)

    # -- background recommendation ---------------------------------------------------------

    def test_background_recommended_above_the_threshold(self):
        moves = self.env["account.move"]
        for _index in range(20):
            moves |= self._make_invoice()
        wizard = self._wizard(moves)
        self.assertEqual(wizard.included_count, 20)
        self.assertFalse(wizard.recommend_background)
        moves |= self._make_invoice()
        wizard = self._wizard(moves)
        self.assertTrue(wizard.recommend_background, "21 > 20")

    def test_the_lowest_threshold_of_the_selection_counts(self):
        self.connection_b.sudo().write({"background_threshold": 2})
        moves = self._make_invoice() | self._make_invoice() | self._make_invoice(
            company=self.company_b)
        wizard = self._wizard(moves)
        self.assertEqual(wizard.background_threshold, 2)
        self.assertTrue(wizard.recommend_background)
        self.assertFalse(self._wizard(moves[:2]).recommend_background)

    # -- actions --------------------------------------------------------------------

    def test_send_now_queues_and_stays_open(self):
        """AC-11."""
        moves = self._make_invoice() | self._make_invoice()
        wizard = self._wizard(moves)
        action = wizard.action_send_now()
        self.assertEqual(action["res_model"], WIZARD)
        self.assertEqual(action["res_id"], wizard.id)
        self.assertEqual(action["target"], "new")
        self.assertEqual(wizard.state, "sending")
        attempts = wizard.line_ids.attempt_id
        self.assertEqual(len(attempts), 2)
        self.assertEqual(set(attempts.mapped("origin")), {"mass_now"})
        self.assertEqual(set(attempts.mapped("run_uuid")), {wizard.run_uuid})
        self.assertEqual(set(attempts.sudo().queue_job_id.mapped("priority")), {10})

    def test_send_in_background_tells_the_count_and_closes(self):
        """AC-12."""
        wizard = self._wizard(self._make_invoice() | self._make_invoice())
        action = wizard.action_send_background()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("2 invoices queued", action["params"]["message"])
        self.assertEqual(action["params"]["next"]["type"], "ir.actions.act_window_close")
        self.assertEqual(wizard.state, "queued")
        attempts = wizard.line_ids.attempt_id
        self.assertEqual(set(attempts.mapped("origin")), {"mass_background"})
        self.assertEqual(set(attempts.sudo().queue_job_id.mapped("priority")), {20})

    def test_closing_changes_nothing(self):
        """AC-11: closing the wizard leaves the queued sends running."""
        wizard = self._wizard(self._make_invoice())
        wizard.action_send_now()
        attempt = wizard.line_ids.attempt_id
        wizard.sudo().unlink()
        attempt.invalidate_recordset()
        self.assertEqual(attempt.state, "queued")
        self.assertEqual(attempt.sudo().queue_job_id.state, "pending")

    def test_invoice_started_elsewhere_meanwhile_is_in_progress(self):
        """Flow C: a send started after the wizard opened is reported, not
        queued; the others go."""
        first, second = self._make_invoice(), self._make_invoice()
        wizard = self._wizard(first | second)
        self.env[ATTEMPT]._ledger_claim(first, "einvoice", "button", self.accountant)
        wizard.action_send_now()
        self.assertEqual(self._line(wizard, first).category, "in_progress")
        self.assertFalse(self._line(wizard, first).attempt_id)
        self.assertEqual(self._line(wizard, second).attempt_id.state, "queued")

    def test_nothing_sendable_is_empty(self):
        wizard = self._wizard(self._make_invoice(self._no_address_customer()))
        self.assertEqual(wizard.included_count, 0)
        self.assertFalse(wizard.can_send)
        with self.assertRaises(UserError):
            wizard.action_send_now()
        with self.assertRaises(UserError):
            wizard.action_send_background()

    def test_fix_returns_and_reclassifies(self):
        """AC-13, Flow G: Fix opens SCR-07 for the row; saving there returns
        to the wizard with the row ready and included."""
        move = self._make_invoice(self._no_address_customer())
        wizard = self._wizard(move)
        line = self._line(wizard, move)
        action = line.action_open_fix()
        self.assertEqual(action["res_model"], FIX)
        fix = self.env[FIX].with_user(self.accountant).with_context(action["context"]).create({})
        self.assertEqual(fix.return_to, "mass_wizard")
        fix.write({"manual_edicode": "003700000043", "manual_operator_id": self.operator.id})
        back = fix.action_save()["params"]["next"]
        self.assertEqual((back["res_model"], back["res_id"]), (WIZARD, wizard.id))
        line.invalidate_recordset()
        self.assertEqual((line.category, line.include), ("ready", True))
