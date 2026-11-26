# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The older core invents a vendor too, and nothing in this module could see it.

🔴 WHAT THIS FILE IS ABOUT. "Never invent" is a product decision,
and it is a POLICY rather than a setting — there is no per-company opt-out here
and no degraded mode. On the search-plan core the place core invents is
`account.edi.ubl._import_ubl_create_missing_customer`, which this module has
suppressed since 19.0.3.0.0. On the legacy core that method does not exist at
all; core creates in a different place, `account.edi.common._import_partner`,
guarded by `if not partner and name and vat` and creating one line later
(`account_edi_common.py:545` / `:549` at `ca1f0aea`).

So until 19.0.3.7.0 the module's central promise — *a bill nobody filed is
visible; a bill filed against the wrong vendor is not* — was simply FALSE on
half its installed base, silently, and the chatter note saying nobody was
invented would have been wrong on exactly the bills where it mattered.

🔴 WHY SUPPRESSION IS WHAT SWITCHES THE REST ON. The post-decode net —
`_create_partner_from_extraction` -> `_ai_post_unrecognised_vendor` -> the
candidate cards — is gated on an EMPTY `partner_id` (`account_move.py:369`).
Core filling it with something it minted does not merely add a row; it silences
the entire recovery path. That is why the tests below assert the NET appears,
not only that no `res.partner` was written.

Three controls make the assertions mean something, and each can fail:

  * without the `ai_invoice_import` context key, core still creates — so the
    override is the difference, measured rather than argued;
  * a vendor that DOES match still comes back, with core's address filling
    intact — so "suppressed" has not quietly become "broken";
  * on the search-plan build a full import enters `_import_partner` ZERO times,
    counted on a real run rather than read off the call graph.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
)

from .profiles import skip_method_unless_profile, skip_unless_profile

# A German VAT that passes `base_vat`'s check, on a vendor nobody has. German
# rather than Finnish because the legacy bench's company is Finnish and a
# same-country number invites an accidental match on the company itself.
UNKNOWN_VAT = "DE100000008"
UNKNOWN_NAME = "ZZ Never Invented GmbH"


class _SuppressionHarness(TransactionCase):
    """A document from a vendor nobody has, printing a tax id.

    🔴 The tax id is the fixture. Core's create guard is `name and vat`, so a
    document printing no tax id never reaches the create at all and would pass
    these tests with the override deleted. Most vendor bills of the kind this
    module receives print none; one that prints an EIN is the kind that minted a
    duplicate.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        cls.EdiCommon = cls.env["account.edi.common"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)],
            limit=1,
        )

    # -- fixtures ------------------------------------------------------

    def _extracted(self, name=UNKNOWN_NAME, vat=UNKNOWN_VAT, iban=None):
        seller_bank = {"iban": iban} if iban else {}
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-NOINVENT-1",
            "issue_date": "2026-09-30",
            "currency": "EUR",
            "seller": {
                "name": name,
                "vat": vat,
                "country_code": "DE",
                "phone": "+49 30 5550199",
                "email": "ap@zz-never-invented.example",
                "city": "Berlin",
                **seller_bank,
            },
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "FI",
            },
            "lines": [
                {
                    "product_code": "ZZ-NI-1",
                    "name": "ZZ Widget",
                    "quantity": 1.0,
                    "unit_code": "C62",
                    "price_unit": 100.0,
                    "line_total": 100.0,
                    "tax_percent": 0.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 0.0,
                    "taxable_amount": 100.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "totals": {
                "line_extension": 100.0,
                "tax_exclusive": 100.0,
                "tax_inclusive": 100.0,
                "payable": 100.0,
            },
        }

    def _run_import(self, extracted=None):
        extracted = extracted or self._extracted()
        attachment = self.env["ir.attachment"].create(
            {
                "name": "zz-noinvent.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        record = self.env["goodahead.invoice.import"].create(
            {
                "name": "zz-noinvent.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": attachment.id,
            }
        )
        with patch.object(
            type(self.AM),
            "_ai_extract_and_generate_ubl",
            return_value={
                "extracted": extracted,
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "model_name": "test-model",
                "xml_bytes": generate_peppol_invoice_xml(extracted),
            },
        ):
            outcome = record._process_import()
        return record, outcome

    def _partner_count(self):
        return self.Partner.with_context(active_test=False).search_count([])

    def _import_partner(self, ours=True, **overrides):
        """Call core's seam the way `_import_fill_invoice` calls it."""
        vals = {
            "name": UNKNOWN_NAME,
            "phone": "+49 30 5550199",
            "email": "ap@zz-never-invented.example",
            "vat": UNKNOWN_VAT,
            "postal_address": {"country_code": "DE", "city": "Berlin"},
        }
        vals.update(overrides)
        edi = self.EdiCommon.with_context(ai_invoice_import=True) if ours else self.EdiCommon
        return edi._import_partner(self.company, **vals)

    def _counting_import_partner(self):
        """Wrap `account.edi.common._import_partner` and record every entry."""
        edi_cls = type(self.EdiCommon)
        original = edi_cls._import_partner
        calls = []

        def counting(this, company_id, name, phone, email, vat, **kwargs):
            calls.append(name)
            return original(this, company_id, name, phone, email, vat, **kwargs)

        return calls, patch.object(edi_cls, "_import_partner", counting)


