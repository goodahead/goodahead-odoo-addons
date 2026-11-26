# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Shared fixtures of the sending tests (data-model.md §Test fixtures).

Two companies with their own sender details, bank accounts and APIX
connections; invented customers, units and invoices; ``FakeApixSendClient``
answering SendInvoiceZIP in every recorded shape. Every name, e-invoice
address, Business ID, account number and APIX id here is invented. No test
reaches the network.
"""

import os
from unittest.mock import patch

from odoo.tests import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.goodahead_apix.lib.apix_client import (
    ApixClient,
    ApixNoAnswer,
    ApixUnexpected,
    ApixUnreachable,
    SendAnswer,
    parse_send_answer,
)

from ..models import ledger

BATCH_ID = "ZZBATCH-0001"
DOCUMENT_ID = "ZZDOC-0001"
RAW_ANSWER = b"<Response><Status>OK</Status></Response>"
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    """The bytes of the invented answer ``name`` under ``tests/fixtures``."""
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


class FakeApixSendClient:
    """Stands in for ``ApixClient.send_invoice_zip``: records each call and
    answers by ``mode``:

    ``accepted``, ``warning`` (accepted with a ValidateText), ``refused``
    (OK, the document rejected), ``err`` (an ERR Response with
    ``status_code``), ``unreachable``, ``no_answer``, ``http`` (HTTP ≠ 200),
    ``fixture`` (the answer file ``fixture`` under ``tests/fixtures``, parsed
    by the real client).
    """

    def __init__(self, mode="accepted", status_code=None, text="ZZ validation text",
                 fixture=None):
        self.mode = mode
        self.status_code = status_code
        self.text = text
        self.fixture = fixture
        self.calls = []

    def __call__(self, client, transfer_id, transfer_key, soft, ver, zip_bytes):
        self.calls.append({
            "transfer_id": transfer_id, "transfer_key": transfer_key, "soft": soft,
            "ver": ver, "zip": zip_bytes, "environment": client.environment,
        })
        if self.mode == "fixture":
            return parse_send_answer(fixture(self.fixture))
        if self.mode == "unreachable":
            raise ApixUnreachable("SendInvoiceZIP: APIX could not be reached (ConnectTimeout)")
        if self.mode == "no_answer":
            raise ApixNoAnswer("SendInvoiceZIP: APIX did not answer within 60 s (ReadTimeout)")
        if self.mode == "http":
            raise ApixUnexpected("SendInvoiceZIP: APIX answered HTTP 503", http_status=503)
        if self.mode == "err":
            return SendAnswer(status="ERR", status_code=self.status_code,
                              free_text="ZZ refused", raw=b"<Response>ERR</Response>")
        texts = {"en": "", "fi": "", "sv": ""}
        if self.mode in ("warning", "refused"):
            texts = {"en": self.text, "fi": f"{self.text} (fi)", "sv": ""}
        accepted = self.mode != "refused"
        return SendAnswer(
            status="OK",
            status_code="",
            batch_id=BATCH_ID,
            saldo=99,
            cost_in_credits=1,
            accepted_count=int(accepted),
            rejected_count=int(not accepted),
            documents=[{"accepted": accepted, "document_id": DOCUMENT_ID, "texts": texts}],
            raw=RAW_ANSWER,
        )

    def patch(self, test):
        """Install this fake for the rest of ``test``."""
        def send_invoice_zip(client, transfer_id, transfer_key, soft, ver, zip_bytes):
            return self(client, transfer_id, transfer_key, soft, ver, zip_bytes)

        patcher = patch.object(ApixClient, "send_invoice_zip", send_invoice_zip)
        patcher.start()
        test.addCleanup(patcher.stop)
        return self


class SendApixCommon(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.finland = cls.env.ref("base.fi")
        cls.operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Operator", "identifier": "ZZOPER"}
        )
        cls.company = cls.company_data["company"]
        cls.bank_account = cls._setup_sender_company(
            cls.company, "ZZ Sender Oy", "0000001-9", "003700000011",
            "FI51 1234 5600 0000 51", "ZZBAFIHH",
        )
        cls.company_b = cls.setup_other_company(name="ZZ Second Company Oy")["company"]
        cls.bank_account_b = cls._setup_sender_company(
            cls.company_b, "ZZ Second Company Oy", "0000003-5", "003700000035",
            "FI16 1234 5600 0000 02", "ZZBBFIHH",
        )
        cls.customer = cls._make_customer("ZZ Customer Oy", "003700000019", "0000002-7")
        cls.unit_pieces = cls.env.ref("uom.product_uom_unit")
        cls.unit_hours = cls.env.ref("uom.product_uom_hour")
        cls.unit_kilograms = cls.env.ref("uom.product_uom_kgm")
        Uom = cls.env["uom.uom"]
        cls.unit_without_code = Uom.create({"name": "ZZ pallet"})
        cls.unit_hundred = Uom.create({
            "name": "ZZ 100 pcs",
            "relative_uom_id": cls.unit_pieces.id,
            "relative_factor": 100.0,
        })
        cls.accountant = new_test_user(
            cls.env,
            login="zz_send_common_accountant",
            groups="account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=[(6, 0, (cls.company | cls.company_b).ids)],
        )

    @classmethod
    def _setup_sender_company(cls, company, name, business_id, edicode, iban, bic):
        """Sender details, a payment account and a working connection with
        sending on; returns the company's bank account. The fiscal country
        stays the test chart's, so its taxes stay valid."""
        fiscal_country = company.account_fiscal_country_id
        company.write({
            "name": name,
            "company_registry": business_id,
            "vat": "FI" + business_id.replace("-", ""),
            "street": "Esimerkkikatu 1",
            "zip": "00100",
            "city": "Helsinki",
            "country_id": cls.finland.id,
        })
        company.account_fiscal_country_id = fiscal_country
        company.partner_id.edicode = edicode
        bank = cls.env["res.bank"].create({"name": f"{name} Bank", "bic": bic})
        account = cls.env["res.partner.bank"].create({
            "partner_id": company.partner_id.id,
            "acc_number": iban,
            "bank_id": bank.id,
            "allow_out_payment": True,
        })
        Connection = cls.env["goodahead.apix.connection"].sudo()
        connection = Connection.search([("company_id", "=", company.id)]) or Connection.create(
            {"company_id": company.id, "business_id": business_id}
        )
        connection.write({
            "environment": "test",
            "transfer_id": f"ZZ-TRANSFER-{edicode}",
            "transfer_key": f"zz-key-{edicode}",
            "status": "working",
            "sending_enabled": True,
        })
        return account

    @classmethod
    def _make_customer(cls, name, edicode=None, business_id=None, **values):
        vals = {
            "name": name,
            "is_company": True,
            "company_registry": business_id or False,
            "street": "Asiakkaantie 2",
            "zip": "00200",
            "city": "Espoo",
            "country_id": cls.finland.id,
            "email": "zz.customer@example.com",
        }
        if edicode:
            vals.update({"edicode": edicode, "einvoice_operator_id": cls.operator.id})
        vals.update(values)
        return cls.env["res.partner"].create(vals)

    def setUp(self):
        super().setUp()
        # steps left by a test that never "committed" must not reach another
        self._drop_ledger_steps()
        self.addCleanup(self._drop_ledger_steps)

    @classmethod
    def _make_tax(cls, company, rate, category=None, reason=None, code=None, tags=(),
                  name=None):
        """A percentage sales tax of ``company`` (finvoice content): an
        optional set VAT category, reason, exemption code and VAT report
        tags on its invoice-side base repartition line."""
        tax = cls.env["account.tax"].create({
            "name": name or f"ZZ {rate:g} % {category or 'none'} {reason or ''}".strip(),
            "amount": rate,
            "amount_type": "percent",
            "type_tax_use": "sale",
            "company_id": company.id,
            "ubl_cii_tax_category_code": category or False,
            "ubl_cii_tax_exemption_reason_code": code or False,
            "einvoice_vat_reason": reason or False,
        })
        if tags:
            base = tax.invoice_repartition_line_ids.filtered(
                lambda line: line.repartition_type == "base")
            base.tag_ids = [(6, 0, [tag.id for tag in tags])]
        return tax

    def _row(self, attempt):
        """The attempt record of a ledger answer — an ``AttemptView`` or a
        ``SendResult`` carrying one — read in the test's transaction (under
        the test runner the ledger writes through its savepoint, so the row
        is visible here; outside tests the caller never reads it)."""
        view = getattr(attempt, "attempt", None) or attempt
        row = self.env["goodahead.invoice.send.attempt"].browse(view.id)
        row.invalidate_recordset()
        return row

    def _drop_ledger_steps(self):
        self.env.cr.postcommit.data.pop(ledger.AFTER_COMMIT, None)
        self.env.cr.postrollback.data.pop(ledger.AFTER_ROLLBACK, None)

    def _press_send(self, wizard):
        """The Send & Print dialog's own Send button: its context marks a
        person's send (review F3); calling the action without it is code."""
        return wizard.with_context(goodahead_person_send=True).action_send_and_print()

    def _after_commit(self):
        """The ledger steps the caller's commit runs after it (the test
        cursor never commits): emails and downloads are recorded then
        (review R5). Only this add-on's steps: other add-ons' hooks of
        earlier tests are left alone."""
        cr = self.env.cr
        cr.flush()
        cr.postrollback.data.pop(ledger.AFTER_ROLLBACK, None)
        ledger.run_steps(cr.postcommit.data.pop(ledger.AFTER_COMMIT, []))

    def _after_rollback(self):
        """The ledger steps the caller's rollback runs after it, without
        rolling the test back: the commit's steps are dropped."""
        cr = self.env.cr
        cr.postcommit.data.pop(ledger.AFTER_COMMIT, None)
        ledger.run_steps(cr.postrollback.data.pop(ledger.AFTER_ROLLBACK, []))

    def _make_invoice(self, customer=None, lines=None, move_type="out_invoice", company=None,
                      post=True, **values):
        """An invoice (or credit note) of ``company``; ``lines`` are dicts of
        ``invoice_line_ids`` values (default: one line of 100)."""
        company = company or self.company
        bank = self.bank_account_b if company == self.company_b else self.bank_account
        journal = self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "sale")], limit=1
        )
        tax = self.env["account.tax"].search([
            ("company_id", "=", company.id), ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"), ("amount", ">", 0),
        ], limit=1)
        line_values = lines or [{"name": "ZZ service", "quantity": 1, "price_unit": 100.0}]
        vals = {
            "move_type": move_type,
            "partner_id": (customer or self.customer).id,
            "company_id": company.id,
            "journal_id": journal.id,
            "invoice_date": "2026-11-25",
            "partner_bank_id": bank.id,
            "invoice_line_ids": [
                (0, 0, {"tax_ids": [(6, 0, tax.ids)], **line}) for line in line_values
            ],
        }
        vals.update(values)
        move = self.env["account.move"].with_company(company).create(vals)
        if post:
            move.action_post()
        return move
