# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The invoice form: "Send via APIX", readiness, outcomes and the
already-sent confirmation (send T29; AC-07, AC-18, AC-28, AC-31, AC-37,
AC-40; contract §1.1 ``action_apix_send``, SCR-01, SCR-03).

``FakeApixSendClient`` stands in for SendInvoiceZIP: no network. Invented
data only.
"""

import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree  # noqa: E402

from odoo.tests import Form, new_test_user, tagged  # noqa: E402

from .common import BATCH_ID, FakeApixSendClient, SendApixCommon  # noqa: E402

ATTEMPT = "goodahead.invoice.send.attempt"
ALREADY_SENT_WIZARD = "goodahead.invoice.send.already.sent"
FIX = "goodahead.invoice.send.fix"


@tagged("post_install", "-at_install")
class TestInvoiceForm(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin_user = new_test_user(
            cls.env,
            login="zz_form_admin",
            groups="account.group_account_manager",
            company_id=cls.company.id,
            company_ids=[(6, 0, (cls.company | cls.company_b).ids)],
        )

    def setUp(self):
        super().setUp()
        self.connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        self.connection_b = self.env["goodahead.apix.connection"]._apix_for_company(self.company_b)

    def _as(self, move, user=None):
        return move.with_user(user or self.accountant).with_context(
            allowed_company_ids=[self.company.id, self.company_b.id]
        )

    def _attempts(self, move):
        return self.env[ATTEMPT].search([("move_id", "=", move.id)], order="id")

    def _params(self, action):
        self.assertEqual(action["type"], "ir.actions.client", action)
        self.assertEqual(action["tag"], "display_notification", action)
        return action["params"]

    # -- the button and the readiness line -----------------------------------------

    def test_ready_invoice_offers_the_primary_button(self):
        move = self._as(self._make_invoice())
        self.assertTrue(move.apix_send_available)
        self.assertEqual(move.send_readiness_level, "ready")
        self.assertIn("Ready to send as e-invoice", move.send_readiness_html)
        self.assertEqual(move.send_state, "ready")

    def test_email_invoice_offers_no_apix_button(self):
        """Review R10, AC-26: an invoice that goes by email is sent with
        Send & Print; "Send via APIX" is not offered on it."""
        move = self._as(self._make_invoice(transmit_method="email"))
        self.assertTrue(move.send_ready, "ready for email")
        self.assertFalse(move.apix_send_available)
        move.write({"transmit_method": "letter"})
        move.invalidate_recordset()
        self.assertTrue(move.apix_send_available)

    def test_form_view_carries_the_sending_elements(self):
        move = self._as(self._make_invoice())
        arch = move.get_views([(False, "form")])["views"]["form"]["arch"]
        tree = etree.fromstring(arch)
        buttons = tree.xpath("//header/button[@name='action_apix_send']")
        self.assertEqual(len(buttons), 2, "primary when ready, secondary otherwise")
        self.assertTrue(tree.xpath("//field[@name='transmit_method']"))
        self.assertTrue(tree.xpath("//field[@name='send_copy_warning']"))
        self.assertTrue(tree.xpath("//field[@name='send_in_progress_info']"))
        self.assertTrue(tree.xpath("//field[@name='send_readiness_html']"))
        # the form opens with the extension in place
        Form(move)

    def test_ready_with_warnings_lists_the_warnings(self):
        move = self._as(self._make_invoice(lines=[{
            "name": "ZZ pallets", "quantity": 2, "price_unit": 10.0,
            "product_uom_id": self.unit_without_code.id,
        }]))
        self.assertTrue(move.send_ready)
        self.assertEqual(move.send_readiness_level, "warning")
        self.assertIn("ZZ pallet", move.send_readiness_html)

    def test_not_ready_lists_each_item_and_where_it_is_fixed(self):
        customer = self._make_customer("ZZ No Address Oy", business_id="0000004-3",
                                       invoice_sending_method="apix")
        move = self._as(self._make_invoice(customer))
        self.assertEqual(move.transmit_method, "einvoice")
        self.assertEqual(move.send_readiness_level, "not_ready")
        self.assertIn("Not ready to send as e-invoice", move.send_readiness_html)
        self.assertIn("e-invoice address", move.send_readiness_html)
        self.assertEqual(move.send_state, "not_ready")

    def test_sending_off_shows_no_apix_action(self):
        """AC-28."""
        self.connection.write({"sending_enabled": False})
        move = self._as(self._make_invoice())
        self.assertFalse(move.apix_send_available)
        self.assertEqual(move.send_readiness_level, "off")
        self.assertIn("not switched on", move.send_readiness_html)

    def test_switching_sending_on_shows_the_button_for_that_company_only(self):
        """AC-37."""
        self.connection.write({"sending_enabled": False})
        self.connection_b.write({"sending_enabled": False})
        move_a = self._as(self._make_invoice())
        move_b = self._as(self._make_invoice(company=self.company_b))
        self.assertFalse(move_a.apix_send_available)
        self.connection.with_user(self.admin_user).action_apix_sending_on()
        move_a.invalidate_recordset()
        move_b.invalidate_recordset()
        self.assertTrue(move_a.apix_send_available)
        self.assertFalse(move_b.apix_send_available)

    def test_draft_and_cancelled_have_no_button_and_are_refused(self):
        """AC-07."""
        draft = self._as(self._make_invoice(post=False))
        self.assertFalse(draft.apix_send_available)
        self.assertFalse(draft.send_readiness_level)
        params = self._params(draft.action_apix_send())
        self.assertEqual(params["type"], "danger")
        self.assertIn("only posted, not cancelled", params["message"])
        cancelled = self._make_invoice()
        cancelled.button_draft()
        cancelled.button_cancel()
        cancelled = self._as(cancelled)
        self.assertFalse(cancelled.apix_send_available)
        params = self._params(cancelled.action_apix_send())
        self.assertIn("only posted, not cancelled", params["message"])
        self.assertFalse(self._attempts(draft))
        self.assertFalse(self._attempts(cancelled))

    def test_copy_warning_shows_and_never_blocks(self):
        """AC-40."""
        fake = FakeApixSendClient("accepted").patch(self)
        with patch.object(type(self.env["goodahead.apix.connection"]), "_apix_copy_warning",
                          lambda connection: "ZZ copy warning"):
            move = self._as(self._make_invoice())
            self.assertEqual(move.send_copy_warning, "ZZ copy warning")
            params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "success")
        self.assertEqual(len(fake.calls), 1)

    # -- outcomes --------------------------------------------------------------------

    def test_accepted_is_a_sticky_success_and_reloads(self):
        FakeApixSendClient("accepted").patch(self)
        move = self._as(self._make_invoice())
        params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "success")
        self.assertTrue(params["sticky"])
        self.assertIn("Sent to APIX", params["message"])
        self.assertIn(BATCH_ID, params["message"])
        self.assertEqual(params["next"]["tag"], "soft_reload")
        self.assertEqual(self._attempts(move).state, "accepted")
        move.invalidate_recordset()
        self.assertEqual(move.send_state, "sent")

    def test_accepted_with_warning(self):
        FakeApixSendClient("warning", text="ZZ buyer VAT code is missing").patch(self)
        move = self._as(self._make_invoice())
        params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "warning")
        self.assertIn("ZZ buyer VAT code is missing", params["message"])

    def test_letter_goes_to_the_print_service(self):
        FakeApixSendClient("accepted").patch(self)
        move = self._as(self._make_invoice(transmit_method="letter"))
        params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "success")
        self.assertIn("print service", params["message"])

    def test_refused_is_a_sticky_danger_with_the_reason(self):
        FakeApixSendClient("refused", text="ZZ invalid address").patch(self)
        move = self._as(self._make_invoice())
        params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "danger")
        self.assertTrue(params["sticky"])
        self.assertIn("ZZ invalid address", params["message"])
        move.invalidate_recordset()
        self.assertEqual(move.send_state, "refused")
        self.assertTrue(move.apix_send_available, "the button is offered again")

    def test_no_answer_says_it_may_already_be_at_apix(self):
        FakeApixSendClient("no_answer").patch(self)
        move = self._as(self._make_invoice())
        params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "danger")
        self.assertIn("may already have reached APIX", params["message"])

    def test_connection_failure_wording_by_role(self):
        """AC-06 wording on SCR-01: Accountants get the general text,
        Administrators the specific reason."""
        FakeApixSendClient("err", status_code="2004").patch(self)
        move = self._as(self._make_invoice())
        params = self._params(move.action_apix_send())
        self.assertIn("connection is not working", params["message"])
        self.assertNotIn("transfer credentials", params["message"])
        self.connection.sudo().write({
            "status": "working", "send_paused_until": False, "status_message": False,
        })
        move_admin = self._as(self._make_invoice(), self.admin_user)
        params = self._params(move_admin.action_apix_send())
        self.assertIn("transfer credentials", params["message"])

    def test_not_ready_refusal_opens_the_fix_it_wizard(self):
        """AC-18: nothing sent, the missing items named, the fix-it wizard
        next; never another channel."""
        fake = FakeApixSendClient("accepted").patch(self)
        customer = self._make_customer("ZZ No Address Oy", business_id="0000004-3",
                                       invoice_sending_method="apix")
        move = self._as(self._make_invoice(customer))
        params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "danger")
        self.assertIn("e-invoice address", params["message"])
        self.assertEqual(params["next"]["res_model"], FIX)
        self.assertEqual(params["next"]["context"]["default_move_id"], move.id)
        self.assertFalse(fake.calls)
        self.assertFalse(self._attempts(move))

    def test_double_click_second_is_in_progress(self):
        """Edge case: the second click loses the claim, one attempt."""
        move = self._as(self._make_invoice())
        self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.accountant)
        params = self._params(move.action_apix_send())
        self.assertEqual(params["type"], "warning")
        self.assertIn("already in progress", params["message"])
        self.assertEqual(len(self._attempts(move)), 1)
        move.invalidate_recordset()
        self.assertEqual(move.send_state, "in_progress")
        self.assertTrue(move.send_in_progress_info)

    # -- already sent ------------------------------------------------------

    def _open_already_sent(self, move):
        action = move.action_apix_send()
        self.assertEqual(action["type"], "ir.actions.act_window", action)
        self.assertEqual(action["res_model"], ALREADY_SENT_WIZARD)
        self.assertEqual(action["target"], "new")
        return self.env[ALREADY_SENT_WIZARD].with_user(self.accountant).with_context(
            action["context"]
        ).create({})

    def test_already_sent_asks_first_then_sends_again(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._as(self._make_invoice())
        move.action_apix_send()
        self.assertEqual(len(fake.calls), 1)
        wizard = self._open_already_sent(move)
        self.assertEqual(len(fake.calls), 1, "nothing sent before the confirmation")
        self.assertIn("already sent", wizard.message)
        self.assertIn("E-invoice", wizard.message)
        params = self._params(wizard.action_send_again())
        self.assertEqual(params["type"], "success")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(self._attempts(move)), 2, "the resend is a new history entry")

    def test_already_sent_declined_sends_nothing(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._as(self._make_invoice())
        move.action_apix_send()
        self._open_already_sent(move)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(self._attempts(move)), 1)

    def test_sent_before_the_add_on_wording(self):
        """AC-62."""
        move = self._make_invoice()
        self.env.cr.execute(
            "UPDATE account_move SET sent_before_install = TRUE WHERE id = %s", (move.id,)
        )
        move.invalidate_recordset()
        wizard = self._open_already_sent(self._as(move))
        self.assertIn("before APIX sending was installed", wizard.message)
        self.assertIn("channel unknown", wizard.message)

    def test_sent_by_email_wording(self):
        move = self._make_invoice()
        self.env[ATTEMPT]._ledger_record(move, "email", "sent", "erp_send", self.accountant, {})
        wizard = self._open_already_sent(self._as(move))
        self.assertIn("Email", wizard.message)
        self.assertIn(self.accountant.name, wizard.message)
