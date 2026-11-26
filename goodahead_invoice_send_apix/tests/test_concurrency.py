# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""One send at a time per invoice, across every door (send T39; AC-08;
spec §6 "One send at a time"; sad.md §10 QG-1).

The doors — the button, Send & Print (APIX or email), the mass wizard, a
background job, a transmit-method change — each meet a send already in
progress and are refused with who started it and when; once it ended the
invoice can be sent again. ``TestConcurrencyRealDatabase`` races a user's
claim against a background queueing on two real connections (the ledger's
own claim / start / cancel races are in ``test_ledger``). Invented data
only; no network.
"""

import threading
import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import UserError
from odoo.modules.registry import DummyRLock, Registry
from odoo.tests import TransactionCase, new_test_user, tagged

from ..models import ledger
from ..models.ledger import Holder
from .common import FakeApixSendClient, SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
WIZARD = "goodahead.invoice.send.wizard"


@tagged("post_install", "-at_install")
class TestOneSendAtATime(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.colleague = new_test_user(
            cls.env, login="zz_conc_colleague", name="ZZ Colleague",
            groups="account.group_account_invoice",
            company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)],
        )

    def setUp(self):
        super().setUp()
        self.fake = FakeApixSendClient("accepted").patch(self)

    def _queued_by_colleague(self, move):
        """A background send of ``move`` queued by the colleague."""
        result = move.with_user(self.colleague).with_context(
            allowed_company_ids=self.company.ids)._apix_queue("zz-run", "mass_background", 20)
        return result["queued"]

    def _assert_names_the_holder(self, text):
        self.assertIn("ZZ Colleague", text)

    def test_every_door_refuses_while_a_send_is_queued(self):
        move = self._make_invoice()
        attempt = self._queued_by_colleague(move)
        me = move.with_user(self.accountant)
        # the button
        result = me._apix_send_now("button")
        self.assertEqual(result.code, "send.in_progress")
        self._assert_names_the_holder(result.message)
        # a second claim (Send & Print, a job) gets the holder with who and when
        holder = self.env[ATTEMPT]._ledger_claim(move, "einvoice", "send_print", self.accountant)
        self.assertIsInstance(holder, Holder)
        self.assertEqual(holder.user_id, self.colleague.id)
        self.assertTrue(holder.since)
        # the ERP's email
        with self.assertRaises(UserError) as caught:
            self.env["account.move.send"].with_user(self.accountant)._generate_and_send_invoices(
                me, sending_methods=["email"])
        self.assertEqual(caught.exception.code, "send.in_progress")
        # the mass wizard
        wizard = self.env[WIZARD].with_user(self.accountant).with_context(
            active_model="account.move", active_ids=move.ids,
            allowed_company_ids=self.company.ids).create({})
        self.assertEqual(wizard.line_ids.category, "in_progress")
        self._assert_names_the_holder(wizard.line_ids.missing)
        # the transmit method
        with self.assertRaises(UserError) as caught:
            me.write({"transmit_method": "email"})
        self.assertEqual(caught.exception.code, "send.in_progress")
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.env[ATTEMPT].search_count([("move_id", "=", move.id)]), 1)
        # once it ended, the invoice is free again
        attempt.with_user(self.colleague).action_cancel()
        self.assertEqual(me._apix_send_now("button").outcome, "accepted")

    def test_queueing_an_invoice_that_is_being_sent_reports_the_holder(self):
        move = self._make_invoice()
        self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.colleague)
        result = move.with_user(self.accountant)._apix_queue("zz-run", "mass_now", 10)
        self.assertFalse(result["queued"])
        self.assertEqual(result["in_progress"][move.id].user_id, self.colleague.id)


@tagged("post_install", "-at_install")
class TestConcurrencyRealDatabase(TransactionCase):
    """A user's claim and a background queueing of the same invoice on two
    real connections: exactly one wins, the other learns who holds it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.registry_ = cls.registry
        with cls.registry_.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            journal = env["account.journal"].create({
                "name": "ZZ Race Journal", "code": "ZZRCE", "type": "general",
                "company_id": env.ref("base.main_company").id,
            })
            cls.journal_id = journal.id
            cls.move_ids = env["account.move"].create([
                {"journal_id": journal.id, "move_type": "entry", "ref": f"ZZ race {i}"}
                for i in range(3)
            ]).ids
            # one more, locked by the lock-timeout test only
            cls.locked_move_id = env["account.move"].create(
                {"journal_id": journal.id, "move_type": "entry", "ref": "ZZ locked"}
            ).id
            cls.move_ids_all = [*cls.move_ids, cls.locked_move_id]
        cls.addClassCleanup(cls._remove_committed_data)

    @classmethod
    def _remove_committed_data(cls):
        with cls.registry_.cursor() as cr:
            cr.execute("DELETE FROM account_move WHERE id = ANY(%s)", [cls.move_ids_all])
            cr.execute("DELETE FROM account_journal WHERE id = %s", [cls.journal_id])

    def setUp(self):
        super().setUp()
        for patcher in (patch.object(ledger, "FORCE_OWN_CURSOR", True),
                        patch.object(Registry, "_lock", DummyRLock())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _race(self, *functions):
        barrier = threading.Barrier(len(functions))
        results = [None] * len(functions)
        errors = []

        def run(index, function):
            try:
                with self.registry_.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    barrier.wait(timeout=10)
                    results[index] = function(env)
            except Exception as error:  # noqa: BLE001 - reported in the test thread
                errors.append(error)

        threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(functions)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse([t for t in threads if t.is_alive()], "a racing connection hung")
        self.assertFalse(errors, errors)
        return results

    def test_claim_while_another_transaction_locks_the_invoice(self):
        """Review R8: another transaction holds the invoice row FOR UPDATE
        (the ERP's batch cron, an email being sent); the claim waits for the
        ledger's lock timeout and answers "in progress" — no database error,
        no attempt left behind."""
        move_id = self.locked_move_id
        count = "SELECT count(*) FROM goodahead_invoice_send_attempt WHERE move_id = %s"
        with self.registry_.cursor() as cr:
            cr.execute(count, [move_id])
            before = cr.fetchone()[0]
        with patch.object(ledger, "LOCK_TIMEOUT", "300ms"), \
                self.registry_.cursor() as holder_cr, self.registry_.cursor() as cr:
            holder_cr.execute("SELECT id FROM account_move WHERE id = %s FOR UPDATE", [move_id])
            env = api.Environment(cr, SUPERUSER_ID, {})
            result = env[ATTEMPT]._ledger_claim(
                env["account.move"].browse(move_id), "einvoice", "button", env.user)
            holder_cr.rollback()
        self.assertIsInstance(result, Holder)
        self.assertEqual(result.code, "send.in_progress")
        with self.registry_.cursor() as cr:
            cr.execute(count, [move_id])
            self.assertEqual(cr.fetchone()[0], before, "no attempt left behind")

    def test_user_claim_against_background_queueing(self):
        for move_id in self.move_ids:
            with self.subTest(move_id=move_id):
                def claim(env, move_id=move_id):
                    result = env[ATTEMPT]._ledger_claim(
                        env["account.move"].browse(move_id), "einvoice", "button", env.user)
                    return "holder" if isinstance(result, Holder) else "won"

                def queue(env, move_id=move_id):
                    attempt_id = env[ATTEMPT]._ledger()._ledger_insert(
                        "queued", env["account.move"].browse(move_id), "einvoice",
                        "mass_background", env.user, "zz-race", fields.Datetime.now())
                    if attempt_id:
                        return "won"
                    # as the queueing does: the holder read fresh (R8)
                    holder = env[ATTEMPT]._ledger_holder_now(env["account.move"].browse(move_id))
                    return "holder" if holder else "lost-without-holder"

                outcomes = self._race(claim, queue)
                self.assertEqual(sorted(outcomes), ["holder", "won"])
                with self.registry_.cursor() as cr:
                    cr.execute(
                        "SELECT count(*) FROM goodahead_invoice_send_attempt"
                        " WHERE move_id = %s AND state IN ('queued', 'running')", [move_id])
                    self.assertEqual(cr.fetchone()[0], 1, "never two in progress")
