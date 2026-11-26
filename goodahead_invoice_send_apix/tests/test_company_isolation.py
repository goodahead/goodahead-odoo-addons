# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Every invoice goes out in its own company's name and stays in its own
company's lists (send T39; AC-15, AC-52, AC-53, AC-54; US-17; sad.md §8
Multi-company).

Two companies, invented data only. ``CompanyFake`` answers SendInvoiceZIP
per calling transfer id: no network.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from odoo.addons.goodahead_einvoice_base.lib import finvoice
from odoo.addons.queue_job.job import Job

from .common import FakeApixSendClient, SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
WIZARD = "goodahead.invoice.send.wizard"
TRANSFER_A = "ZZ-TRANSFER-003700000011"
TRANSFER_B = "ZZ-TRANSFER-003700000035"


class CompanyFake(FakeApixSendClient):
    """``modes``: transfer id → ``(mode, status_code)``; default accepted."""

    def __init__(self, modes=None):
        super().__init__("accepted")
        self.modes = modes or {}

    def __call__(self, client, transfer_id, transfer_key, soft, ver, zip_bytes):
        self.mode, self.status_code = self.modes.get(transfer_id, ("accepted", None))
        return super().__call__(client, transfer_id, transfer_key, soft, ver, zip_bytes)


@tagged("post_install", "-at_install")
class TestCompanyIsolation(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.accountant_a = new_test_user(
            cls.env, login="zz_iso_a_only", groups="account.group_account_invoice",
            company_id=cls.company.id, company_ids=[(6, 0, cls.company.ids)],
        )

    def _as(self, records, user=None, active=None):
        """``records`` as ``user`` (the two-company Accountant), ``active``
        (A) first among the allowed companies."""
        user = user or self.accountant
        active = active or self.company
        allowed = [active.id] + [c.id for c in user.company_ids if c != active]
        return records.with_user(user).with_context(allowed_company_ids=allowed)

    def _perform(self, attempt):
        job = Job.load(self.env, attempt.sudo().queue_job_id.uuid)
        job.set_started()
        job.store()
        job.set_done(result=job.perform())
        job.store()
        self._after_commit()
        attempt.invalidate_recordset()

    # -- AC-52: the invoice's own company --------------------------------------------------------

    def test_invoice_of_the_second_company_goes_with_its_own_connection(self):
        fake = CompanyFake().patch(self)
        move_b = self._make_invoice(company=self.company_b)
        result = self._as(move_b)._apix_send_now("button")
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual([call["transfer_id"] for call in fake.calls], [TRANSFER_B])
        message = self._row(result).sudo().message_attachment_id.raw
        self.assertIn(b"ZZ Second Company Oy", message)
        self.assertNotIn(b"ZZ Sender Oy", message)
        self.assertEqual(self._row(result).company_id, self.company_b)
        Attempt = self.env[ATTEMPT]
        self.assertFalse(self._as(Attempt, self.accountant_a).search([("move_id", "=", move_b.id)]))

    # -- AC-15: a selection across companies -------------------------------------------------------

    def test_mass_send_splits_per_company(self):
        fake = CompanyFake().patch(self)
        self.env["goodahead.apix.connection"]._apix_for_company(self.company_b).write(
            {"sending_enabled": False})
        a_einvoice = self._make_invoice()
        b_einvoice = self._make_invoice(company=self.company_b)
        b_email = self._make_invoice(company=self.company_b, transmit_method="email")
        wizard = self._as(self.env[WIZARD]).with_context(
            active_model="account.move", active_ids=(a_einvoice | b_einvoice | b_email).ids,
        ).create({})
        line_b = wizard.line_ids.filtered(lambda line: line.move_id == b_einvoice)
        self.assertEqual(line_b.category, "sending_off")
        wizard.action_send_background()
        attempts = self.env[ATTEMPT].search([("run_uuid", "=", wizard.run_uuid)])
        self.assertEqual(attempts.move_id, a_einvoice | b_email)
        for attempt in attempts:
            self._perform(attempt)
        self.assertEqual([call["transfer_id"] for call in fake.calls], [TRANSFER_A])
        self.assertEqual(
            {a.move_id: (a.state, a.channel) for a in attempts},
            {a_einvoice: ("accepted", "einvoice"), b_email: ("sent", "email")},
        )

    # -- AC-53: one company failing in the background -------------------------------------------

    def test_failing_company_fails_alone_in_the_background(self):
        fake = CompanyFake({TRANSFER_B: ("err", "4004")}).patch(self)
        moves_a = self._make_invoice() | self._make_invoice()
        moves_b = self._make_invoice(company=self.company_b) | self._make_invoice(
            company=self.company_b)
        wizard = self._as(self.env[WIZARD]).with_context(
            active_model="account.move", active_ids=(moves_a | moves_b).ids,
        ).create({})
        wizard.action_send_background()
        attempts = self.env[ATTEMPT].search([("run_uuid", "=", wizard.run_uuid)], order="id")
        for attempt in attempts:
            self.assertEqual(attempt.sudo().queue_job_id.company_id, attempt.company_id)
            self._perform(attempt)
        states = {attempt.move_id: attempt.state for attempt in attempts}
        self.assertEqual({states[m] for m in moves_a}, {"accepted"})
        self.assertEqual({states[m] for m in moves_b}, {"failed"})
        reasons = [a.reason for a in attempts if a.company_id == self.company_b]
        self.assertTrue(reasons[0].startswith("[send.credentials_refused]"), reasons)
        self.assertTrue(reasons[1].startswith("[send.company_paused]"),
                        "the paused company fails fast, no second call")
        self.assertEqual(sorted(call["transfer_id"] for call in fake.calls),
                         sorted([TRANSFER_A, TRANSFER_A, TRANSFER_B]))

    # -- AC-54: other companies stay hidden ------------------------------------------------------

    def test_other_company_is_neither_visible_nor_sendable(self):
        move_a = self._make_invoice()
        move_b = self._make_invoice(company=self.company_b)
        Attempt = self.env[ATTEMPT]
        for move in move_a | move_b:
            claim = Attempt._ledger_claim(move, "einvoice", "button", self.accountant)
            Attempt._ledger_finish(claim, "failed", {"reason": "[send.no_answer] ZZ"})
        Move = self._as(self.env["account.move"], self.accountant_a)
        self.assertEqual(Move.search([("id", "in", (move_a | move_b).ids)]), move_a)
        self.assertEqual(self._as(Attempt, self.accountant_a).search(
            [("move_id", "in", (move_a | move_b).ids)]).move_id, move_a)
        data = self._as(self.env["goodahead.einvoice.attention"], self.accountant_a)._attention_data()
        failed = next(s for s in data["sections"] if s["key"] == "failed_sends")
        self.assertEqual(failed["count"], 1)
        wizard = self._as(self.env[WIZARD], self.accountant_a).with_context(
            active_model="account.move", active_ids=(move_a | move_b).ids).create({})
        self.assertEqual(wizard.line_ids.move_id, move_a)
        with self.assertRaises(AccessError):
            self._as(move_b, self.accountant_a)._apix_send_now("button")


# ---------------------------------------------------------------------------
# finvoice content T16: each company's own taxes in a real send and
# content at the length edges
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestFinvoiceContentIsolation(SendApixCommon):
    def _as(self, records, user=None, active=None):
        """``records`` as the two-company Accountant with ``active`` (A) first."""
        user = user or self.accountant
        active = active or self.company
        allowed = [active.id] + [c.id for c in user.company_ids if c != active]
        return records.with_user(user).with_context(allowed_company_ids=allowed)

    def test_second_company_sends_its_own_vat_categories_and_reasons(self):
        fake = CompanyFake().patch(self)
        tax_a = self._make_tax(self.company, 0.0, "AE", reason="ZZ reason of company A")
        tax_b = self._make_tax(self.company_b, 0.0, "G", reason="ZZ reason of company B")
        move_b = self._make_invoice(company=self.company_b, lines=[{
            "name": "ZZ export", "quantity": 1, "price_unit": 100.0,
            "tax_ids": [(6, 0, tax_b.ids)]}])
        result = self._as(move_b)._apix_send_now("button")
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual([call["transfer_id"] for call in fake.calls], [TRANSFER_B])
        message = self._row(result).sudo().message_attachment_id.raw
        self.assertEqual(finvoice.validate_schema(message), [])
        spec = etree.fromstring(message).find("InvoiceDetails/VatSpecificationDetails")
        self.assertEqual(spec.findtext("VatCode"), "G")
        self.assertEqual(spec.findtext("VatFreeText"), "ZZ reason of company B")
        self.assertNotIn(b"ZZ reason of company A", message)
        self.assertTrue(tax_a)

    def test_administrator_of_one_company_neither_sees_nor_changes_the_others_taxes(self):
        admin_a = new_test_user(
            self.env, login="zz_iso_tax_admin_a", groups="account.group_account_manager",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )
        tax_b = self._make_tax(self.company_b, 0.0, "G")
        Tax = self.env["account.tax"].with_user(admin_a)
        self.assertFalse(Tax.search([("id", "=", tax_b.id)]))
        with self.assertRaises(AccessError):
            tax_b.with_user(admin_a).write({"einvoice_vat_reason": "ZZ"})

    def test_content_at_the_length_edges_stays_schema_valid(self):
        """AC-25: 3 × 70 reason, a long delivery name, a long note and a long
        heading, references at 70 — valid; identifiers never cut."""
        fake = CompanyFake().patch(self)
        tax = self._make_tax(self.company, 0.0, "E", reason="\n".join(["R" * 70] * 3),
                             code="VATEX-EU-132-1A")
        site = self.env["res.partner"].create({
            "name": "ZZ Delivery Site With A Name Far Longer Than Thirty-Five Characters",
            "type": "delivery", "parent_id": self.customer.id,
            "street": "Toimituskatu 5 " + "B" * 40, "zip": "20100", "city": "Turku",
            "country_id": self.finland.id,
        })
        move = self._make_invoice(
            lines=[
                {"display_type": "line_section", "name": "H" * 600},
                {"name": "ZZ line", "quantity": 3, "price_unit": 9.99, "discount": 7.0,
                 "tax_ids": [(6, 0, tax.ids)]},
                {"display_type": "line_note", "name": " ".join(["note"] * 400)},
            ],
            ref="P" * 70, invoice_origin="S" * 70, einvoice_buyer_reference="B" * 70,
            payment_reference="RF471234567890", partner_shipping_id=site.id,
        )
        result = self._as(move)._apix_send_now("button")
        self.assertEqual(result.outcome, "accepted")
        message = self._row(result).sudo().message_attachment_id.raw
        self.assertEqual(finvoice.validate_schema(message), [])
        root = etree.fromstring(message)
        details = root.find("InvoiceDetails")
        self.assertEqual(details.findtext("OrderIdentifier"), "P" * 70)
        self.assertEqual(details.findtext("SellerReferenceIdentifier"), "S" * 70)
        self.assertEqual(details.findtext("BuyerReferenceIdentifier"), "B" * 70)
        self.assertEqual([t.text for t in details.findall(
            "VatSpecificationDetails/VatFreeText")], ["R" * 70] * 3)
        self.assertEqual(len(root.findtext("DeliveryPartyDetails/DeliveryOrganisationName")), 35)
        self.assertEqual(len(fake.calls), 1)
