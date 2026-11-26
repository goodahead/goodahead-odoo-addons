# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Readiness, already-sent and the sendable checks on invoices and contacts.


Every name, e-invoice address, Business ID and account number here is
invented. No APIX call is made: a counting stub of the APIX client proves it.
"""

import time
import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import fields  # noqa: E402
from odoo.tests import new_test_user, tagged  # noqa: E402

from odoo.addons.account.tests.common import AccountTestInvoicingCommon  # noqa: E402
from odoo.addons.goodahead_apix.lib.apix_client import ApixClient  # noqa: E402

from ..models.account_move import (  # noqa: E402
    ALREADY_SENT,
    NOT_CUSTOMER_DOCUMENT,
    NOT_POSTED,
    NOT_READY,
    SENDING_OFF,
    SEND_IN_PROGRESS,
)

from .common import SendApixCommon  # noqa: E402

ATTEMPT = "goodahead.invoice.send.attempt"


@tagged("post_install", "-at_install")
class TestReadinessChecks(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        finland = cls.env.ref("base.fi")
        cls.company.write({
            "name": "ZZ Sender Oy",
            "company_registry": "0000001-9",
            "street": "Esimerkkikatu 1",
            "zip": "00100",
            "city": "Helsinki",
            "country_id": finland.id,
        })
        bank = cls.env["res.bank"].create({"name": "ZZ Bank", "bic": "ZZBAFIHH"})
        cls.bank_account = cls.env["res.partner.bank"].create({
            "partner_id": cls.company.partner_id.id,
            "acc_number": "FI51 1234 5600 0000 51",
            "bank_id": bank.id,
            "allow_out_payment": True,
        })
        cls.operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Operator", "identifier": "ZZOPER"}
        )
        Partner = cls.env["res.partner"]
        cls.customer = Partner.create({
            "name": "ZZ Customer Oy",
            "is_company": True,
            "edicode": "003700000019",
            "einvoice_operator_id": cls.operator.id,
            "company_registry": "0000002-7",
            "street": "Asiakkaantie 2",
            "zip": "00200",
            "city": "Espoo",
            "country_id": finland.id,
            "email": "zz.customer@example.com",
        })
        # an invoice contact without its own details: everything from the
        # commercial customer
        cls.desk = Partner.create({
            "name": "ZZ Billing Desk", "type": "invoice", "parent_id": cls.customer.id,
        })
        cls.foreign = Partner.create({
            "name": "ZZ Foreign GmbH",
            "is_company": True,
            "edicode": "DE000000000001",
            "einvoice_operator_id": cls.operator.id,
            "street": "Beispielstrasse 3",
            "zip": "10115",
            "city": "Berlin",
            "country_id": cls.env.ref("base.de").id,
        })
        cls.bare = Partner.create({"name": "ZZ Bare Customer Oy", "edicode": "003700000027"})
        cls.connection = cls.env["goodahead.apix.connection"].sudo().search(
            [("company_id", "=", cls.company.id)]
        ) or cls.env["goodahead.apix.connection"].sudo().create(
            {"company_id": cls.company.id, "business_id": "0000001-9"}
        )
        cls.connection.write({"status": "working", "sending_enabled": True})
        cls.accountant = new_test_user(
            cls.env,
            login="zz_ready_accountant",
            groups="account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )

    def _invoice(self, partner, move_type="out_invoice", post=True, method=None):
        move = self.init_invoice(move_type, partner=partner, amounts=[100.0], post=False)
        move.partner_bank_id = self.bank_account
        if method:
            move.transmit_method = method
        if post:
            move.action_post()
        return move

    def _codes(self, move, channel=None, **kwargs):
        refusals = move._apix_check_sendable(channel or move.transmit_method, **kwargs)
        return [refusal["code"] for refusal in refusals]

    def _record(self, move, channel, state):
        return self.env[ATTEMPT]._ledger_record(
            move, channel, state, "erp_send" if channel in ("email", "download") else "button",
            self.accountant, {"end_date": fields.Datetime.now()},
        )

    # -- readiness ----------------------------------------------------------

    def test_ready_invoice(self):
        move = self._invoice(self.customer)
        self.assertEqual(move.transmit_method, "einvoice")
        self.assertTrue(move.send_ready, move.send_readiness)
        self.assertTrue(move.send_readiness["ready"])
        self.assertEqual(self._codes(move), [])

    def test_contact_falls_back_to_the_commercial_customer_per_field(self):
        move = self._invoice(self.desk)
        self.assertTrue(move.send_ready, move.send_readiness)
        recipient = move._apix_recipient_values()
        self.assertEqual(recipient["name"], "ZZ Billing Desk")
        self.assertEqual(recipient["edicode"], "003700000019")
        self.assertEqual(recipient["operator_identifier"], "ZZOPER")
        self.assertTrue(recipient["operator_listed"])
        self.assertEqual(recipient["street"], "Asiakkaantie 2")
        self.assertEqual(recipient["country_code"], "FI")

    def test_contact_value_wins_over_the_commercial_customer(self):
        self.desk.edicode = "003700000035"
        move = self._invoice(self.desk)
        self.assertEqual(move._apix_recipient_values()["edicode"], "003700000035")

    def test_sender_values_from_the_company(self):
        move = self._invoice(self.customer)
        sender = move._apix_sender_values()
        self.assertEqual(sender["name"], "ZZ Sender Oy")
        self.assertEqual(sender["business_id"], "0000001-9")
        self.assertEqual(sender["iban"], "FI5112345600000051")
        self.assertEqual(sender["bic"], "ZZBAFIHH")

    def test_missing_items_are_listed_with_where_they_are_fixed(self):
        move = self._invoice(self.bare)
        self.assertFalse(move.send_ready)
        items = {item["key"]: item for item in move.send_readiness["items"]}
        self.assertEqual(items["recipient_operator"]["severity"], "block")
        self.assertEqual(items["recipient_operator"]["fix_at"], "fix_it")
        self.assertEqual(items["recipient_postal_address"]["severity"], "warn")

    def test_sender_bank_account_missing_blocks(self):
        move = self._invoice(self.customer, post=False)
        move.partner_bank_id = False
        self.bank_account.active = False
        keys = [item["key"] for item in move.send_readiness["items"]]
        self.assertIn("sender_bank_account", keys)

    def test_missing_business_id_and_vat_warns_and_leaves_ready_unchanged(self):
        move = self._invoice(self.customer)
        self.assertTrue(move.send_ready, move.send_readiness)
        before = [i for i in move.send_readiness["items"] if i["key"] != "recipient_business_id"]
        self.customer.company_registry = False
        move.invalidate_recordset()
        self.assertTrue(move.send_ready, "a warning never blocks the send")
        items = {item["key"]: item for item in move.send_readiness["items"]}
        self.assertEqual(items["recipient_business_id"]["severity"], "warn")
        self.assertIn("Look up e-invoice address", items["recipient_business_id"]["message"])
        self.assertEqual([i for i in move.send_readiness["items"]
                          if i["key"] != "recipient_business_id"], before)
        self.assertEqual(self._codes(move), [])
        # the commercial customer's VAT number is enough for its contact
        self.customer.vat = "FI00000027"
        desk_move = self._invoice(self.desk)
        keys = [item["key"] for item in desk_move.send_readiness["items"]]
        self.assertNotIn("recipient_business_id", keys)

    def test_foreign_customer_without_business_id_is_ready(self):
        move = self._invoice(self.foreign)
        self.assertTrue(move.send_ready, move.send_readiness)
        keys = [item["key"] for item in move.send_readiness["items"]]
        self.assertIn("recipient_business_id", keys)  # a warning only
        self.assertEqual(self._codes(move), [])

    def test_email_needs_only_the_email_address(self):
        move = self._invoice(self.bare, method="email")
        self.assertFalse(move.send_ready)
        self.assertEqual(
            [item["key"] for item in move.send_readiness["items"]], ["recipient_email"]
        )
        self.bare.email = "zz.bare@example.com"
        move.invalidate_recordset()
        self.assertTrue(move.send_ready)

    def test_credit_note_same_rules(self):
        refund = self._invoice(self.customer, "out_refund")
        self.assertTrue(refund.send_ready, refund.send_readiness)
        self.assertEqual(self._codes(refund), [])

    def test_contact_readiness(self):
        self.assertTrue(self.customer.send_ready, self.customer.send_readiness)
        self.assertFalse(self.bare.send_ready)
        keys = [item["key"] for item in self.bare.send_readiness["items"]]
        self.assertIn("recipient_operator", keys)
        self.assertNotIn("due_date", keys)  # no invoice-level items on a contact

    # -- sending off ---------------------------------------------------------------

    def test_sending_off_says_so_and_offers_no_action(self):
        move = self._invoice(self.customer)
        self.assertTrue(move.apix_send_available)
        self.connection.write({"sending_enabled": False})
        move.invalidate_recordset()
        self.assertFalse(move.apix_send_available)
        self.assertEqual([i["key"] for i in move.send_readiness["items"]], ["sending_off"])
        self.assertEqual(self._codes(move), [SENDING_OFF])
        self.assertEqual(self._codes(move, "letter"), [SENDING_OFF])
        self.assertNotIn(SENDING_OFF, self._codes(move, "email"))

    def test_no_connection_counts_as_sending_off(self):
        move = self._invoice(self.customer)
        self.connection.unlink()
        move.invalidate_recordset()
        self.assertFalse(move.apix_send_available)
        self.assertEqual(self._codes(move), [SENDING_OFF])

    # -- pre-send refusals ------------------------------------------------

    def test_draft_and_cancelled_are_refused(self):
        draft = self._invoice(self.customer, post=False)
        self.assertEqual(self._codes(draft), [NOT_POSTED])
        refusal = draft._apix_check_sendable("einvoice")[0]
        self.assertIn("only posted, not cancelled invoices and credit notes can be sent",
                      refusal["message"])
        self.assertFalse(draft.apix_send_available)
        cancelled = self._invoice(self.customer)
        cancelled.button_draft()
        cancelled.button_cancel()
        self.assertEqual(self._codes(cancelled, "einvoice"), [NOT_POSTED])

    def test_vendor_bill_is_not_a_customer_document(self):
        bill = self.init_invoice("in_invoice", partner=self.customer, amounts=[10.0], post=True)
        self.assertEqual(self._codes(bill, "einvoice"), [NOT_CUSTOMER_DOCUMENT])
        self.assertFalse(bill.apix_send_available)

    def test_not_ready_lists_the_blocking_items_and_never_falls_back(self):
        move = self._invoice(self.bare)
        refusals = move._apix_check_sendable("einvoice")
        self.assertEqual([r["code"] for r in refusals], [NOT_READY])
        keys = [item["key"] for item in refusals[0]["items"]]
        self.assertIn("recipient_operator", keys)
        self.assertTrue(all(item["severity"] == "block" for item in refusals[0]["items"]))
        self.assertEqual(move.transmit_method, "einvoice", "no other channel is chosen")

    # -- already sent ----------------------------------------------

    def test_never_sent_is_free(self):
        move = self._invoice(self.customer)
        self.assertFalse(move.already_sent)
        self.assertFalse(move._apix_already_sent_info())

    def test_refused_failed_and_downloaded_are_not_already_sent(self):
        move = self._invoice(self.customer)
        self._record(move, "einvoice", "refused")
        self._record(move, "einvoice", "failed")
        self._record(move, "download", "downloaded")
        move.invalidate_recordset()
        self.assertFalse(move.already_sent)
        self.assertEqual(self._codes(move), [])

    def test_sent_through_apix_needs_confirmation(self):
        move = self._invoice(self.customer)
        self._record(move, "einvoice", "accepted")
        move.invalidate_recordset()
        self.assertTrue(move.already_sent)
        refusals = move._apix_check_sendable("einvoice")
        self.assertEqual([r["code"] for r in refusals], [ALREADY_SENT])
        info = refusals[0]["items"]
        self.assertEqual(info["channel"], "einvoice")
        self.assertEqual(info["by"], self.accountant.name)
        self.assertFalse(info["before_install"])
        self.assertTrue(info["when"])
        self.assertEqual(self._codes(move, resend_confirmed=True), [])

    def test_sent_by_email_counts_as_sent(self):
        move = self._invoice(self.customer)
        self._record(move, "email", "sent")
        move.invalidate_recordset()
        info = move._apix_already_sent_info()
        self.assertEqual(info["channel"], "email")
        self.assertEqual(info["origin"], "erp_send")
        self.assertEqual(self._codes(move), [ALREADY_SENT])

    def test_sent_before_install(self):
        move = self._invoice(self.customer)
        self.env.cr.execute(
            "UPDATE account_move SET sent_before_install = TRUE WHERE id = %s", (move.id,)
        )
        move.invalidate_recordset()
        self.assertTrue(move.already_sent)
        info = move._apix_already_sent_info()
        self.assertTrue(info["before_install"])
        self.assertEqual(info["channel"], "unknown")
        refusal = move._apix_check_sendable("einvoice")[0]
        self.assertEqual(refusal["code"], ALREADY_SENT)
        self.assertIn("before", refusal["message"])

    # -- in progress ---------------------------------------------------------------

    def test_send_in_progress_names_the_holder(self):
        move = self._invoice(self.customer)
        self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.accountant)
        move.invalidate_recordset()
        self.assertTrue(move.send_in_progress)
        self.assertIn(self.accountant.name, move.send_in_progress_info)
        refusals = move._apix_check_sendable("einvoice")
        self.assertEqual([r["code"] for r in refusals], [SEND_IN_PROGRESS])
        self.assertEqual(refusals[0]["items"]["user"], self.accountant.name)
        self.assertTrue(refusals[0]["items"]["since"])

    # -- the copy warning ------------------------------------------------

    def test_copy_warning_is_the_connection_s(self):
        move = self._invoice(self.customer)
        self.assertFalse(move.send_copy_warning)
        with patch.object(type(self.env["goodahead.apix.connection"]), "_apix_copy_warning",
                          lambda connection: "ZZ copy warning"):
            move.invalidate_recordset()
            self.assertEqual(move.send_copy_warning, "ZZ copy warning")

    # -- no APIX call, no write -------------------------------------------

    def test_reading_readiness_of_100_invoices_makes_no_apix_call(self):
        partners = (self.customer, self.desk, self.foreign, self.bare)
        moves = self.env["account.move"].create([
            {
                "move_type": "out_invoice" if i % 4 else "out_refund",
                "partner_id": partners[i % 4].id,
                "partner_bank_id": self.bank_account.id,
                "invoice_line_ids": [(0, 0, {"name": f"ZZ line {i}", "quantity": 1,
                                             "price_unit": 10.0 + i})],
            }
            for i in range(100)
        ])
        moves.action_post()
        calls = []
        attempts = self.env[ATTEMPT].sudo().search_count([])

        def count(name):
            def stub(*args, **kwargs):
                calls.append(name)
                raise AssertionError(f"APIX called: {name}")
            return stub

        with patch.object(ApixClient, "_get", count("_get")), \
                patch.object(ApixClient, "_send", count("_send")), \
                patch.object(ApixClient, "__init__", count("__init__")):
            moves.invalidate_recordset()
            seen = [
                (move.send_readiness, move.send_ready, move.already_sent,
                 move.apix_send_available, move._apix_check_sendable(move.transmit_method))
                for move in moves.with_user(self.accountant)
            ]
            seen += [partner.with_user(self.accountant).send_ready for partner in partners]
        self.assertEqual(len(seen), 104)
        self.assertEqual(calls, [])
        self.assertEqual(self.env[ATTEMPT].sudo().search_count([]), attempts)


# ---------------------------------------------------------------------------
# finvoice content T14: the content warnings on the invoice (AC-05, AC-07,
# AC-08, AC-11, AC-15, AC-27, AC-28)
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestContentReadiness(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.s24 = cls._make_tax(cls.company, 24.0, "S")
        cls.ae0 = cls._make_tax(cls.company, 0.0, "AE")
        cls.unclassified0 = cls._make_tax(cls.company, 0.0, name="ZZ 0 % unclassified")
        cls.site = cls.env["res.partner"].create({
            "name": "ZZ Site", "type": "delivery", "parent_id": cls.customer.id,
            "street": "Toimituskatu 7", "city": "Turku", "country_id": cls.finland.id,
        })

    def _line(self, price, *taxes):
        return {"name": "ZZ line", "quantity": 1, "price_unit": price,
                "tax_ids": [(6, 0, [tax.id for tax in taxes])]}

    def _readiness(self, move, user=None):
        move = move.with_user(user) if user else move
        move.invalidate_recordset()
        return move._apix_readiness("einvoice")

    def _keys(self, result):
        return [item["key"] for item in result["items"]]

    def test_unclassified_tax_warns_and_the_send_is_allowed(self):
        move = self._make_invoice(lines=[self._line(100.0, self.unclassified0)])
        result = self._readiness(move)
        self.assertTrue(result["ready"])
        item = next(i for i in result["items"] if i["key"] == "vat_unclassified_tax")
        self.assertEqual(item["severity"], "warn")
        self.assertIn("ZZ 0 % unclassified", item["message"])

    def test_accountant_sees_which_taxes_lack_a_category(self):
        move = self._make_invoice(lines=[self._line(100.0, self.unclassified0)])
        self.assertIn("vat_unclassified_tax", self._keys(self._readiness(move, self.accountant)))

    def test_buyer_vat_number_on_a_reverse_charge_invoice(self):
        customer = self._make_customer("ZZ No VAT AB", "003700000027")
        move = self._make_invoice(customer, [self._line(100.0, self.ae0)])
        self.assertIn("buyer_vat_missing", self._keys(self._readiness(move)))
        customer.vat = "SE556677889901"
        self.assertNotIn("buyer_vat_missing", self._keys(self._readiness(move)))
        self.assertEqual(move._apix_finvoice_values()["buyer"]["vat"], "SE556677889901")

    def test_lines_without_one_clear_category(self):
        move = self._make_invoice(lines=[self._line(100.0), self._line(50.0, self.s24, self.ae0)])
        keys = self._keys(self._readiness(move))
        self.assertIn("vat_line_no_percentage_tax", keys)
        self.assertIn("vat_line_mixed_categories", keys)

    def test_references_and_delivery_gaps(self):
        move = self._make_invoice(ref="P" * 71, invoice_origin="1232", payment_reference=False,
                                  partner_shipping_id=self.site.id)
        result = self._readiness(move)
        self.assertTrue(result["ready"])
        keys = self._keys(result)
        for key in ("order_number_too_long", "seller_reference_withheld",
                    "delivery_address_incomplete"):
            self.assertIn(key, keys)

    def test_readiness_and_message_agree(self):
        move = self._make_invoice(lines=[self._line(100.0, self.unclassified0)],
                                  invoice_origin="1232", payment_reference=False)
        values = move._apix_finvoice_values()
        keys = self._keys(self._readiness(move))
        self.assertIsNone(values["taxes"][0]["code"])
        self.assertIn("vat_unclassified_tax", keys)
        self.assertIsNone(values["invoice"]["seller_reference"])
        self.assertIn("seller_reference_withheld", keys)

    def test_no_apix_call_and_quick(self):
        plain = self._make_invoice()
        rich = self._make_invoice(
            lines=[self._line(100.0, self.unclassified0), self._line(50.0, self.s24, self.ae0)],
            ref="P" * 71, invoice_origin="1232", payment_reference=False,
            partner_shipping_id=self.site.id)

        def p95(move):
            timings = []
            for _run in range(50):
                start = time.perf_counter()
                self._readiness(move)
                timings.append(time.perf_counter() - start)
            return sorted(timings)[int(len(timings) * 0.95) - 1]

        with patch.object(ApixClient, "__init__", side_effect=AssertionError("APIX called")):
            added = p95(rich) - p95(plain)
        self.assertLess(added, 0.05)
