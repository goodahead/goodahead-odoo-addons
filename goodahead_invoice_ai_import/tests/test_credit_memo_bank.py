# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A received credit note must not put the vendor's bank account on the company.

Core reads the bank details of an ``in_refund`` as the company's own, which is true
only of credit notes the company issues. These tests run the real decoder and check
which partner holds the account and which account the document states.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.models.account_move import (
    iban_checksum_fails,
)
from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
    PROFILE_SEARCH_PLAN_18,
)

from .profiles import skip_method_unless_profile

VENDOR_IBAN = "FI2912345600004521"
# A misread with one digit too many, failing the IBAN checksum.
MISREAD_IBAN = "FI29123456000004521"
# Any valid Finnish IBAN that is not the vendor's.
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
                "vat": self.company.vat or "FI20000002",
                "country_code": "FI",
            },
            "payment_reference": "12345672",
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
            # The real entry point, so the IBAN checksum runs as in a real import.
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
        """Archive the company partner's existing accounts."""
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
        """The supplier's account goes on the supplier, and never on us.

        Since the AI reader looks bank accounts up only (security fix), "goes
        on the supplier" means: it waits for a person as a sensitive
        suggestion on the supplier; nothing is created from the PDF."""
        move = self._import(self._extracted())

        self.assertEqual(move.move_type, "in_refund")
        self.assertFalse(
            self._company_banks(),
            "the vendor's IBAN must not be attached to the company partner",
        )
        self.assertFalse(
            self.Bank.with_context(active_test=False).search(
                [("sanitized_acc_number", "=", VENDOR_IBAN)]
            ),
            "no bank account is created from a PDF",
        )
        suggestion = self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", move.id), ("field_name", "=", "bank_account"),
             ("state", "=", "pending")]
        )
        self.assertEqual(suggestion.partner_id, self.vendor,
                         "the vendor's IBAN is suggested for the vendor")
        self.assertEqual(suggestion.invoice_value, VENDOR_IBAN)
        self.assertFalse(
            move.partner_bank_id.sanitized_acc_number == VENDOR_IBAN,
            "a credit note is refunded into our account, not the vendor's",
        )

    def test_credit_note_ignores_a_vendor_iban_already_on_the_company(self):
        """A vendor IBAN already left on the company partner is not stated on the bill.

        Other company accounts are archived so the polluted one is the only candidate.
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
        """On an ordinary bill the vendor's account is stated.

        The vendor holds the account already: since the security fix an AI
        reading only looks a bank account up on the vendor."""
        self.Bank.create({"acc_number": VENDOR_IBAN, "partner_id": self.vendor.id})
        move = self._import(self._extracted(doc_type="Invoice"))

        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(
            move.partner_bank_id.sanitized_acc_number,
            VENDOR_IBAN,
            "a bill we pay must state the vendor's account",
        )
        self.assertEqual(move.partner_bank_id.partner_id, self.vendor)

    def test_bill_keeps_the_vendor_account_despite_a_stray_copy_on_the_company(self):
        """A stray copy of the vendor IBAN on the company does not strip it off a bill.

        The number check applies only when the company is the payee; on a bill
        ownership alone decides.

        The vendor's own row exists before the import: since the security fix
        an AI reading only looks a bank account up on the vendor.
        """
        self.Bank.create(
            {"acc_number": VENDOR_IBAN, "partner_id": self.company.partner_id.id}
        )
        self.Bank.create({"acc_number": VENDOR_IBAN, "partner_id": self.vendor.id})

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

    def test_the_import_note_does_not_trigger_cores_attachment_decode(self):
        """The token-usage note carries `extraction.json`; on 18.0 posting an
        attachment makes core try to decode it into the bill, and core then
        says the bill "already contains lines". That note must not appear."""
        move = self._import(self._extracted(doc_type="Invoice"))

        bodies = " ".join(move.message_ids.mapped("body"))
        self.assertIn("Imported via AI invoice extraction", bodies)
        self.assertNotIn("already contains lines", bodies)

    def test_a_genuine_e_invoice_keeps_cores_bank_handling(self):
        """Without the AI marker, `_import_partner_bank` is core's: a vendor
        with a portal login still gets its IBAN recorded and stated."""
        self.env["res.users"].create(
            {
                "name": "ZZ Vendor Portal",
                "login": "zz-vendor-portal-bank",
                "partner_id": self.vendor.id,
                "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])],
            }
        )
        self.Bank.with_context(active_test=False).search(
            [("partner_id", "=", self.vendor.id)]
        ).unlink()
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.vendor.id}
        )

        self.env["account.edi.xml.ubl_bis3"]._import_partner_bank(move, [VENDOR_IBAN])

        self.assertEqual(move.partner_bank_id.sanitized_acc_number, VENDOR_IBAN)
        self.assertEqual(move.partner_bank_id.partner_id, self.vendor)

    def _import_with_the_iban_held_by_an_employee(self):
        employee = self.env["res.partner"].create({"name": "ZZ Employee"})
        self.env["res.users"].create(
            {"login": "zz-employee-iban", "name": "ZZ Employee", "partner_id": employee.id}
        )
        employee_row = self.Bank.create(
            {"acc_number": VENDOR_IBAN, "partner_id": employee.id}
        )
        self.Bank.with_context(active_test=False).search(
            [("partner_id", "=", self.vendor.id)]
        ).unlink()
        return self._import(self._extracted(doc_type="Invoice")), employee_row

    def test_a_bill_never_states_an_account_held_by_an_employee(self):
        """Whatever the build, the bill is never paid into the employee's row."""
        move, employee_row = self._import_with_the_iban_held_by_an_employee()

        self.assertNotEqual(move.partner_bank_id, employee_row)
        self.assertEqual(employee_row.partner_id.name, "ZZ Employee")

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN_18)
    def test_an_iban_held_by_an_employee_is_only_named_in_the_chatter_on_18_search_plan(
        self,
    ):
        """18.0 builds from April 2026 on look the number up among the vendor's
        own accounts only. Since the AI reader never creates a bank account
        (security fix), a number registered to somebody else is neither copied
        onto the vendor, nor stated on the bill, nor offered as a suggestion:
        the chatter names whose it is."""
        move, employee_row = self._import_with_the_iban_held_by_an_employee()

        self.assertFalse(move.partner_bank_id, "no account is stated on the bill")
        self.assertFalse(
            self.Bank.with_context(active_test=False).search(
                [("partner_id", "=", self.vendor.id)]
            ),
            "the employee's number is not copied onto the vendor",
        )
        self.assertEqual(employee_row.partner_id.name, "ZZ Employee")
        self.assertFalse(
            self.env["goodahead.vendor.data.suggestion"].search(
                [("move_id", "=", move.id), ("field_name", "=", "bank_account")]
            ),
            "a number registered to another partner is not offered as a suggestion",
        )
        self.assertTrue(
            any(
                "ZZ Employee" in (message.body or "")
                for message in move.message_ids
            ),
            "the chatter names who holds the number",
        )

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_an_iban_held_by_an_employee_is_not_copied_onto_the_vendor(self):
        """Only stray copies on our own company partner are ignored. On the
        legacy build a number another person holds (here an employee) keeps
        core's refusal: the bill gets no account and none is created on the
        vendor."""
        move, _employee_row = self._import_with_the_iban_held_by_an_employee()

        self.assertFalse(move.partner_bank_id)
        self.assertFalse(
            self.Bank.with_context(active_test=False).search(
                [("partner_id", "=", self.vendor.id)]
            ),
            "an IBAN someone else holds must not be created on the vendor",
        )

    # ------------------------------------------------------------------
    # The search-plan decoder's hook, called directly
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

    # `_import_ubl_retrieve_partner_bank` does not exist on the legacy build, so
    # these are gated per method; the `test_older_hook_*` tests run on both.
    @skip_method_unless_profile(PROFILE_SEARCH_PLAN, PROFILE_SEARCH_PLAN_18)
    def test_new_hook_records_the_iban_on_the_vendor(self):
        """The supplier's account goes on the supplier."""
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

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN, PROFILE_SEARCH_PLAN_18)
    def test_new_hook_states_no_account_on_a_received_credit_note(self):
        """``partner_bank_id`` is left to the compute, which picks the company's account."""
        collected = self._collected()

        self.env["account.edi.xml.ubl_bis3"]._import_ubl_retrieve_partner_bank(collected)

        self.assertFalse(
            collected["to_write"].get("partner_bank_id"),
            "a received credit note must not state an account taken from the file",
        )
        self.assertFalse(collected["partner_bank_values"]["partner_banks"])

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN, PROFILE_SEARCH_PLAN_18)
    def test_new_hook_leaves_an_ordinary_bill_to_core(self):
        """Only ``in_refund`` is diverted; bills keep core's behaviour."""
        collected = self._collected(move_type="in_invoice")

        self.env["account.edi.xml.ubl_bis3"]._import_ubl_retrieve_partner_bank(collected)

        self.assertTrue(
            collected["to_write"].get("partner_bank_id"),
            "a bill we pay must still state the vendor's account",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN, PROFILE_SEARCH_PLAN_18)
    def test_new_hook_reads_the_partner_from_customer_values(self):
        """The hook reads the partner from ``customer_values['customer']``.

        That is where the partner-resolving step writes it, unlike the key core's
        own hook reads. Here it is the only place the partner appears.
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
    # The legacy decoder's hook
    #
    # Odoo 18.0 (the legacy pipeline) resolves the bank through
    # ``account.edi.common._import_partner_bank``. Search-plan cores still have
    # it (Factur-X and UBL 2.0 reach it), so these run on either pipeline.
    # ------------------------------------------------------------------

    def _refund(self, move_type="in_refund"):
        return self.env["account.move"].create(
            {"move_type": move_type, "partner_id": self.vendor.id}
        )

    def test_older_hook_records_the_iban_on_the_vendor(self):
        """The number goes on the vendor, not on the company."""
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
        """Core alone would un-archive a matching row on the company partner."""
        archived = self.Bank.create(
            {"acc_number": VENDOR_IBAN, "partner_id": self.company.partner_id.id}
        )
        archived.active = False

        self.env["account.edi.common"]._import_partner_bank(
            self._refund(), [VENDOR_IBAN]
        )

        self.assertFalse(archived.active, "an archived stray row stays archived")

    def test_older_hook_ignores_a_sender_resolved_to_the_company(self):
        """With the sender resolved to the company, the IBAN still stays off it."""
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
        # Odd spacing of the correct number is still valid.
        self.assertFalse(iban_checksum_fails("FI29123456000045 21"))
        # Not shaped like an IBAN: a domestic number is not ours to judge.
        self.assertFalse(iban_checksum_fails("987654320"))
        self.assertFalse(iban_checksum_fails(None))

    def test_misread_iban_never_becomes_a_bank_account(self):
        """An IBAN failing its checksum is recorded on nobody and stated nowhere."""
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
