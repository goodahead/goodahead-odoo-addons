# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Credit-note bank handling — a received credit note must not put the vendor's account on us.

Core reads the bank details of an ``in_refund`` as the company's own, because
on a credit note we are the payee. True of one we issue; false of every one we
receive, where the account printed is the supplier's. These tests run the real
decoder over a real generated CreditNote and assert which partner ends up
holding the account, and which account the document states.

The document is invented: a credit note whose IBAN the model misreads; the
vendor identifiers are faked (``ZZ-…``) so the DB-touching match cannot collide
with a real vendor already in the database.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.models.account_move import (
    iban_checksum_fails,
)
from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
)

from .profiles import skip_method_unless_profile

VENDOR_IBAN = "FI2912345600004521"
# How Claude keeps reading it: one digit too many, failing the IBAN checksum.
MISREAD_IBAN = "FI29123456000004521"
# Any valid Finnish IBAN that is not the vendor's; the company's own number is
# avoided so the test cannot collide with a row already in the database.
COMPANY_IBAN = "FI2112345600000785"


@tagged("post_install", "-at_install")
class TestCreditMemoBank(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.fi = cls.env.ref("base.fi")
        cls.Bank = cls.env["res.partner.bank"]

        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "ZZ Bank Vendor Oy",
                "is_company": True,
                "company_registry": "ZZ-BANK-0001",
                "country_id": cls.fi.id,
            }
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extracted(self, doc_type="CreditNote"):
        sign = -1 if doc_type == "CreditNote" else 1
        return {
            "type": doc_type,
            "invoice_number": "ZZ-CN-1",
            "issue_date": "2026-03-02",
            "due_date": "2026-03-02",
            "currency": "EUR",
            "seller": {
                "name": "ZZ Bank Vendor Oy",
                "vat": "FI09990002",
                "business_id": "ZZ-BANK-0001",
                "country_code": "FI",
                "iban": VENDOR_IBAN,
                "bic": "DABAFIHH",
            },
            "buyer": {
                "name": self.company.name,
                "vat": self.company.vat or "FI00000123",
                "country_code": "FI",
            },
            "payment_reference": "10203040501",
            "lines": [
                {
                    "product_code": "7700123",
                    "name": "ZZ WALL SOCKET",
                    "quantity": sign * 4.0,
                    "unit_code": "C62",
                    "price_unit": 22.5,
                    "line_total": sign * 81.0,
                    "tax_percent": 25.5,
                    "tax_amount": sign * 20.66,
                    "tax_category": "S",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 25.5,
                    "taxable_amount": sign * 81.0,
                    "tax_amount": sign * 20.66,
                    "tax_category": "S",
                }
            ],
            "totals": {
                "line_extension": sign * 81.0,
                "tax_exclusive": sign * 81.0,
                "tax_inclusive": sign * 101.66,
                "payable": sign * 101.66,
            },
        }

    def _import(self, extracted):
        attachment = self.env["ir.attachment"].create(
            {
                "name": "credit-memo.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        record = self.env["goodahead.invoice.import"].create(
            {
                "name": "credit-memo.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": attachment.id,
            }
        )
        ai_result = {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            # The model's entry point, as the import calls it, so the IBAN
            # checksum runs exactly as it does on a real extraction.
            "xml_bytes": self.env["account.move"]._ai_generate_ubl_from_extracted(
                extracted, self.company
            ),
        }
        with patch.object(
            type(self.env["account.move"]),
            "_ai_extract_and_generate_ubl",
            return_value=ai_result,
        ):
            result = record._process_import()
        self.assertTrue(result.startswith("OK"), result)
        return record.move_id

    def _deactivate_company_banks(self):
        """Take whatever the database already hangs off the company partner out
        of the running, so a test controls which account can be selected."""
        self.company.partner_id.bank_ids.filtered("active").write({"active": False})

    def _company_banks(self):
        return self.Bank.with_context(active_test=False).search(
            [
                ("sanitized_acc_number", "=", VENDOR_IBAN),
                ("partner_id", "=", self.company.partner_id.id),
            ]
        )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_credit_note_records_the_iban_on_the_vendor(self):
        """The supplier's account goes on the supplier, and never on us."""
        move = self._import(self._extracted())

        self.assertEqual(move.move_type, "in_refund")
        self.assertFalse(
            self._company_banks(),
            "the vendor's IBAN must not be attached to the company partner",
        )
        vendor_bank = self.Bank.search(
            [("sanitized_acc_number", "=", VENDOR_IBAN), ("partner_id", "=", self.vendor.id)]
        )
        self.assertTrue(vendor_bank, "the vendor's IBAN belongs on the vendor")
        self.assertNotEqual(
            move.partner_bank_id,
            vendor_bank,
            "a credit note is refunded into our account, not the vendor's",
        )

    def test_credit_note_ignores_a_vendor_iban_already_on_the_company(self):
        """The reported case: an earlier import left the vendor's IBAN on the
        company partner, where it answers to the right owner and the compute
        picks it up regardless.

        Every other company account is deactivated first, so the polluted one is
        the only candidate — otherwise another account could be selected and the
        assertion would pass without ever exercising the collision.
        """
        self._deactivate_company_banks()
        polluting = self.Bank.create(
            {"acc_number": VENDOR_IBAN, "partner_id": self.company.partner_id.id}
        )

        move = self._import(self._extracted())

        self.assertEqual(move.move_type, "in_refund")
        self.assertNotEqual(
            move.partner_bank_id,
            polluting,
            "the vendor's IBAN must not be stated on the bill, whoever it hangs off",
        )
        self.assertFalse(
            move.partner_bank_id,
            "no clean company account is left, so the bill states none",
        )

    def test_credit_note_falls_back_to_a_clean_company_account(self):
        """With the polluted row beside a real one, the real one is stated."""
        self._deactivate_company_banks()
        self.Bank.create(
            {"acc_number": VENDOR_IBAN, "partner_id": self.company.partner_id.id}
        )
        ours = self.Bank.create(
            {
                "acc_number": COMPANY_IBAN,
                "partner_id": self.company.partner_id.id,
                "allow_out_payment": True,
            }
        )

        move = self._import(self._extracted())

        self.assertEqual(
            move.partner_bank_id, ours, "the company's own account, not the vendor's"
        )

    def test_bill_still_states_the_vendor_account(self):
        """The guard must not disturb an ordinary bill, where the account we pay
        into is precisely the vendor's."""
        move = self._import(self._extracted(doc_type="Invoice"))

        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(
            move.partner_bank_id.sanitized_acc_number,
            VENDOR_IBAN,
            "a bill we pay must state the vendor's account",
        )
        self.assertEqual(move.partner_bank_id.partner_id, self.vendor)

    def test_bill_keeps_the_vendor_account_despite_a_stray_copy_on_the_company(self):
        """🔴 The guard must not strip a CORRECT account off an ordinary bill.

        The residue this module exists to clean up is a vendor IBAN sitting on
        the company partner. On a bill we pay, `_ai_enforce_document_bank_side`
        used to treat that stray copy as evidence against the vendor's OWN row:
        the legitimate account failed the "not also the other party's" test, no
        replacement survived the same test, `partner_bank_id` was cleared, and
        the chatter said the account "was not the account of the party being
        paid" — which was not what had happened.

        The number test now applies only when WE are the payee. On a bill the
        vendor is the payee, the number on the document is theirs by definition,
        and ownership alone decides.
        """
        self.Bank.create(
            {"acc_number": VENDOR_IBAN, "partner_id": self.company.partner_id.id}
        )

        move = self._import(self._extracted(doc_type="Invoice"))

        self.assertEqual(move.move_type, "in_invoice")
        self.assertTrue(
            move.partner_bank_id, "the bill must still state an account to pay into"
        )
        self.assertEqual(
            move.partner_bank_id.sanitized_acc_number,
            VENDOR_IBAN,
            "a stray copy on the company partner must not disqualify the "
            "vendor's own account on a bill we pay",
        )
        self.assertEqual(move.partner_bank_id.partner_id, self.vendor)

    # ------------------------------------------------------------------
    # 🔴 The NEW decoder's hook — the one that fires on THIS tree
    #
    # Called directly, as the older hook's tests below are, so the two sit side
    # by side and it is obvious which covers which core. The tests above reach
    # this same hook through the real decoder; these pin its contract.
    # ------------------------------------------------------------------

    def _collected(self, move_type="in_refund", account_numbers=(VENDOR_IBAN,)):
        invoice = self.env["account.move"].create({"move_type": move_type})
        return {
            "invoice": invoice,
            "company": self.company,
            "customer_values": {"customer": self.vendor},
            "partner_bank_values": {"account_numbers": list(account_numbers)},
            "logs": [],
            "to_write": {},
        }

    # 🔴 The four `test_new_hook_*` below call
    # `account.edi.ubl._import_ubl_retrieve_partner_bank` DIRECTLY, and that
    # method is core's on the search-plan build and does not exist at all on the
    # legacy one — `super()` there raises `AttributeError`, as does
    # `res.partner.bank._find_or_create_bank_account`. There is no portable
    # spelling: the hook under test is the build. They are gated per METHOD, not
    # per class, because the `test_older_hook_*` pair in this same class is the
    # LIVE path on the legacy build and must keep running there.
    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_new_hook_records_the_iban_on_the_vendor(self):
        """The live path here: the supplier's account goes on the supplier."""
        collected = self._collected()

        self.env["account.edi.xml.ubl_bis3"]._import_ubl_retrieve_partner_bank(collected)

        self.assertFalse(
            self._company_banks(),
            "the vendor's IBAN must not be attached to the company partner",
        )
        self.assertTrue(
            self.Bank.search(
                [
                    ("sanitized_acc_number", "=", VENDOR_IBAN),
                    ("partner_id", "=", self.vendor.id),
                ]
            ),
            "the vendor's IBAN belongs on the vendor",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_new_hook_states_no_account_on_a_received_credit_note(self):
        """``partner_bank_id`` is left to ``_compute_partner_bank_id``, which on
        an ``in_refund`` picks OUR account — the one the money comes back to."""
        collected = self._collected()

        self.env["account.edi.xml.ubl_bis3"]._import_ubl_retrieve_partner_bank(collected)

        self.assertFalse(
            collected["to_write"].get("partner_bank_id"),
            "a received credit note must not state an account taken from the file",
        )
        self.assertFalse(collected["partner_bank_values"]["partner_banks"])

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_new_hook_leaves_an_ordinary_bill_to_core(self):
        """Only ``in_refund`` is diverted. On a bill we pay, core's behaviour —
        the vendor's account, stated on the document — is exactly right."""
        collected = self._collected(move_type="in_invoice")

        self.env["account.edi.xml.ubl_bis3"]._import_ubl_retrieve_partner_bank(collected)

        self.assertTrue(
            collected["to_write"].get("partner_bank_id"),
            "a bill we pay must still state the vendor's account",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_new_hook_reads_the_partner_from_customer_values(self):
        """🔴 Guards the key spelling.

        Core's own version of this hook reads ``collected_values['customer']``,
        while the step that resolves the partner writes
        ``customer_values['customer']``. Ours reads the latter — the one that is
        actually populated on this build. If a future core moves the value and
        this silently starts reading None, the account would stop being recorded
        against the vendor. Here the ONLY place the partner appears is
        ``customer_values``, and the move carries no partner, so the test fails
        if the spelling is "corrected" to core's.
        """
        collected = self._collected()
        self.assertFalse(collected["invoice"].partner_id)

        self.env["account.edi.xml.ubl_bis3"]._import_ubl_retrieve_partner_bank(collected)

        self.assertTrue(
            self.Bank.search(
                [
                    ("sanitized_acc_number", "=", VENDOR_IBAN),
                    ("partner_id", "=", self.vendor.id),
                ]
            ),
            "the partner was not found under customer_values['customer']",
        )

    # ------------------------------------------------------------------
    # The older decoder's hook
    #
    # Core before 2026-04 — which an older 19.0 nightly runs, and which this
    # tree does NOT — resolves the bank through
    # ``account.edi.common._import_partner_bank`` and never reaches the hook
    # the tests above go through. This core still has the method (Factur-X and
    # UBL 2.0 reach it), so it is called directly: these hold on either core.
    # ------------------------------------------------------------------

    def _refund(self, move_type="in_refund"):
        return self.env["account.move"].create(
            {"move_type": move_type, "partner_id": self.vendor.id}
        )

    def test_older_hook_records_the_iban_on_the_vendor(self):
        """The reported case: the number goes on the vendor, not on us."""
        refund = self._refund()

        self.env["account.edi.common"]._import_partner_bank(refund, [VENDOR_IBAN])

        self.assertFalse(
            self._company_banks(),
            "the vendor's IBAN must not be attached to the company partner",
        )
        vendor_bank = self.Bank.search(
            [("sanitized_acc_number", "=", VENDOR_IBAN), ("partner_id", "=", self.vendor.id)]
        )
        self.assertTrue(vendor_bank, "the vendor's IBAN belongs on the vendor")
        self.assertNotEqual(refund.partner_bank_id, vendor_bank)

    def test_older_hook_leaves_an_archived_company_row_archived(self):
        """The older core un-archives a matching row on the company partner,
        which is why archiving the stray accounts by hand never held."""
        archived = self.Bank.create(
            {"acc_number": VENDOR_IBAN, "partner_id": self.company.partner_id.id}
        )
        archived.active = False

        self.env["account.edi.common"]._import_partner_bank(
            self._refund(), [VENDOR_IBAN]
        )

        self.assertFalse(archived.active, "an archived stray row stays archived")

    def test_older_hook_ignores_a_sender_resolved_to_the_company(self):
        """Seller and buyer swapped: the document's sender is us. The IBAN must
        still not land on the company partner."""
        refund = self.env["account.move"].create(
            {"move_type": "in_refund", "partner_id": self.company.partner_id.id}
        )

        self.env["account.edi.common"]._import_partner_bank(refund, [VENDOR_IBAN])

        self.assertFalse(self._company_banks())

    def test_older_hook_does_not_duplicate_a_known_vendor_account(self):
        self.Bank.create({"acc_number": VENDOR_IBAN, "partner_id": self.vendor.id})

        self.env["account.edi.common"]._import_partner_bank(
            self._refund(), [VENDOR_IBAN]
        )

        self.assertEqual(
            self.Bank.search_count(
                [("sanitized_acc_number", "=", VENDOR_IBAN), ("partner_id", "=", self.vendor.id)]
            ),
            1,
        )

    # ------------------------------------------------------------------
    # A misread IBAN
    # ------------------------------------------------------------------

    def test_iban_checksum(self):
        self.assertFalse(iban_checksum_fails(VENDOR_IBAN))
        self.assertFalse(iban_checksum_fails("FI29 1234 5600 0045 21"))
        self.assertTrue(iban_checksum_fails(MISREAD_IBAN))
        # Claude's odd spacing of the correct number: still valid.
        self.assertFalse(iban_checksum_fails("FI29123456000045 21"))
        # Not shaped like an IBAN: a domestic number is not ours to judge.
        self.assertFalse(iban_checksum_fails("630830900"))
        self.assertFalse(iban_checksum_fails(None))

    def test_misread_iban_never_becomes_a_bank_account(self):
        """On a bill the account is recorded on the vendor and stated on the
        document — the worst place for a number that does not exist."""
        extracted = self._extracted(doc_type="Invoice")
        extracted["seller"]["iban"] = MISREAD_IBAN

        move = self._import(extracted)

        self.assertFalse(
            self.Bank.with_context(active_test=False).search(
                [("sanitized_acc_number", "=", MISREAD_IBAN)]
            ),
            "a number failing the IBAN checksum is recorded on nobody",
        )
        self.assertFalse(move.partner_bank_id)
        self.assertEqual(extracted["seller"]["iban_rejected"], MISREAD_IBAN)
        self.assertTrue(
            move.message_ids.filtered(lambda m: MISREAD_IBAN in (m.body or "")),
            "the bill says why it carries no vendor account",
        )
