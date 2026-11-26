# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The delivery tab, the invoice list columns and filters, and readiness on
the invoice contact.

Two companies, invented data only; no APIX call.
"""

import unittest
import uuid

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests import Form, new_test_user, tagged

from ..models.goodahead_invoice_send_attempt import CANNOT_CANCEL
from .common import SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"


@tagged("post_install", "-at_install")
class TestDeliveryTab(SendApixCommon):
    def _as(self, records, user=None, companies=None):
        companies = companies or (self.company | self.company_b)
        return records.with_user(user or self.accountant).with_context(
            allowed_company_ids=companies.ids
        )

    def _arch(self, model, view_type, view_id=False):
        views = self.env[model].with_user(self.accountant).get_views([(view_id, view_type)])
        return etree.fromstring(views["views"][view_type]["arch"])

    def _record(self, move, channel="einvoice", state="accepted", origin="button", **values):
        return self._row(self.env[ATTEMPT]._ledger_record(move, channel, state, origin,
                                                          self.accountant, values))

    # -- SCR-02 delivery tab -------------------------------------------

    def test_delivery_tab_lists_every_attempt_newest_first(self):
        move = self._make_invoice()
        refused = self._record(move, state="refused", reason="[send.refused] ZZ refused")
        sent = self._record(move, apix_batch_id="ZZBATCH-1", apix_document_id="ZZDOC-1",
                            apix_credits=1.0, warning_text="ZZ warning")
        download = self._record(move, channel="download", state="downloaded", origin="send_print")
        move = self._as(move)
        self.assertEqual(move.send_attempt_ids.ids, (download | sent | refused).ids)
        row = move.send_attempt_ids.filtered(lambda a: a.id == sent.id)
        self.assertTrue(row.counts_as_sent)
        self.assertEqual((row.apix_batch_id, row.apix_document_id, row.apix_credits),
                         ("ZZBATCH-1", "ZZDOC-1", 1.0))
        self.assertEqual(row.started_by_label, self.accountant.name)
        self.assertTrue(row.display_date)
        self.assertFalse(move.send_attempt_ids.filtered(lambda a: a.id == download.id).counts_as_sent)
        self.assertFalse(move.delivery_empty)

    def test_background_rows_name_who_queued(self):
        move = self._make_invoice()
        attempt = self._as(move)._apix_queue(str(uuid.uuid4()), "mass_background", 20)["queued"]
        self.assertIn("background", attempt.started_by_label)
        self.assertIn(self.accountant.name, attempt.started_by_label)

    def test_form_carries_the_delivery_page(self):
        tree = self._arch("account.move", "form")
        page = tree.xpath("//page[@name='apix_delivery']")
        self.assertTrue(page)
        rows = page[0].xpath(".//field[@name='send_attempt_ids']")
        self.assertTrue(rows)
        self.assertEqual(rows[0].get("readonly"), "1")
        cancel = page[0].xpath(".//button[@name='action_cancel']")
        self.assertTrue(cancel)
        self.assertIn("queued", cancel[0].get("invisible"))
        self.assertTrue(page[0].xpath(".//field[@name='sent_before_install']"))
        Form(self._as(self._make_invoice()))

    def test_empty_and_sent_before_the_add_on(self):
        """AC-62: the pre-install mark is a banner line, not a row."""
        move = self._make_invoice()
        self.assertTrue(self._as(move).delivery_empty)
        self.env.cr.execute(
            "UPDATE account_move SET sent_before_install = TRUE WHERE id = %s", (move.id,)
        )
        move.invalidate_recordset()
        move = self._as(move)
        self.assertFalse(move.delivery_empty)
        self.assertFalse(move.send_attempt_ids)
        self.assertTrue(move.sent_before_install)

    def test_rows_cannot_be_changed_from_the_invoice(self):
        """AC-32."""
        move = self._make_invoice()
        attempt = self._record(move)
        with self.assertRaises(AccessError):
            self._as(move).write({"send_attempt_ids": [(1, attempt.id, {"reason": "ZZ edited"})]})
        with self.assertRaises(AccessError):
            self._as(move).write({"send_attempt_ids": [(2, attempt.id, 0)]})

    def test_cancel_from_the_row_and_refused_once_running(self):
        """AC-58."""
        move = self._make_invoice()
        attempt = self._as(move)._apix_queue(str(uuid.uuid4()), "mass_background", 20)["queued"]
        row = self._as(move).send_attempt_ids
        row.action_cancel()
        row.invalidate_recordset()
        self.assertEqual(row.state, "cancelled")
        self.assertEqual(row.cancelled_by_id, self.accountant)
        second = self._as(move)._apix_queue(str(uuid.uuid4()), "mass_background", 20)["queued"]
        self.env[ATTEMPT]._ledger_start(second)
        with self.assertRaises(Exception) as caught:
            self._as(second).action_cancel()
        self.assertEqual(getattr(caught.exception, "code", None), CANNOT_CANCEL)
        self.assertNotEqual(attempt, second)

    def test_attempt_views_are_read_only(self):
        for view_type in ("list", "form"):
            with self.subTest(view_type=view_type):
                tree = self._arch(ATTEMPT, view_type)
                self.assertEqual(tree.get("create"), "0")
                self.assertEqual(tree.get("edit"), "0")
                self.assertEqual(tree.get("delete"), "0")

    # -- SCR-05 invoice list (filters, two companies) -------------------------------------------------

    def test_list_columns_and_filters(self):
        tree = self._arch("account.move", "list", self.env.ref("account.view_invoice_tree").id)
        for name in ("transmit_method", "send_state"):
            with self.subTest(column=name):
                field = tree.xpath(f"//field[@name='{name}']")
                self.assertTrue(field)
                self.assertEqual(field[0].get("optional"), "show")
        search = self._arch("account.move", "search",
                            self.env.ref("account.view_account_invoice_filter").id)
        for name in ("apix_ready", "apix_not_ready", "apix_failed", "apix_not_sent"):
            with self.subTest(filter=name):
                self.assertTrue(search.xpath(f"//filter[@name='{name}']"))

    def test_send_state_search(self):
        ready = self._make_invoice()
        not_ready = self._make_invoice(self._make_customer(
            "ZZ No Address Oy", business_id="0000004-3", invoice_sending_method="apix"))
        sent = self._make_invoice()
        self._record(sent)
        failed = self._make_invoice()
        self._record(failed, state="failed", reason="[send.no_answer] ZZ")
        running = self._make_invoice()
        self.env[ATTEMPT]._ledger_claim(running, "einvoice", "button", self.accountant)
        draft = self._make_invoice(post=False)
        everything = ready | not_ready | sent | failed | running | draft
        Move = self._as(self.env["account.move"])

        def found(domain):
            return Move.search(domain + [("id", "in", everything.ids)])

        self.assertEqual(found([("send_state", "=", "ready")]), ready)
        self.assertEqual(found([("send_state", "=", "not_ready")]), not_ready)
        self.assertEqual(found([("send_state", "=", "sent")]), sent)
        self.assertEqual(found([("send_state", "in", ("refused", "failed"))]), failed)
        self.assertEqual(found([("send_state", "=", "in_progress")]), running)
        self.assertEqual(found([("already_sent", "=", False), ("state", "=", "posted")]),
                         ready | not_ready | failed | running)

    def test_send_state_search_matches_the_badge(self):
        """Review R9: the bounded searches find exactly the invoices whose
        badge shows that state, also when outcomes follow one another."""
        Attempt = self.env[ATTEMPT]
        moves = self.env["account.move"]
        for _i in range(2):
            moves |= self._make_invoice()
        sent_then_failed = self._make_invoice()
        self._record(sent_then_failed)
        self._record(sent_then_failed, state="refused", reason="[send.refused] ZZ")
        failed_then_sent = self._make_invoice()
        self._record(failed_then_sent, state="failed", reason="[send.no_answer] ZZ")
        self._record(failed_then_sent)
        before_install = self._make_invoice()
        self.env.cr.execute("UPDATE account_move SET sent_before_install = TRUE WHERE id = %s",
                            [before_install.id])
        failed_then_running = self._make_invoice()
        self._record(failed_then_running, state="failed", reason="[send.no_answer] ZZ")
        Attempt._ledger_claim(failed_then_running, "einvoice", "button", self.accountant)
        downloaded_after_failure = self._make_invoice()
        self._record(downloaded_after_failure, state="failed", reason="[send.no_answer] ZZ")
        Attempt._ledger_record(downloaded_after_failure, "download", "downloaded", "send_print",
                               self.accountant, {})
        not_ready = self._make_invoice(self._make_customer(
            "ZZ No Address Oy", business_id="0000004-3", invoice_sending_method="apix"))
        moves |= (sent_then_failed | failed_then_sent | before_install | failed_then_running
                  | downloaded_after_failure | not_ready | self._make_invoice(post=False))
        moves.invalidate_recordset()
        Move = self._as(self.env["account.move"])
        badges = {move.id: move.send_state for move in self._as(moves)}
        self.assertEqual(badges[downloaded_after_failure.id], "failed")
        for state in ("ready", "not_ready", "in_progress", "sent", "refused", "failed"):
            with self.subTest(state=state):
                found = Move.search([("send_state", "=", state), ("id", "in", moves.ids)])
                self.assertEqual(set(found.ids), {i for i, badge in badges.items() if badge == state})
        found = Move.search([("send_state", "in", ("ready", "sent")), ("id", "in", moves.ids)])
        self.assertEqual(set(found.ids), {i for i, b in badges.items() if b in ("ready", "sent")})

    def test_other_companies_never_appear(self):
        """AC-54 on SCR-05: an Accountant of company A only."""
        user_a = new_test_user(
            self.env, login="zz_list_a_only", groups="account.group_account_invoice",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )
        move_a = self._make_invoice()
        move_b = self._make_invoice(company=self.company_b)
        Move = self.env["account.move"].with_user(user_a).with_context(
            allowed_company_ids=self.company.ids
        )
        found = Move.search([("send_state", "=", "ready"), ("id", "in", (move_a | move_b).ids)])
        self.assertEqual(found, move_a)
        with self.assertRaises(AccessError):
            move_b.with_user(user_a).send_attempt_ids.read(["state"])

    # -- SCR-08 invoice contact ----------------------------------------------------------------

    def test_contact_readiness_banner(self):
        customer = self._as(self.customer)
        self.assertEqual(customer.send_readiness_level, "ready")
        self.assertIn("Ready to receive e-invoices", customer.send_readiness_html)
        missing = self._as(self._make_customer("ZZ No Address Oy", business_id="0000004-3",
                                               invoice_sending_method="apix"))
        self.assertEqual(missing.send_readiness_level, "not_ready")
        self.assertIn("e-invoice address", missing.send_readiness_html)
        email = self._as(self._make_customer("ZZ Mail Oy", email=False,
                                             invoice_sending_method="email"))
        self.assertEqual(email.send_readiness_level, "not_ready")
        self.assertIn("email", email.send_readiness_html.lower())
        tree = self._arch("res.partner", "form")
        self.assertTrue(tree.xpath("//field[@name='send_readiness_html']"))
