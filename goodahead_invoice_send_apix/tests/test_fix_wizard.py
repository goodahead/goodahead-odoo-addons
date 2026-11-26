# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The fix-it wizard (send T25; AC-20 – AC-25, AC-61; contract §4, SCR-07).

APIX is never called: the client's AddressQuery and DeliveryMethod are
patched. Every name, address and identifier here is invented.
"""

import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import AccessError  # noqa: E402
from odoo.tests import new_test_user, tagged  # noqa: E402

from odoo.addons.goodahead_apix.lib.apix_client import (  # noqa: E402
    ApixClient,
    ApixRejected,
    ApixUnreachable,
)

from ..wizard.send_fix import (  # noqa: E402
    ADDRESS_REQUIRED,
    DELIVERY_METHOD_UNAVAILABLE,
    LOOKUP_INPUT_REQUIRED,
    LOOKUP_UNAVAILABLE,
    NONE_FOUND,
    NOTHING_PICKED,
    OPERATOR_NOT_LISTED,
    TRANSMIT_NOT_SWITCHED,
    FixError,
)
from .common import SendApixCommon  # noqa: E402

FIX = "goodahead.invoice.send.fix"
ATTEMPT = "goodahead.invoice.send.attempt"


def _found(edicode, operator, name="ZZ Plain Customer Oy", business_id="0000004-1"):
    return {"receiver_name": name, "business_id": business_id, "edicode": edicode,
            "operator": operator}


@tagged("post_install", "-at_install")
class TestFixWizard(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plain = cls._make_customer("ZZ Plain Customer Oy", business_id="0000004-1")
        cls.other_operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Other Operator", "identifier": "ZZOTHER"}
        )

    def setUp(self):
        super().setUp()
        self.move = self._make_invoice(customer=self.plain)
        self.assertEqual(self.move.transmit_method, "email")

    def _wizard(self, move=None, **values):
        move = move or self.move
        return self.env[FIX].with_user(self.accountant).with_context(
            tracking_disable=False, mail_notrack=False,
            allowed_company_ids=[self.company.id, self.company_b.id],
        ).create(dict({"move_id": move.id}, **values))

    def _lookup(self, wizard, answer=None, error=None):
        with patch.object(ApixClient, "address_query", autospec=True,
                          return_value=answer, side_effect=error) as query:
            wizard.action_lookup()
        return query

    def _code(self, call):
        with self.assertRaises(FixError) as caught:
            call()
        return caught.exception.code

    # -- default -----------------------------------------------------------

    def test_defaults_and_shared_contact_notice(self):
        desk = self.env["res.partner"].create(
            {"name": "ZZ Plain Billing", "type": "invoice", "parent_id": self.plain.id}
        )
        wizard = self._wizard(self._make_invoice(customer=desk))
        self.assertEqual(wizard.partner_id, desk)
        self.assertEqual(wizard.lookup_business_id, "0000004-1")
        self.assertEqual(wizard.lookup_name, "ZZ Plain Customer Oy")
        self.assertIn("ZZ Plain Billing", wizard.notice)
        self.assertIn("every company", wizard.notice)

    # -- lookup ------------------------------------------------------

    def test_several_results_are_listed_none_picked_nothing_saved(self):
        wizard = self._wizard()
        query = self._lookup(wizard, [
            _found("003700000041", "ZZOPER"), _found("003700000042", "ZZOTHER"),
        ])
        args = query.call_args
        self.assertEqual(args.kwargs.get("business_id"), "0000004-1")
        self.assertEqual(len(wizard.result_ids), 2)
        self.assertEqual(wizard.result_ids.mapped("operator_id"), self.operator | self.other_operator)
        self.assertFalse(wizard.picked_result_id)
        self.assertFalse(self.plain.edicode)
        self.assertEqual(self._code(wizard.action_save), NOTHING_PICKED)
        self.assertFalse(self.plain.edicode)

    def test_pick_and_save_to_the_invoice_contact(self):
        wizard = self._wizard()
        self._lookup(wizard, [_found("003700000041", "ZZOPER")])
        wizard.result_ids[0].action_use()
        self.assertEqual(wizard.picked_result_id, wizard.result_ids[0])
        # the contact was created in this transaction: end its creation
        # before the change, or the ERP folds the change into it untracked
        self.env.cr.flush()
        wizard.action_save()
        # the tracking is written when the transaction's pre-commit runs
        self.env.flush_all()
        self.env.cr.flush()
        self.plain.invalidate_recordset(["message_ids"])
        self.assertEqual(self.plain.edicode, "003700000041")
        self.assertEqual(self.plain.einvoice_operator_id, self.operator)
        tracked = self.plain.message_ids.tracking_value_ids.filtered(
            lambda value: value.field_id.name == "edicode"
        )
        self.assertTrue(tracked)
        self.assertEqual(tracked[0].new_value_char, "003700000041")
        self.assertEqual(tracked[0].mail_message_id.author_id, self.accountant.partner_id)
        # the invoice switches to e-invoice and is ready now
        self.move.invalidate_recordset()
        self.assertEqual(self.move.transmit_method, "einvoice")
        self.assertTrue(self.move.send_ready, self.move.send_readiness)
        # the Accountant can still change it by hand afterwards
        self.move.with_user(self.accountant).transmit_method = "email"
        self.assertEqual(self.move.transmit_method, "email")

    def test_save_goes_to_the_invoice_contact_not_the_commercial_customer(self):
        desk = self.env["res.partner"].create(
            {"name": "ZZ Plain Billing", "type": "invoice", "parent_id": self.plain.id}
        )
        wizard = self._wizard(self._make_invoice(customer=desk))
        self._lookup(wizard, [_found("003700000041", "ZZOPER")])
        wizard.result_ids.action_use()
        wizard.action_save()
        self.assertEqual(desk.edicode, "003700000041")
        self.assertFalse(self.plain.edicode)

    def test_none_found_offers_entry_by_hand(self):
        wizard = self._wizard()
        self._lookup(wizard, [])
        self.assertEqual(wizard.lookup_state, NONE_FOUND)
        self.assertIn("0000004-1", wizard.lookup_message)
        wizard.write({"manual_edicode": "003700000043", "manual_operator_id": self.operator.id})
        wizard.action_save()
        self.assertEqual(self.plain.edicode, "003700000043")

    def test_lookup_unavailable_saves_nothing_and_leaves_the_connection_alone(self):
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        wizard = self._wizard()
        self._lookup(wizard, error=ApixUnreachable("APIX could not be reached"))
        self.assertEqual(wizard.lookup_state, LOOKUP_UNAVAILABLE)
        self.assertIn("APIX could not be reached", wizard.lookup_message)
        self.assertFalse(wizard.result_ids)
        self.assertFalse(self.plain.edicode)
        self.assertEqual(connection.status, "working")
        self.assertFalse(connection.send_paused_until)
        self._lookup(wizard, error=ApixRejected("Permission denied", status_code="9204"))
        self.assertEqual(wizard.lookup_state, LOOKUP_UNAVAILABLE)
        self.assertEqual(connection.status, "working")
        # entry by hand stays possible
        wizard.write({"manual_edicode": "003700000043", "manual_operator_id": self.operator.id})
        wizard.action_save()
        self.assertEqual(self.plain.edicode, "003700000043")

    def test_no_working_connection_means_lookup_unavailable(self):
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        connection.write({"status": "rejected", "sending_enabled": False})
        wizard = self._wizard()
        query = self._lookup(wizard, [_found("003700000041", "ZZOPER")])
        query.assert_not_called()
        self.assertEqual(wizard.lookup_state, LOOKUP_UNAVAILABLE)

    def test_lookup_needs_a_business_id_or_a_name(self):
        wizard = self._wizard()
        wizard.write({"lookup_business_id": False, "lookup_name": False})
        self.assertEqual(self._code(wizard.action_lookup), LOOKUP_INPUT_REQUIRED)

    def test_operator_not_on_the_list(self):
        wizard = self._wizard()
        self._lookup(wizard, [_found("FI0000000000000000", "ZZUNKNOWN")])
        result = wizard.result_ids
        self.assertFalse(result.operator_id)
        self.assertFalse(result.operator_listed)
        result.action_use()
        self.assertEqual(self._code(wizard.action_save), OPERATOR_NOT_LISTED)
        self.assertFalse(self.plain.edicode)
        wizard.manual_operator_id = self.other_operator
        wizard.action_save()
        self.assertEqual(self.plain.edicode, "FI0000000000000000")
        self.assertEqual(self.plain.einvoice_operator_id, self.other_operator)

    # -- entry by hand --------------------------------------------------------------

    def test_entry_by_hand_validation(self):
        wizard = self._wizard()
        wizard.manual_operator_id = self.operator
        self.assertEqual(self._code(wizard.action_save), ADDRESS_REQUIRED)
        wizard.write({"manual_edicode": "003700000043", "manual_operator_id": False})
        self.assertEqual(self._code(wizard.action_save), OPERATOR_NOT_LISTED)
        self.other_operator.active = False
        wizard.manual_operator_id = self.other_operator
        self.assertEqual(self._code(wizard.action_save), OPERATOR_NOT_LISTED)
        self.assertFalse(self.plain.edicode)

    # -- delivery method ------------------------------------------------------------

    def test_delivery_method_check_on_request_only(self):
        wizard = self._wizard()
        answer = [{"document_id": self.move.name, "channel_name": "eInvoice",
                   "cost_in_credits": 1, "receiver_name": "ZZ Plain Customer Oy",
                   "letter_pages": None, "letter_class": ""}]
        wizard.write({"manual_edicode": "003700000043", "manual_operator_id": self.operator.id})
        with patch.object(ApixClient, "delivery_method", autospec=True,
                          return_value=answer) as method:
            wizard.action_check_delivery_method()
        documents = method.call_args.args[3]
        self.assertEqual(documents[0]["receiver_edicode"], "003700000043")
        self.assertIn("eInvoice", wizard.delivery_message)
        self.assertIn("1", wizard.delivery_message)
        self.assertFalse(self.plain.edicode)
        with patch.object(ApixClient, "delivery_method", autospec=True,
                          side_effect=ApixUnreachable("APIX could not be reached")):
            wizard.action_check_delivery_method()
        self.assertEqual(wizard.delivery_state, DELIVERY_METHOD_UNAVAILABLE)
        self.assertIn("APIX could not be reached", wizard.delivery_message)
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        self.assertEqual(connection.status, "working")
        self.assertFalse(connection.send_paused_until)

    # -- in progress ---------------------------------------------------------

    def test_save_while_a_send_is_in_progress_keeps_the_transmit_method(self):
        self.env[ATTEMPT]._ledger_claim(self.move, "einvoice", "button", self.accountant)
        wizard = self._wizard()
        wizard.write({"manual_edicode": "003700000043", "manual_operator_id": self.operator.id})
        action = wizard.action_save()
        self.assertEqual(self.plain.edicode, "003700000043")
        self.assertEqual(wizard.outcome_code, TRANSMIT_NOT_SWITCHED)
        self.assertEqual(action["params"]["type"], "warning")
        self.move.invalidate_recordset()
        self.assertEqual(self.move.transmit_method, "email")

    # -- the invoice's own company --------------------------------------------------

    def test_lookup_uses_the_invoice_company_connection(self):
        move_b = self._make_invoice(customer=self.plain, company=self.company_b)
        wizard = self._wizard(move_b)
        self.assertEqual(wizard.env.company, self.company)
        query = self._lookup(wizard, [_found("003700000041", "ZZOPER")])
        self.assertEqual(query.call_args.args[1], "ZZ-TRANSFER-003700000035")
        self.assertIn("every company", wizard.notice)

    # -- access to the invoice (review R7; AC-54, §6.1) --------------------------------------

    def _user_of_company_a(self):
        return new_test_user(
            self.env, login="zz_fix_a_only", groups="account.group_account_invoice",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )

    def test_another_companys_invoice_is_refused_on_create(self):
        """An RPC-created wizard for an invoice the user cannot see."""
        move_b = self._make_invoice(customer=self.plain, company=self.company_b)
        user_a = self._user_of_company_a()
        with self.assertRaises(AccessError):
            self.env[FIX].with_user(user_a).with_context(
                allowed_company_ids=self.company.ids
            ).create({"move_id": move_b.id})

    def test_every_action_checks_the_invoice_before_any_apix_call(self):
        move_b = self._make_invoice(customer=self.plain, company=self.company_b)
        user_a = self._user_of_company_a()
        # a wizard that got past create (e.g. the user lost the company since)
        wizard = self.env[FIX].with_user(user_a).with_context(
            allowed_company_ids=self.company.ids
        ).sudo().create({"move_id": move_b.id}).sudo(False)
        wizard.sudo().write({"lookup_business_id": "0000004-1",
                             "manual_edicode": "003700000043",
                             "manual_operator_id": self.operator.id})
        actions = {
            "action_lookup": wizard.action_lookup,
            "action_check_delivery_method": wizard.action_check_delivery_method,
            "action_save": wizard.action_save,
        }
        with patch.object(ApixClient, "address_query", autospec=True) as query, \
                patch.object(ApixClient, "delivery_method", autospec=True) as method:
            for name, action in actions.items():
                with self.subTest(action=name), self.assertRaises(AccessError):
                    action()
        self.assertFalse(query.called or method.called, "no APIX call")
        self.assertFalse(self.plain.edicode, "nothing saved")

    # -- returning ------------------------------------------------------------

    def test_returns_to_the_invoice_or_the_mass_wizard(self):
        wizard = self._wizard()
        wizard.write({"manual_edicode": "003700000043", "manual_operator_id": self.operator.id})
        action = wizard.action_save()
        self.assertEqual(action["params"]["next"]["type"], "ir.actions.act_window_close")
        back = self._wizard(return_to="mass_wizard", return_res_model=FIX,
                            return_res_id=wizard.id)
        back.write({"manual_edicode": "003700000044", "manual_operator_id": self.operator.id})
        action = back.action_save()
        self.assertEqual(action["params"]["next"]["res_model"], FIX)
        self.assertEqual(action["params"]["next"]["res_id"], wizard.id)

    def test_open_from_the_invoice(self):
        action = self.move.with_user(self.accountant).action_apix_open_fix()
        self.assertEqual(action["res_model"], FIX)
        self.assertEqual(action["context"]["default_move_id"], self.move.id)
