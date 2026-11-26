# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The e-invoice address lookup from the contact form and the company form.

The fix-it wizard in partner mode (a contact) and company mode (the
company's own address and operator): open through the buttons the way the
web client calls them, look up with a mocked APIX answer, enter by hand,
save the address and operator as a pair, and the access refusals.

APIX is never called: either the client's AddressQuery is patched, or the
HTTP session answers with the invented ``goodahead_apix`` fixtures. Every
name, address and identifier here is invented.
"""

import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

import requests  # noqa: E402
from lxml import etree  # noqa: E402

from odoo.exceptions import AccessError, UserError  # noqa: E402
from odoo.service.model import call_kw  # noqa: E402
from odoo.tests import new_test_user, tagged  # noqa: E402
from odoo.tools.misc import file_path  # noqa: E402

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient  # noqa: E402

from ..wizard.send_fix import (  # noqa: E402
    FOUND,
    LOOKUP_UNAVAILABLE,
    NOTHING_PICKED,
    FixError,
)
from .common import SendApixCommon  # noqa: E402

FIX = "goodahead.invoice.send.fix"
ATTEMPT = "goodahead.invoice.send.attempt"
BUTTON = "action_apix_open_einvoice_lookup"


def _found(edicode, operator, name="ZZ Lookup Customer Oy", business_id="0000006-8"):
    return {"receiver_name": name, "business_id": business_id, "edicode": edicode,
            "operator": operator}


class _FixtureResponse:
    status_code = 200

    def __init__(self, name):
        with open(file_path(f"goodahead_apix/tests/fixtures/{name}"), "rb") as handle:
            self.content = handle.read()


@tagged("post_install", "-at_install")
class TestEinvoiceLookup(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        both = (cls.company | cls.company_b).ids
        cls.customer_lookup = cls._make_customer("ZZ Lookup Customer Oy", business_id="0000006-8")
        cls.other_operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "Example Operator Oy", "identifier": "ZZEXAMPLE"}
        )
        # a billing user who may also change contacts
        cls.writer = new_test_user(
            cls.env, login="zz_lookup_writer",
            groups="account.group_account_invoice,base.group_partner_manager",
            company_id=cls.company.id, company_ids=[(6, 0, both)],
        )
        # an accounting administrator who may change the company
        cls.admin = new_test_user(
            cls.env, login="zz_lookup_admin",
            groups="account.group_account_manager,base.group_erp_manager,"
                   "base.group_partner_manager",
            company_id=cls.company.id, company_ids=[(6, 0, both)],
        )
        # may change companies, but is no accounting administrator
        cls.settings_only = new_test_user(
            cls.env, login="zz_lookup_settings",
            groups="account.group_account_invoice,base.group_erp_manager",
            company_id=cls.company.id, company_ids=[(6, 0, both)],
        )
        # an accounting administrator of the first company only
        cls.admin_a = new_test_user(
            cls.env, login="zz_lookup_admin_a",
            groups="account.group_account_manager,base.group_erp_manager,"
                   "base.group_partner_manager",
            company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)],
        )
        cls.company_b.partner_id.einvoice_operator_id = cls.operator

    # -- helpers --------------------------------------------------------------------------

    def _env(self, user, companies=None):
        companies = companies or (self.company | self.company_b)
        return self.env(user=user, context=dict(
            self.env.context, allowed_company_ids=companies.ids,
            tracking_disable=False, mail_notrack=False,
        ))

    def _open(self, user, model, record, companies=None):
        """Press the button as the web client does, then create the wizard
        with the action's context, as the dialog does."""
        env = self._env(user, companies)
        action = call_kw(env[model], BUTTON, [[record.id]], {"context": dict(env.context)})
        self.assertEqual(action["res_model"], FIX)
        self.assertEqual(action["target"], "new")
        context = dict(env.context, **action["context"])
        wizard_id = call_kw(env[FIX], "create", [{}], {"context": context})
        return env[FIX].browse(wizard_id)

    def _lookup(self, wizard, answer=None, error=None):
        with patch.object(ApixClient, "address_query", autospec=True,
                          return_value=answer, side_effect=error) as query:
            call_kw(wizard, "action_lookup", [wizard.ids], {})
        return query

    def _arch(self, user, model, view_xmlid):
        view = self.env.ref(view_xmlid)
        arch = self.env[model].with_user(user).get_view(view.id, "form")["arch"]
        return etree.fromstring(arch)

    def _counts(self):
        return {
            model: self.env[model].sudo().search_count([])
            for model in ("account.move", ATTEMPT, "queue.job", "res.partner",
                          "res.partner.operator.einvoice")
        }

    # -- the contact form button ---------------------------------------------------------

    def test_contact_button_shown_to_billing_users_who_may_change_the_contact(self):
        buttons = self._arch(self.writer, "res.partner", "base.view_partner_form").xpath(
            f"//button[@name='{BUTTON}']"
        )
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get("string"), "Look up e-invoice address")
        self.assertIn("einvoice_lookup_allowed", buttons[0].get("invisible") or "")
        # next to the e-invoice address and operator, on the Sales & Purchase tab
        page = buttons[0].xpath("ancestor::page[@name='sales_purchases']")
        self.assertTrue(page)
        self.assertTrue(page[0].xpath(".//field[@name='einvoice_operator_id']"))
        partner = self.customer_lookup
        self.assertTrue(partner.with_user(self.writer).einvoice_lookup_allowed)
        # a billing user without the right to change contacts does not get it
        self.assertFalse(partner.with_user(self.accountant).einvoice_lookup_allowed)
        # a user who may change contacts but cannot use the wizard
        clerk = new_test_user(self.env, login="zz_lookup_clerk",
                              groups="base.group_user,base.group_partner_manager")
        self.assertFalse(self._arch(clerk, "res.partner", "base.view_partner_form").xpath(
            f"//button[@name='{BUTTON}']"
        ))
        self.assertFalse(partner.with_user(clerk).einvoice_lookup_allowed)

    def test_open_from_the_contact_in_the_current_company(self):
        wizard = self._open(self.writer, "res.partner", self.customer_lookup,
                            companies=self.company_b | self.company)
        self.assertEqual(wizard.mode, "partner")
        self.assertFalse(wizard.move_id)
        self.assertEqual(wizard.partner_id, self.customer_lookup)
        self.assertEqual(wizard.company_id, self.company_b, "the user's current company")
        self.assertEqual(wizard.lookup_business_id, "0000006-8")
        self.assertEqual(wizard.lookup_name, "ZZ Lookup Customer Oy")
        self.assertIn("ZZ Lookup Customer Oy", wizard.notice)
        self.assertIn("every company", wizard.notice)

    def test_lookup_from_the_contact_with_the_apix_answer_saves_the_pair(self):
        before = self._counts()
        wizard = self._open(self.writer, "res.partner", self.customer_lookup,
                            companies=self.company_b | self.company)
        response = _FixtureResponse("address_one.xml")
        with patch.object(requests.Session, "request", autospec=True,
                          return_value=response) as http:
            call_kw(wizard, "action_lookup", [wizard.ids], {})
        url = http.call_args.args[2]
        params = dict(parse_qsl(urlsplit(url).query))
        self.assertEqual(params["uid"], "ZZ-TRANSFER-003700000035",
                         "the current company's connection")
        self.assertEqual(wizard.lookup_state, FOUND)
        self.assertEqual(len(wizard.result_ids), 1)
        result = wizard.result_ids
        self.assertEqual(result.edicode, "003700000035")
        self.assertEqual(result.operator_id, self.other_operator)
        # nothing is written until Save
        self.assertFalse(self.customer_lookup.edicode)
        self.assertEqual(self._code(lambda: call_kw(wizard, "action_save", [wizard.ids], {})),
                         NOTHING_PICKED)
        call_kw(result, "action_use", [result.ids], {})
        self.assertFalse(self.customer_lookup.edicode)
        self.env.cr.flush()
        action = call_kw(wizard, "action_save", [wizard.ids], {})
        # the tracking is written when the transaction's pre-commit runs
        self.env.flush_all()
        self.env.cr.flush()
        self.customer_lookup.invalidate_recordset(["message_ids"])
        self.assertEqual(self.customer_lookup.edicode, "003700000035")
        self.assertEqual(self.customer_lookup.einvoice_operator_id, self.other_operator)
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn("ZZ Lookup Customer Oy", action["params"]["message"])
        self.assertEqual(action["params"]["next"]["type"], "ir.actions.act_window_close")
        # nothing sent, nothing created but the wizard
        self.assertEqual(self._counts(), before)
        tracked = self.customer_lookup.message_ids.tracking_value_ids.filtered(
            lambda value: value.field_id.name == "edicode"
        )
        self.assertTrue(tracked)
        self.assertEqual(tracked[0].mail_message_id.author_id, self.writer.partner_id)

    def test_entry_by_hand_from_the_contact(self):
        wizard = self._open(self.writer, "res.partner", self.customer_lookup)
        wizard.write({"manual_edicode": " 003700000061 ", "manual_operator_id": self.operator.id})
        self.assertFalse(self.customer_lookup.edicode)
        call_kw(wizard, "action_save", [wizard.ids], {})
        self.assertEqual(self.customer_lookup.edicode, "003700000061")
        self.assertEqual(self.customer_lookup.einvoice_operator_id, self.operator)

    def test_saves_to_the_contact_itself_not_its_commercial_customer(self):
        desk = self.env["res.partner"].create({
            "name": "ZZ Lookup Billing", "type": "invoice", "parent_id": self.customer_lookup.id,
        })
        wizard = self._open(self.writer, "res.partner", desk)
        self.assertEqual(wizard.partner_id, desk)
        self.assertEqual(wizard.lookup_business_id, "0000006-8")
        self._lookup(wizard, [_found("003700000062", "ZZOPER")])
        wizard.result_ids.action_use()
        wizard.action_save()
        self.assertEqual(desk.edicode, "003700000062")
        self.assertEqual(desk.einvoice_operator_id, self.operator)
        self.assertFalse(self.customer_lookup.edicode)

    def test_no_working_connection_reports_it_and_entry_by_hand_still_works(self):
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        connection.write({"status": "rejected", "sending_enabled": False})
        wizard = self._open(self.writer, "res.partner", self.customer_lookup,
                            companies=self.company)
        query = self._lookup(wizard, [_found("003700000062", "ZZOPER")])
        query.assert_not_called()
        self.assertEqual(wizard.lookup_state, LOOKUP_UNAVAILABLE)
        self.assertIn("ZZ Sender Oy has no working APIX connection", wizard.lookup_message)
        self.assertIn("by hand", wizard.lookup_message)
        wizard.write({"manual_edicode": "003700000063", "manual_operator_id": self.operator.id})
        call_kw(wizard, "action_save", [wizard.ids], {})
        self.assertEqual(self.customer_lookup.edicode, "003700000063")

    def test_invoice_only_parts_are_hidden_and_refused_for_a_contact(self):
        arch = etree.fromstring(self.env[FIX].get_view(
            self.env.ref("goodahead_invoice_send_apix.goodahead_invoice_send_fix_view_form").id,
            "form",
        )["arch"])
        check = arch.xpath("//button[@name='action_check_delivery_method']")[0]
        self.assertIn("mode != 'invoice'", check.get("invisible") or "")
        wizard = self._open(self.writer, "res.partner", self.customer_lookup)
        with patch.object(ApixClient, "delivery_method", autospec=True) as method, \
                self.assertRaises(FixError):
            wizard.action_check_delivery_method()
        method.assert_not_called()

    # -- access to the contact (as review R7) ----------------------------------------------

    def test_a_user_who_may_not_change_the_contact_is_refused(self):
        env = self._env(self.accountant)
        with self.assertRaises(AccessError):
            call_kw(env["res.partner"], BUTTON, [[self.customer_lookup.id]], {})
        with self.assertRaises(AccessError):
            call_kw(env[FIX], "create", [{"target_partner_id": self.customer_lookup.id}], {})

    def test_another_companys_contact_is_refused_over_rpc(self):
        foreign = self._make_customer("ZZ Foreign Customer Oy", business_id="0000007-6",
                                      company_id=self.company_b.id)
        writer_a = new_test_user(
            self.env, login="zz_lookup_writer_a",
            groups="account.group_account_invoice,base.group_partner_manager",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )
        env = self._env(writer_a, self.company)
        with self.assertRaises(AccessError):
            call_kw(env["res.partner"], BUTTON, [[foreign.id]], {})
        with self.assertRaises(AccessError):
            call_kw(env[FIX], "create", [{"target_partner_id": foreign.id}], {})
        # nor can the lookup run with a company the user is not in
        with self.assertRaises(AccessError):
            call_kw(env[FIX], "create", [{
                "target_partner_id": self.customer_lookup.id,
                "lookup_company_id": self.company_b.id,
            }], {})

    def test_every_action_checks_the_contact_before_any_apix_call(self):
        env = self._env(self.accountant)
        # a wizard that got past create (e.g. the user lost the right since)
        wizard = env[FIX].sudo().create({
            "target_partner_id": self.customer_lookup.id,
            "lookup_company_id": self.company.id,
            "manual_edicode": "003700000064",
            "manual_operator_id": self.operator.id,
        }).sudo(False)
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
        self.assertFalse(self.customer_lookup.edicode, "nothing saved")

    def test_a_wizard_needs_exactly_one_target(self):
        Fix = self._env(self.admin)[FIX]
        with self.assertRaises(UserError):
            Fix.create({})
        with self.assertRaises(UserError):
            Fix.create({"target_partner_id": self.customer_lookup.id,
                        "target_company_id": self.company.id})

    # -- the company form button -----------------------------------------------------------

    def test_company_button_for_accounting_administrators_only(self):
        buttons = self._arch(self.admin, "res.company", "base.view_company_form").xpath(
            f"//button[@name='{BUTTON}']"
        )
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get("string"), "Look up e-invoice address")
        self.assertFalse(self._arch(self.settings_only, "res.company", "base.view_company_form")
                         .xpath(f"//button[@name='{BUTTON}']"))

    def test_lookup_for_the_company_uses_its_own_connection_and_saves_the_pair(self):
        before = self._counts()
        # opened while the first company is the current one
        wizard = self._open(self.admin, "res.company", self.company_b)
        self.assertEqual(wizard.mode, "company")
        self.assertEqual(wizard.company_id, self.company_b)
        self.assertEqual(wizard.partner_id, self.company_b.partner_id)
        self.assertEqual(wizard.lookup_business_id, "0000003-5")
        self.assertEqual(wizard.lookup_name, "ZZ Second Company Oy")
        self.assertEqual(wizard.current_edicode, "003700000035")
        self.assertIn("ZZ Second Company Oy", wizard.notice)
        query = self._lookup(wizard, [_found("003700000065", "ZZEXAMPLE",
                                             name="ZZ Second Company Oy", business_id="0000003-5")])
        self.assertEqual(query.call_args.args[1], "ZZ-TRANSFER-003700000035")
        self.assertEqual(query.call_args.kwargs.get("business_id"), "0000003-5")
        self.assertEqual(self.company_b.edicode, "003700000035", "nothing written yet")
        wizard.result_ids.action_use()
        action = call_kw(wizard, "action_save", [wizard.ids], {})
        self.assertEqual(self.company_b.edicode, "003700000065")
        self.assertEqual(self.company_b.einvoice_operator_id, self.other_operator)
        self.assertEqual(self.company.edicode, "003700000011", "the other company untouched")
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(self._counts(), before)

    def test_company_entry_by_hand_without_a_connection(self):
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company_b)
        connection.write({"status": "rejected", "sending_enabled": False})
        wizard = self._open(self.admin, "res.company", self.company_b)
        query = self._lookup(wizard, [_found("003700000066", "ZZOPER")])
        query.assert_not_called()
        self.assertEqual(wizard.lookup_state, LOOKUP_UNAVAILABLE)
        self.assertIn("ZZ Second Company Oy has no working APIX connection",
                      wizard.lookup_message)
        wizard.write({"manual_edicode": "003700000066", "manual_operator_id": self.operator.id})
        wizard.action_save()
        self.assertEqual(self.company_b.edicode, "003700000066")
        self.assertEqual(self.company_b.einvoice_operator_id, self.operator)

    def test_company_lookup_refused_to_non_administrators(self):
        for user in (self.settings_only, self.writer):
            env = self._env(user)
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    call_kw(env["res.company"], BUTTON, [[self.company.id]], {})
                with self.assertRaises(AccessError):
                    call_kw(env[FIX], "create", [{"target_company_id": self.company.id}], {})

    def test_company_lookup_refused_for_another_company_over_rpc(self):
        env = self._env(self.admin_a, self.company)
        with self.assertRaises(AccessError):
            call_kw(env["res.company"], BUTTON, [[self.company_b.id]], {})
        with self.assertRaises(AccessError):
            call_kw(env[FIX], "create", [{"target_company_id": self.company_b.id}], {})

    def test_every_company_action_checks_the_administrator_before_any_apix_call(self):
        env = self._env(self.settings_only)
        wizard = env[FIX].sudo().create({
            "target_company_id": self.company.id,
            "manual_edicode": "003700000067",
            "manual_operator_id": self.operator.id,
        }).sudo(False)
        with patch.object(ApixClient, "address_query", autospec=True) as query:
            for action in (wizard.action_lookup, wizard.action_save):
                with self.subTest(action=action.__name__), self.assertRaises(AccessError):
                    action()
        query.assert_not_called()
        self.assertEqual(self.company.edicode, "003700000011", "nothing saved")

    # -- the invoice mode is unchanged -----------------------------------------------------

    def test_the_invoice_mode_keeps_its_parts(self):
        move = self._make_invoice(customer=self.customer_lookup)
        wizard = self._env(self.accountant)[FIX].create({"move_id": move.id})
        self.assertEqual(wizard.mode, "invoice")
        self.assertEqual(wizard.company_id, move.company_id)
        self.assertEqual(wizard.partner_id, self.customer_lookup)

    def _code(self, call):
        with self.assertRaises(FixError) as caught:
            call()
        return caught.exception.code