@tagged("post_install", "-at_install")
class TestTheLegacyCreateIsSuppressed(_SuppressionHarness):
    """🔴 THIS CLASS USED TO BE GATED `legacy` WHOLE, AND THAT WAS a review finding,
    FINDING F6 — seven of its nine tests were gated into silence.

    Seven call `account.edi.common._import_partner` DIRECTLY, and that method,
    its ``if not partner and name and vat`` create guard, its two log strings
    and its post-match address fill are the same on both cores
    (`account_edi_ubl_cii/models/account_edi_common.py:640` at `189df353`,
    `:545` at `ca1f0aea`); this module's override branches only on the context
    key and on ``name and vat``. So they were portable all along, and on the
    search-plan bench they were not running.

    The worst loss was `test_a_vendor_reachable_only_by_its_peppol_pair_still_
    matches`: it is the only tripwire on `_ai_core_peppol_domain`, which is a
    SECOND SPELLING of four lines of core, and a copied rule whose tripwire only
    fires on one of two benches is a copied rule with half a tripwire.

    Only the two that drive a FULL import are gated now, per method, because on
    the search-plan build the import never reaches this seam at all — which
    `TestTheLegacySuppressionIsInertOnTheSearchPlanBuild` below asserts by
    counting, on a real run.
    """

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_a_full_import_of_an_unknown_vendor_creates_nobody(self):
        before = self._partner_count()

        record, outcome = self._run_import()

        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertTrue(record.move_id, "the bill itself must still be created")
        self.assertFalse(
            record.move_id.partner_id,
            "an unrecognised vendor leaves the bill empty, on purpose",
        )
        self.assertEqual(
            self._partner_count(),
            before,
            "no res.partner may be created for an unrecognised vendor",
        )

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_the_unrecognised_vendor_net_appears(self):
        """🔴 Suppression is what SWITCHES THIS ON. The note and the candidate
        cards are gated on an empty `partner_id`; a vendor core minted silences
        them, which is worse than the duplicate row."""
        record, outcome = self._run_import()
        move = record.move_id

        notes = move.message_ids.filtered(
            lambda m: "not recognised" in (m.body or "")
        )
        self.assertEqual(len(notes), 1, "exactly one note, naming what was read")
        self.assertIn(UNKNOWN_NAME, notes.body)
        # The values, not the labels — the labels are translated and this bench
        # is a Finnish database.
        self.assertIn(UNKNOWN_VAT, notes.body)
        self.assertIn("ap@zz-never-invented.example", notes.body)
        self.assertIn("No new contact was created for it on purpose", notes.body)

    def test_a_suppressed_bill_carrying_a_payee_account_still_imports(self):
        """🔴 a review finding — the suppressed path had never run with
        bank details on it.

        Every other fixture here carries no `iban`, no `account_number` and no
        `payment_reference`, so `adapters/ubl_generator.py:226` never emitted a
        `PaymentMeans` block and core's bank step was never reached with an
        EMPTY `partner_id`. That looked certain to raise: `res.partner.bank.
        partner_id` is `required=True`, and core creates one from the document.

        It does not raise, and the reason is worth the test rather than the
        reading: core first tests ``partner_bank.partner_id == partner``
        (`account_edi_common.py:584` at `ca1f0aea`), and with BOTH sides empty
        recordsets that is True, so core returns early and creates nothing. The
        vendor's number is simply dropped, which is correct with no vendor —
        but it is correct by an accident of recordset equality, and nothing
        asserted it. Now something does, on both benches.
        """
        before = self._partner_count()
        Bank = self.env["res.partner.bank"].with_context(active_test=False)
        iban = "DE89370400440532013000"

        record, outcome = self._run_import(self._extracted(iban=iban))

        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertTrue(record.move_id, "the bill itself must still be created")
        self.assertFalse(record.move_id.partner_id)
        self.assertEqual(self._partner_count(), before)
        self.assertFalse(
            Bank.search([("acc_number", "=", iban)]),
            "no bank account may be created for a vendor that does not exist",
        )
        # CONTROL: the document really did carry a payee account, so an
        # extraction that quietly dropped it cannot pass this.
        self.assertIn(
            iban.encode(),
            self.AM._ai_generate_ubl_from_extracted(
                self._extracted(iban=iban), self.company
            ),
        )

    def test_the_suppressed_call_returns_cores_shape(self):
        """⚠️ THE TRAP. `_import_fill_invoice` does `invoice.partner_id =
        partner.id` on the very next line (`account_edi_xml_ubl_20.py:1116`), so
        the early return has to be a `(recordset, logs)` 2-tuple holding an
        EMPTY `res.partner` — not `False`, not `None`, not a bare recordset."""
        before = self._partner_count()

        result = self._import_partner()

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        partner, logs = result
        self.assertEqual(partner._name, "res.partner")
        self.assertFalse(partner)
        self.assertEqual(len(partner), 0)
        # The caller's very next statement, reproduced.
        self.assertFalse(partner.id)
        self.assertEqual(self._partner_count(), before)
        self.assertIsInstance(logs, list)

    def test_the_logs_say_it_was_deliberate_and_not_a_near_miss(self):
        """🔴 Core's `logs` are USER-VISIBLE, and a silent suppression is the
        failure mode this project keeps finding. Core's own no-match line
        ("Could not retrieve partner with details: …") would be a lie by
        omission here: core did not fail to find one, it was stopped from
        making one up."""
        _partner, logs = self._import_partner()

        joined = " ".join(logs)
        self.assertTrue(logs, "the suppressed path must say something")
        self.assertIn(UNKNOWN_NAME, joined)
        self.assertIn("left without a vendor", joined)
        self.assertNotIn("A new partner was created", joined)

    def test_core_would_have_created_one_without_our_context_key(self):
        """🔴 THE CONTROL. Without this, every assertion above would also pass
        on a core that never creates anything — and this is a measurement of
        core on this bench, not a reading of its source."""
        before = self._partner_count()

        partner, logs = self._import_partner(ours=False)

        self.assertTrue(partner, "core must still invent one when we do not stop it")
        self.assertEqual(partner.name, UNKNOWN_NAME)
        self.assertEqual(self._partner_count(), before + 1)
        self.assertIn("A new partner was created", " ".join(logs))

    def test_a_vendor_that_matches_still_comes_back(self):
        """🔴 THE SECOND CONTROL. An override that returned an empty recordset
        unconditionally would pass every test above and break every import."""
        vendor = self.Partner.create(
            {
                "name": UNKNOWN_NAME,
                "is_company": True,
                "supplier_rank": 3,
                "country_id": self.env.ref("base.de").id,
                "vat": UNKNOWN_VAT,
            }
        )
        before = self._partner_count()

        partner, _logs = self._import_partner()

        self.assertEqual(partner, vendor)
        self.assertEqual(self._partner_count(), before)

    def test_a_matched_vendor_still_gets_cores_address_filling(self):
        """Core fills an empty address from the document after a match. That is
        core's behaviour on a vendor we already have, it is not create, and the
        suppression must not cost it — which is why the match path goes through
        `super()` rather than returning the partner we just found."""
        vendor = self.Partner.create(
            {"name": UNKNOWN_NAME, "is_company": True, "vat": UNKNOWN_VAT}
        )

        partner, _logs = self._import_partner()

        self.assertEqual(partner, vendor)
        self.assertEqual(partner.city, "Berlin")
        self.assertEqual(partner.country_id, self.env.ref("base.de"))

    def test_a_vendor_reachable_only_by_its_peppol_pair_still_matches(self):
        """🔴 THE TRIPWIRE FOR THE ONE COPIED RULE.

        `_ai_core_peppol_domain` is a second spelling of four lines of core
        (`account_edi_common.py:530-534`), and a copied rule with nothing
        watching it is how two spellings drift apart. This vendor is reachable
        through NOTHING else in core's chain — a different name, a different
        VAT, no phone, no email — so the moment the domain stops agreeing with
        core's, the override stops handing it to `super()`, returns empty, and
        this test fails.
        """
        vendor = self.Partner.create(
            {
                "name": "ZZ Peppol Only Oy",
                "is_company": True,
                "peppol_eas": "0007",
                "peppol_endpoint": "1234567890",
            }
        )
        before = self._partner_count()

        partner, _logs = self._import_partner(
            peppol_eas="0007", peppol_endpoint="1234567890"
        )

        self.assertEqual(
            partner,
            vendor,
            "a vendor core would have found through the Peppol domain must "
            "still be found through ours",
        )
        self.assertEqual(self._partner_count(), before)

    def test_a_document_with_no_tax_id_is_left_entirely_to_core(self):
        """The override intervenes only where core could invent. With no VAT
        core's own guard already declines, so `super()` runs untouched and the
        logs are core's."""
        before = self._partner_count()

        partner, logs = self._import_partner(vat=None)

        self.assertFalse(partner)
        self.assertEqual(self._partner_count(), before)
        self.assertIn("Could not retrieve partner with details", " ".join(logs))


@skip_unless_profile(PROFILE_SEARCH_PLAN)
@tagged("post_install", "-at_install")
class TestTheLegacySuppressionIsInertOnTheSearchPlanBuild(_SuppressionHarness):
    """🔴 Inert here is a COUNT from a real import, not an argument from reading
    the call graph. `_ubl_import_invoice` drives the search plan and calls
    neither `_import_partner` nor `_retrieve_partner`; this is what proves it."""

    def test_a_full_import_never_enters_the_legacy_seam(self):
        before = self._partner_count()
        calls, counting = self._counting_import_partner()

        with counting:
            record, outcome = self._run_import()

        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertEqual(
            calls,
            [],
            "the legacy create-suppression must never be entered on this build",
        )
        # 🔴 THE CONTROL: the import really ran, and the vendor really was
        # suppressed — here by `_import_ubl_create_missing_customer`, which is
        # where this build invents. An empty call list is also what a dead
        # import would produce.
        self.assertTrue(record.move_id)
        self.assertFalse(record.move_id.partner_id)
        self.assertEqual(self._partner_count(), before)
