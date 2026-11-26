# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A document's vendor is never one of our own companies — and neither is its bank.

🔴 WHAT THIS FILE IS ABOUT, MEASURED RATHER THAN IMAGINED.

The case, on a single-company test database (`ZZ Buyer Oy`, whose company
partner is partner 1):

  * bill A, an `in_invoice` from *Goodahead OÜ*, resolved its VENDOR to
    partner 1 — the buyer's OWN company partner;
  * `res_partner_bank` row R, ``BE07 1234 5600 0066`` — the SELLER's IBAN,
    printed on that document — was created against partner 1.

The extraction was **right**: its `seller` block reads
``{"name": "Goodahead OÜ", "vat": "EE100000000", "phone": "+358400000444", …}``
and its `buyer` block reads ``{"name": "Goodahead Oy", "vat": "FI00000158"}``.
Nothing was swapped. What matched was the PHONE: the seller printed
``+358400000444``, which is byte-identical to the phone on partner 1.

Core's legacy chain (`account/models/partner.py:958` at `ca1f0aea`) runs each of
its four steps TWICE — once company-scoped, once with ``company_id = False`` —
and accepts a step's answer when exactly one row comes back. On the
company-scoped pass that phone matched four employee/related contacts, so
``len(partner) == 1`` declined. On the ``company_id = False`` pass it matched
exactly one row: the company's own partner. That is the whole mechanism, and
the IBAN followed the vendor — core's `_import_partner_bank` takes
``invoice.partner_id`` for an `in_invoice` (`in_invoice` is NOT in
``get_inbound_types``), which by then was partner 1.

🔴 SO THERE IS ONE ROOT CAUSE, NOT TWO, AND IT IS AT PARTNER RESOLUTION.
The bank-side guards are the second line of defence, and they are tested here
too because the first line cannot be the only one: core has a
`bank_account_number` search step of its own on the newer pipeline, which reads
back exactly the stray rows this bug leaves behind.

🔴 AND THE RULE WAS NEVER "LOST" — IT WAS NEVER COMPLETE. `goodahead.partner.
identifier._drop_partners_that_are_never_a_vendor` has said *"We are never a
vendor on our own bill. This is not a heuristic."* since 19.0.3.0.0, measured
against both US-deployment company partners. But it lives inside the three steps
THIS module inserts, and core resolves a partner through its own vat /
eas_endpoint / bank_account_number / email / phone / name steps, none of which
pass through it. The guard could not fire because core had already decided.
What 19.0.3.23.0 adds is the same body at core's seam, nothing more.

⚠️ AND THE BANK ROW IS A LEGACY-CORE DEFECT ONLY. The newer core refuses it
one level down: `res.partner.bank._find_or_create_bank_account` raises *"Please
add your own bank account manually"* for a `res.company.partner_id`
(`base/models/res_bank.py:209-214` at `189df353`). The legacy core has neither
that protection nor `_get_company_partner_ids`, and does a bare ``create()``.
Row R could not exist on the US-deployment build; the wrong VENDOR could.

⚠️ WHY THE GUARD IS SCOPED TO `ai_invoice_import` AND NOT APPLIED TO EVERY
DOCUMENT. The US deployment is a TWO-COMPANY database (`Alpha LLC` and
`Beta Inc.`): a Beta customer order becomes a Beta purchase order to
Alpha, so a genuine intercompany invoice's counterparty legitimately IS
another company's partner, and real orders travel that path. An unscoped
refusal would break it. A model's reading of a PDF is a different thing from a
structured document a counterparty sent, which is the same sentence the three
sibling overrides in this module already carry.

Controls, each of which can fail and each of which is asserted below:

  * ``test_control_without_our_context_core_still_picks_the_company_partner`` —
    the match is REAL and the override is the difference. If this one ever
    fails, every other test here passes for the wrong reason.
  * ``test_control_a_genuine_vendor_still_matches`` — "refuses the company" has
    not quietly become "refuses everybody".
  * ``test_control_a_genuine_vendors_iban_is_still_recorded`` — widening the
    bank hook to `in_invoice` has not stopped it doing its job.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.base.models.res_bank import sanitize_account_number
from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_import.models.core_profile import PROFILE_LEGACY

from .profiles import skip_method_unless_profile

#: The phone the trap is built on. Put on the COMPANY partner in `setUpClass`
#: and printed as the SELLER's phone on the document — which is exactly the
#: shape of bill A above, where the seller prints the same number the buyer's
#: own company partner carries.
TRAP_PHONE = "+358501119999"

#: A German VAT that passes `base_vat`'s check and belongs to nobody here.
#: German rather than Finnish so it cannot match the (Finnish) company by VAT —
#: the point is to reach the PHONE step, not to short-circuit at step 1.
UNKNOWN_VAT = "DE100000008"

#: A name that resembles nothing in the database, so core's name step cannot
#: fire either.
TRAP_NAME = "ZZ Own Company Trap OU"

#: A valid Belgian IBAN (the canonical documentation example). NOT the real
#: number from bill A — a live account number does not belong in a fixture.
TRAP_IBAN = "BE68 5390 0754 7034"

#: A second valid IBAN, for the genuine-vendor control.
OTHER_IBAN = "BE71 0961 2345 6769"


class _OwnCompanyTrapHarness(TransactionCase):
    """A document whose seller shares a contact key with our own company."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.company_partner = cls.company.partner_id
        # The trap. One write, and it is the only fixture that matters: after
        # it, core's phone step resolves this number to the company partner
        # alone on its `company_id = False` pass.
        cls.company_partner.phone = TRAP_PHONE
        cls.Partner = cls.env["res.partner"]
        cls.Bank = cls.env["res.partner.bank"]
        cls.AM = cls.env["account.move"]
        cls.EdiCommon = cls.env["account.edi.common"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)],
            limit=1,
        )

    # -- fixtures ------------------------------------------------------

    def _seller(self, name=TRAP_NAME, vat=UNKNOWN_VAT, phone=TRAP_PHONE,
                iban=TRAP_IBAN):
        """🔴 THE `seller` BLOCK ON ITS OWN.

        It exists because `_extracted()` returns the whole extraction and three
        tests here handed THAT to `res.partner._ai_find_vendor` and
        `account.move._ai_vendor_candidates`, which read ``seller["name"]`` and
        ``seller["vat"]`` — keys the outer dict does not have. All three passed,
        proving nothing. A matcher handed an unreadable seller always refuses.
        """
        seller = {
            "name": name,
            "vat": vat,
            "country_code": "DE",
            "city": "Berlin",
            "street": "Beispielweg 1",
        }
        if phone:
            seller["phone"] = phone
        if iban:
            seller["iban"] = iban
        return seller

    def _extracted(self, **seller_kwargs):
        """The whole extraction, built around `_seller(**seller_kwargs)`."""
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-OWNCO-1",
            "issue_date": "2026-09-30",
            "currency": "EUR",
            "seller": self._seller(**seller_kwargs),
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "FI",
            },
            "lines": [
                {
                    "product_code": "ZZ-OC-1",
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
                "name": "zz-ownco.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        record = self.env["goodahead.invoice.import"].create(
            {
                "name": "zz-ownco.pdf",
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

    def _import_partner(self, ours=True, **overrides):
        """Call core's legacy seam the way `_import_fill_invoice` calls it."""
        vals = {
            "name": TRAP_NAME,
            "phone": TRAP_PHONE,
            "email": None,
            "vat": UNKNOWN_VAT,
            "postal_address": {"country_code": "DE", "city": "Berlin"},
        }
        vals.update(overrides)
        edi = (
            self.EdiCommon.with_context(ai_invoice_import=True)
            if ours
            else self.EdiCommon
        )
        return edi._import_partner(self.company, **vals)

    def _bank_rows_for(self, partner, number=TRAP_IBAN):
        return self.Bank.with_context(active_test=False).search(
            [
                ("sanitized_acc_number", "=", sanitize_account_number(number)),
                ("partner_id", "=", partner.id),
            ]
        )

    def _draft_bill(self, partner=None, move_type="in_invoice"):
        return self.AM.create(
            {
                "move_type": move_type,
                "journal_id": self.journal.id,
                "partner_id": partner.id if partner else False,
            }
        )

    def _genuine_vendor(self, **overrides):
        vals = {
            "name": "ZZ Genuine Vendor GmbH",
            "is_company": True,
            "phone": "+4930555000111",
            "vat": UNKNOWN_VAT,
            "country_id": self.env.ref("base.de").id,
        }
        vals.update(overrides)
        return self.Partner.create(vals)

    def _other_company(self):
        """A SECOND company, so "ours" can be told from "any company".

        🔴 WITHOUT THIS FIXTURE THE WHOLE OWNERSHIP RULE IS UNTESTABLE. The legacy
        bench has exactly one company, so every "is this one of ours" assertion
        passes whether the rule reads *this* company or *any* company — and the
        difference is every US-deployment intercompany order.

        🔴 AN EXISTING SECOND COMPANY IS PREFERRED, AND NOT ONLY TO SAVE WORK.
        On a two-company bench the second company is `Beta Inc.` — the
        counterparty of the intercompany flow this rule exists to protect, with
        `out_invoice` rows against it. A fixture company proves the rule; an
        existing one proves the rule on the data.

        ⚠️ AND CREATING ONE IS HOSTILE ON BOTH BENCHES, FOR TWO DIFFERENT
        REASONS — both measured, neither anything to do with this module:

        * **The legacy bench**: `stock.res_company.create` has a branch that fires *only
          during a test run* (`stock/models/res_company.py:192` at `ca1f0aea`),
          ``if modules.module.current_test: self.env['stock.warehouse'].create(
          [{'company_id': company.id} …])``. A warehouse created from
          `company_id` alone gets no `code`, so `view_location_id` is never
          computed and the INSERT dies on its NOT NULL constraint. The identical
          `res.company.create` SUCCEEDS outside a test run — so this is core's
          test-only convenience failing on a database it did not seed. Muting
          the flag for that one statement is the smallest thing that removes it
          from the path, and nothing else in `res.company.create` reads it.
        * **A two-company bench**: a third company trips that database's own routing data —
          ``ValidationError: Rule Vendors → Customers belongs to ZZ Other Co Oy
          while the route belongs to ZZ Main Route``. Nothing a fixture should be
          fixing.
        """
        existing = (
            self.env["res.company"]
            .sudo()
            .search([("id", "!=", self.company.id)], limit=1)
        )
        if existing:
            return existing

        from odoo.modules import module as _module

        previous = _module.current_test
        _module.current_test = False
        try:
            return self.env["res.company"].create({"name": "ZZ Other Co Oy"})
        finally:
            _module.current_test = previous


@tagged("post_install", "-at_install")
class TestTheVendorIsNeverOurOwnCompany(_OwnCompanyTrapHarness):
    """Partner resolution. The root cause of bill A."""

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_a_full_import_never_files_the_bill_against_our_own_company(self):
        record, outcome = self._run_import()

        self.assertTrue(outcome.startswith("OK"), outcome)
        move = record.move_id
        self.assertTrue(move, "the bill itself must still be created")
        self.assertNotEqual(
            move.partner_id,
            self.company_partner,
            "a document's vendor must never be one of our own companies",
        )
        self.assertFalse(
            move.partner_id,
            "nothing else matched this seller, so the bill is left for a human",
        )

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_a_full_import_creates_no_bank_account_on_our_own_company(self):
        before = self._bank_rows_for(self.company_partner)
        self.assertFalse(before, "fixture check: the trap IBAN is not there yet")

        record, _outcome = self._run_import()

        self.assertFalse(
            self._bank_rows_for(self.company_partner),
            "the seller's IBAN must not be recorded against our own company",
        )
        self.assertFalse(
            record.move_id.partner_bank_id,
            "and the bill must not point at one either",
        )

    def test_the_legacy_seam_refuses_the_company_partner(self):
        partner, logs = self._import_partner()

        self.assertNotEqual(partner, self.company_partner)
        self.assertFalse(partner, "an empty res.partner, not False and not None")
        self.assertEqual(partner._name, "res.partner", "the return shape is the trap")
        self.assertTrue(logs, "the refusal has to be visible to the accountant")

    def test_the_refusal_does_not_depend_on_a_printed_tax_id(self):
        """🔴 NOT GATED ON ``name and vat``, unlike the create suppression.

        Core's create cannot fire without both, which is why the suppression
        next door tests for them. Core's *match* has no such guard: the phone
        step runs on any document that prints a phone. A document with no tax
        id at all therefore reaches the company partner just as bill A did.
        """
        partner, _logs = self._import_partner(vat=None)

        self.assertNotEqual(partner, self.company_partner)
        self.assertFalse(partner)

    def test_our_own_matcher_never_returns_a_company_partner_on_its_phone_step(self):
        """⚠️ THIS ONE ALREADY PASSED AT 19.0.3.22.0, AND IS HERE AS A LOCK.

        `_ai_find_vendor`'s phone step resolves through
        `goodahead.partner.identifier._commercial_partner_for`, which has
        applied the own-company rule since 19.0.3.0.0. Nothing in 19.0.3.23.0
        changes this path; the test exists so that a future refactor of the
        identifier helper cannot drop the rule silently. It is NOT evidence for
        this change — the two below are.
        """
        seller = self._extracted()["seller"]

        match = self.Partner._ai_find_vendor(seller)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_our_own_matcher_never_returns_a_company_partner_on_its_vat_step(self):
        """🔴 RED AT 19.0.3.22.0. Two of the matcher's six steps were unguarded.

        Steps 2-4 go through `goodahead.partner.identifier`, so the matcher
        LOOKED guarded. Step 1 (VAT, ``=ilike``) and step 6 (name, ``=ilike``)
        search `res.partner` directly and did not. A document printing our own
        VAT — a seller/buyer block the model read the wrong way round, which is
        the hypothesis bill A was first blamed on — went straight past it.
        """
        # ⚠️ NOT a Finnish number. The first version of this fixture used
        # `FI00000094`, which this database already holds on two OTHER partners
        # (another company and a contact of it) — so the fixture check below
        # failed and said so, instead of the test quietly measuring an
        # ambiguous VAT. `UNKNOWN_VAT` is German, valid, and belongs to nobody.
        self.company_partner.vat = UNKNOWN_VAT
        seller = self._seller(vat=self.company_partner.vat, phone=None)
        # ⚠️ NOT ``== self.company_partner``. `vat` is a COMMERCIAL field, so
        # writing it on the company partner propagates it to every child
        # contact — on the US bench that is five rows, and the first
        # version of this check failed there and said so. What has to be true is
        # that every row the key reaches is OUR OWN PARTY, which is what the
        # guard is about; how many contact rows carry it is not the subject.
        reached = self.Partner.search([("vat", "=ilike", seller["vat"])])
        self.assertIn(self.company_partner, reached, "fixture check: the key reaches us")
        self.assertEqual(
            reached.commercial_partner_id,
            self.company_partner,
            "fixture check: every row the printed VAT reaches is our own party",
        )

        match = self.Partner._ai_find_vendor(seller)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_our_own_matcher_never_returns_a_company_partner_on_its_name_step(self):
        """🔴 RED AT 19.0.3.22.0 — step 6, for the same reason as step 1."""
        # ⚠️ THE COMPANY IS RENAMED TO A SENTINEL, NOT MATCHED ON ITS REAL NAME.
        # The first version printed `self.company_partner.name` and the fixture
        # check failed on the US bench, where `name =ilike 'Alpha
        # LLC'` reaches THREE top-level partners (ids 1, <id>, 100306). An
        # ambiguous key tests core's uniqueness rule, not this guard. The write
        # is rolled back with the test.
        self.company_partner.name = "ZZ Our Own Company Oy"
        seller = self._seller(name=self.company_partner.name, vat=None, phone=None)
        self.assertEqual(
            self.Partner.search(
                [("name", "=ilike", seller["name"]), ("parent_id", "=", False)]
            ),
            self.company_partner,
            "fixture check: the printed name resolves to the company and nobody else",
        )

        match = self.Partner._ai_find_vendor(seller)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_the_company_partner_is_not_offered_as_a_candidate_by_name(self):
        """🔴 RED AT 19.0.3.22.0. The phone and domain signals were guarded; the
        three NAME searches in `_ai_vendor_candidates` were not.

        Offering it is an invitation to make the same mistake by hand:
        `vendor_candidate.action_use_this` writes `move.partner_id = candidate`
        and then applies the seller's phone, email and address to it — so a card
        for our own company is one click from BOTH halves of bill A.
        """
        self.company_partner.name = "ZZ Our Own Company Oy"
        seller = self._seller(name=self.company_partner.name, phone=None)

        candidates = self.AM._ai_vendor_candidates(seller, company=self.company)

        self.assertNotIn(
            self.company_partner,
            [c["partner"] for c in candidates],
            "our own company is never a candidate vendor",
        )

    def test_control_a_name_that_resembles_a_real_vendor_is_still_offered(self):
        """Dropping ourselves is not dropping the feature.

        🔴 AND THIS CONTROL EARNED ITS KEEP BEFORE THE FIX WAS EVEN FINISHED.
        Its first version passed ``self._extracted(...)`` — the whole extraction
        dict — where a `seller` BLOCK was wanted, so `_ai_vendor_candidates` read
        ``.get("name")`` off a dict that has no ``name`` key, found nothing and
        returned ``[]``. This control failed and said so. The three tests either
        side of it had the identical mistake and PASSED, vacuously: a guard
        cannot be proved by handing the matcher a seller it cannot read. That is
        why `_seller()` exists and why each of them now asserts its fixture
        first.
        """
        vendor = self._genuine_vendor()
        seller = self._seller(name="ZZ Genuine", vat=None, phone=None)

        candidates = self.AM._ai_vendor_candidates(seller, company=self.company)

        self.assertIn(vendor, [c["partner"] for c in candidates])

    # -- controls ------------------------------------------------------

    def test_control_without_our_context_core_still_picks_the_company_partner(self):
        """🔴 THE CONTROL THE WHOLE FILE RESTS ON.

        It proves the match is real rather than hypothetical, and that the
        override is what changes the answer. It also pins the SCOPE: core's
        behaviour on a document this module did not produce is deliberately
        unchanged, because an intercompany invoice's counterparty legitimately
        is another company's partner.
        """
        partner, _logs = self._import_partner(ours=False)

        self.assertEqual(
            partner,
            self.company_partner,
            "if this stops being true the other tests prove nothing",
        )

    def test_control_a_genuine_vendor_still_matches(self):
        vendor = self._genuine_vendor()

        partner, _logs = self._import_partner(phone=vendor.phone, vat=None)

        self.assertEqual(partner, vendor, "refusing the company is not refusing everybody")


@tagged("post_install", "-at_install")
class TestTheGuardIsScopedToTheBillsOwnCompany(_OwnCompanyTrapHarness):
    """🔴 THE RULE WAS NARROWED, AND THE NARROWING IS LOAD-BEARING.

    The first statement of the requirement: exclude any company-related contact,
    and any user as well. Then, asked what "our own" means in a two-company
    estate: for Alpha, first check the current company for a contact, and then
    accept a contact that does not belong solely to another company.

    Read literally, the FIRST would refuse **Beta Inc.** — and a Beta
    document billing Alpha LLC is the intercompany flow real orders
    travel, not a mistake. So the rule is:

      1. refuse the BILL'S OWN company partner and its contact tree;
      2. refuse any partner carrying a `res.users` row;
      3. refuse a contact OWNED by another company (`company_id` set to it);
      4. **allow** another company's own partner — `company_id = False`, a
         shared contact under the ownership rule above.

    Every test below has a control, and the controls are what distinguish "this
    company" from "any company" and "ancestry" from "commercial partner".
    """

    # -- rule 1: the ancestry, not just the partner --------------------

    def test_a_child_of_the_company_partner_is_refused(self):
        """🔴 RED. `commercial_partner_id` alone does NOT catch a grandchild
        whose parent carries ``is_company = True`` — the child is then its own
        commercial partner and the company never appears. bill A came
        within one unique phone match of being filed against a related contact,
        so the ancestry walk is the structural answer.
        """
        child = self.Partner.create(
            {"name": "ZZ Dept Of Ours", "is_company": True,
             "parent_id": self.company_partner.id}
        )
        grandchild = self.Partner.create(
            {"name": "ZZ Desk Of Ours", "parent_id": child.id}
        )
        self.assertEqual(
            grandchild.commercial_partner_id, child,
            "fixture check: commercial_partner_id stops at the is_company child",
        )

        for partner in (child, grandchild):
            self.assertEqual(
                self.Partner._ai_impossible_vendor_reason(
                    partner, company=self.company
                ),
                "own_company",
                "a contact in our own company's tree is never a vendor",
            )

    def test_control_a_vendor_with_a_parent_company_of_its_own_is_accepted(self):
        """The control that keeps rule 1 from eating the contact book.

        A test that only checked "has a parent" or "has a commercial partner"
        would refuse every contact of every vendor — a US reference database has dozens of child
        contacts under partners with `supplier_rank > 0`.
        """
        vendor = self._genuine_vendor()
        contact = self.Partner.create(
            {"name": "ZZ AP Desk", "parent_id": vendor.id}
        )

        for partner in (vendor, contact):
            self.assertFalse(
                self.Partner._ai_impossible_vendor_reason(
                    partner, company=self.company
                ),
                "an ordinary vendor and its contacts are perfectly good vendors",
            )

    # -- rule 2: a login is never a vendor -----------------------------

    def test_a_partner_carrying_a_user_is_refused(self):
        """🔴 RED. Rule 2: any user is refused as well."""
        vendor = self._genuine_vendor()
        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(vendor, company=self.company),
            "fixture check: this partner is accepted BEFORE it has a login — so "
            "the test is about the user and not about the partner",
        )

        self.env["res.users"].create(
            {"name": vendor.name, "login": "zz-ownco-vendor-login",
             "partner_id": vendor.id}
        )

        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(vendor, company=self.company),
            "own_user",
        )

    def test_a_vendor_whose_child_contact_has_a_user_is_still_accepted(self):
        """🔴 THE ANCESTRY WALK MUST NOT GO DOWNWARD.

        A vendor with a portal login on one of its contacts is ordinary. If
        `_ai_partner_ancestry` ever walked children, this test fails and
        real vendors would start being refused.
        """
        vendor = self._genuine_vendor()
        contact = self.Partner.create(
            {"name": "ZZ Portal Contact", "parent_id": vendor.id}
        )
        self.env["res.users"].create(
            {"name": contact.name, "login": "zz-ownco-portal-contact",
             "partner_id": contact.id}
        )

        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(vendor, company=self.company),
            "a login on a vendor's contact says nothing about the vendor",
        )

    # -- rules 3 and 4: ownership, and the intercompany case -----------

    def test_a_contact_owned_by_another_company_is_refused(self):
        """🔴 RED. *"and than contact that doesn't belong solely to another
        company"* — so one that DOES belong solely to another company is out.

        On a US reference database this is not hypothetical: a share of the partners with
        `supplier_rank > 0` carry `company_id = 2`, and on an Alpha LLC bill
        every one of them is the wrong answer.
        """
        other = self._other_company()
        theirs = self._genuine_vendor(
            name="ZZ Theirs Only Oy", company_id=other.id, vat=None, phone=None
        )

        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(theirs, company=self.company),
            "other_company_contact",
        )

    def test_control_the_same_contact_shared_is_accepted(self):
        """The only thing that proves the test above is about OWNERSHIP.

        Same record, same name, same everything — `company_id` cleared.
        """
        other = self._other_company()
        shared = self._genuine_vendor(
            name="ZZ Theirs Only Oy", company_id=other.id, vat=None, phone=None
        )
        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(shared, company=self.company),
            "other_company_contact",
            "fixture check: it is refused while it is owned",
        )

        shared.company_id = False

        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(shared, company=self.company),
            "a shared contact is reachable from every company",
        )

    def test_control_another_companys_own_partner_is_accepted(self):
        """🔴 THE INTERCOMPANY CASE, AND THE REASON THE RULE WAS NARROWED.

        `Beta Inc.` billing `Alpha LLC` is a legitimate document. Under the
        first reading of the brief — "any company-related contact" — this
        assertion fails and the intercompany flow breaks.
        """
        other = self._other_company()

        self.assertTrue(other.partner_id, "fixture check: the company has a partner")
        self.assertFalse(
            other.partner_id.company_id,
            "fixture check: a company's own partner is a SHARED contact, which "
            "is what makes it reachable under the ownership rule",
        )
        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(
                other.partner_id, company=self.company
            ),
            "another company's partner is a legitimate vendor",
        )

    def test_the_guard_follows_the_BILLS_company_not_the_environments(self):
        """🔴 `self.env.company` IS NOT THE ANSWER, AND PASSING IT WOULD BE A
        SILENT DEFECT.

        The import runs in a queue job. If the guard read the environment, it
        would protect whichever company the job happened to be in — refusing a
        legitimate intercompany vendor and letting the real defect through. Both
        directions are asserted, so neither can pass by accident.
        """
        other = self._other_company()

        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(
                self.company_partner, company=self.company
            ),
            "own_company",
        )
        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(
                self.company_partner, company=other
            ),
            "from the OTHER company's point of view, our partner is just a party",
        )

    # -- the ordering -------------------------------------------------

    def test_the_current_companys_contact_wins_over_an_equally_good_shared_one(self):
        """🔴 *"it would first check the current company for a contact, and than
        contact that doesn't belong solely to another company."*

        Two partners, same exact name, one owned by this company and one shared.
        Core's legacy chain already orders its own steps this way; this asserts
        that OUR two direct `res.partner` steps do too.
        """
        shared = self._genuine_vendor(
            name="ZZ Tie Break Oy", vat=None, phone=None, company_id=False
        )
        ours = self._genuine_vendor(
            name="ZZ Tie Break Oy", vat=None, phone=None, company_id=self.company.id
        )
        self.assertNotEqual(shared, ours, "fixture check: two distinct rows")

        match = self.Partner._ai_find_vendor(
            self._seller(name="ZZ Tie Break Oy", vat=None, phone=None),
            company=self.company,
        )

        self.assertEqual(
            match, ours, "the current company's contact is checked first"
        )

    def test_control_with_no_current_company_match_the_shared_one_is_still_found(self):
        """The control that stops the ordering passing by refusing everything.

        Delete the current-company row and the shared one must be the answer —
        otherwise "ours wins" would be indistinguishable from "nothing matches".
        """
        shared = self._genuine_vendor(
            name="ZZ Tie Break Oy", vat=None, phone=None, company_id=False
        )

        match = self.Partner._ai_find_vendor(
            self._seller(name="ZZ Tie Break Oy", vat=None, phone=None),
            company=self.company,
        )

        self.assertEqual(match, shared, "a shared contact is still a match")

    # -- PR #336 review finding 1: every step, not five of six -------------

    def test_the_business_id_step_refuses_the_company_partner(self):
        """🔴 RED. The ONE step in the matcher that applied no refusal at all.

        Steps 1 and 6 go through `_ai_vendor_search`; the phone and domain steps
        go through `goodahead.partner.identifier`, which has dropped our own
        company partners since 19.0.3.0.0. The business-id step did a bare
        `search(..., limit=1)` and handed the result straight back — to
        `_create_partner_from_extraction`, to the create-contact button whose
        docstring promises this cannot happen, and to core through the legacy
        name-slot adapter. Five of six covered is exactly why nothing noticed.
        """
        self.company_partner.company_registry = "ZZ-OWNCO-REG-1"
        self.company_partner.country_id = self.env.ref("base.fi").id
        seller = self._seller(
            name="ZZ Nothing Like Us", vat=None, phone=None
        )
        seller["business_id"] = "ZZ-OWNCO-REG-1"
        seller["country_code"] = "FI"
        self.assertEqual(
            self.Partner.search(
                self.Partner._ai_vendor_registry_domain("ZZ-OWNCO-REG-1", "FI")
            ),
            self.company_partner,
            "fixture check: that registration number reaches us and nobody else",
        )

        match = self.Partner._ai_find_vendor(seller, company=self.company)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_the_import_path_caller_is_given_the_bills_company(self):
        """🔴 RED. `_create_partner_from_extraction` passed no company, so the
        guard inside the matcher protected `self.env.company` — which in a queue
        job is not reliably the bill's.

        Asserted by watching what the matcher is ASKED, because the answer is
        the same on a one-company bench and the defect is in the question.
        """
        seen = []
        original = type(self.Partner)._ai_find_vendor

        def recording(this, seller, company=None):
            seen.append(company)
            return original(this, seller, company=company)

        bill = self._draft_bill(None)
        with patch.object(type(self.Partner), "_ai_find_vendor", recording):
            self.AM._create_partner_from_extraction(
                bill, {"seller": self._seller(vat=None, phone=None)}
            )

        self.assertTrue(seen, "the matcher was reached at all")
        self.assertEqual(
            seen[0],
            bill.company_id,
            "the matcher is asked about the BILL's company, not the env's",
        )

    # -- PR #336 review finding 2: the fetch window -------------------------

    def test_a_refused_row_does_not_consume_the_fetch_window(self):
        """🔴 RED, AND THE FAILURE IS A MISS RATHER THAN A WRONG MATCH.

        Twenty refused rows came back, all were dropped, and a legitimate vendor
        at row twenty-one was never seen — so a bill that used to match
        correctly matched NOTHING. That is the opposite failure from the one
        this branch exists to fix, and it is silent.

        🔴 THE FIRST VERSION OF THIS TEST PASSED ON THE BROKEN CODE, which
        means it was testing nothing. It filled the window with contacts UNDER
        the company partner and searched through `_ai_seller_name_domain` —
        whose own leaf is ``('parent_id', '=', False)``. Every filler row was
        excluded by the caller's domain before the guard was reached, so the
        window was never full. A fixture that cannot reproduce the defect is
        indistinguishable from a fix.

        It uses the BUSINESS-ID step instead, which carries no `parent_id` leaf.
        `company_registry` is a COMMERCIAL field
        (`base/models/res_partner.py:688` at `ca1f0aea`), so children of the
        company partner inherit it — 25 rows that satisfy the caller's domain
        and are all refused by rule 1. ⚠️ `company_registry` rather than `vat`
        for one measured reason: `vat` carries a checksum, an invented German
        number raised `ValidationError` in the fixture, and a registration
        number is validated by nobody. Both the fillers and the eligible vendor
        are ``company_id = False``, so they compete in the SAME ownership pass,
        and the eligible one is created last so it sorts behind them on the
        `create_date asc, id asc` tiebreak.
        """
        registry = "ZZ-WINDOW-REG-1"
        country = self.env.ref("base.de")
        self.company_partner.write(
            {"company_registry": registry, "country_id": country.id}
        )
        for index in range(25):
            self.Partner.create(
                {
                    "name": "ZZ Filler %s" % index,
                    "parent_id": self.company_partner.id,
                }
            )
        domain = self.Partner._ai_vendor_registry_domain(registry, "DE")
        reachable = self.Partner.search(domain)
        self.assertGreater(
            len(reachable),
            20,
            "fixture check: more rows reach the key than the fetch window holds",
        )
        self.assertEqual(
            reachable.commercial_partner_id,
            self.company_partner,
            "fixture check: every one of them is our own party, so rule 1 "
            "refuses all of them",
        )
        eligible = self.Partner.create(
            {
                "name": "ZZ Eligible Beyond The Window",
                "is_company": True,
                "company_registry": registry,
                "country_id": country.id,
            }
        )

        match = self.Partner._ai_vendor_search(
            domain,
            company=self.company,
            order="supplier_rank desc, create_date asc, id asc",
        )

        self.assertEqual(
            match,
            eligible,
            "the eligible vendor behind the refused rows is still found",
        )

    def test_control_an_eligible_vendor_inside_the_window_costs_the_same_searches(self):
        """🔴 THE CONTROL THAT STOPS "RAISE THE LIMIT TO 200" PASSING.

        Two things at once, and both matter. The ANSWER must be unchanged when
        the eligible vendor is inside the window; and the number of searches
        must be unchanged too — one per ownership pass — because the fix had to
        be a domain term and not a paging loop, on a path that runs for every
        imported bill.
        """
        registry = "ZZ-NARROW-REG-1"
        country = self.env.ref("base.de")
        eligible = self.Partner.create(
            {
                "name": "ZZ Narrow Window Oy",
                "is_company": True,
                "company_registry": registry,
                "country_id": country.id,
            }
        )
        domain = self.Partner._ai_vendor_registry_domain(registry, "DE")

        # 🔴 ONLY THE CALLER'S OWN FETCHES ARE COUNTED, AND TWO EARLIER
        # VERSIONS OF THIS TEST COUNTED THE WRONG THING.
        #
        #   * "at most 2 partner searches" measured **5** — because the total
        #     includes the guard's own bookkeeping: `_ai_refused_vendor_domain`
        #     runs a `child_of` search for the company's contact tree and a
        #     login lookup, on every call.
        #   * "the same total in both cases" measured **6 against 5** — because
        #     filling the tree with 25 contacts makes the `child_of` resolution
        #     itself cost one more query. That is recursion depth, not paging,
        #     and a paging loop would have looked identical.
        #
        # The sentinel REGISTRATION NUMBER appears only in the caller's own
        # domain, so counting the searches that mention it isolates exactly the
        # fetches a paging loop would multiply: one per ownership pass, twice,
        # whatever the data.
        counted = []
        original = type(self.Partner)._search

        def counting(this, dom, *args, **kwargs):
            if this._name == "res.partner" and registry in str(dom):
                counted.append(dom)
            return original(this, dom, *args, **kwargs)

        with patch.object(type(self.Partner), "_search", counting):
            match = self.Partner._ai_vendor_search(domain, company=self.company)
        inside = len(counted)

        self.assertEqual(match, eligible, "the plain case still answers")
        self.assertEqual(
            inside, 2, "one fetch per ownership pass, and there are two passes"
        )

        # And with the window full it is still two: the refused rows are gone
        # from the SEARCH, so there is nothing to page through.
        self.company_partner.write(
            {"company_registry": registry, "country_id": country.id}
        )
        for index in range(25):
            self.Partner.create(
                {
                    "name": "ZZ Narrow Filler %s" % index,
                    "parent_id": self.company_partner.id,
                }
            )
        self.assertGreater(
            self.Partner.search_count(domain),
            20,
            "fixture check: the window really is full this time",
        )
        counted.clear()
        with patch.object(type(self.Partner), "_search", counting):
            match = self.Partner._ai_vendor_search(domain, company=self.company)

        self.assertEqual(match, eligible, "still the eligible one")
        self.assertEqual(
            len(counted),
            inside,
            "and it cost the same number of fetches as the empty case — a "
            "paging loop would have had to go round again",
        )

    def test_control_the_post_filter_is_still_the_authority(self):
        """The domain is a NARROWING, not the rule.

        It cannot express the ancestry walk, so a refused party the domain
        misses must still be dropped. Proved by stubbing the narrowing away and
        asserting the answer is unchanged — if the post-filter had been removed
        in favour of the domain, this fails.
        """
        with patch.object(
            type(self.Partner), "_ai_refused_vendor_domain", return_value=[]
        ):
            match = self.Partner._ai_vendor_search(
                [("id", "=", self.company_partner.id)], company=self.company
            )

        self.assertFalse(
            match, "with the domain disarmed, the filter still refuses us"
        )

    def test_a_contact_owned_by_another_company_never_wins_the_name_step(self):
        """Rule 3 inside the matcher, not just inside the predicate."""
        other = self._other_company()
        self._genuine_vendor(
            name="ZZ Only Theirs Oy", vat=None, phone=None, company_id=other.id
        )

        match = self.Partner._ai_find_vendor(
            self._seller(name="ZZ Only Theirs Oy", vat=None, phone=None),
            company=self.company,
        )

        self.assertFalse(
            match, "a contact owned solely by another company is never the vendor"
        )


@tagged("post_install", "-at_install")
class TestTheBankHookIsTheSecondLineOfDefence(_OwnCompanyTrapHarness):
    """`account.edi.common._import_partner_bank` — the older decoder's half.

    🔴 WHAT THE ``in_refund``-ONLY CONDITION WAS PROTECTING, BEFORE IT WAS
    WIDENED. Core picks the side from ``invoice.is_inbound()``, and
    ``get_inbound_types`` is ``['out_invoice', 'in_refund', 'out_receipt']``
    (`account/models/account_move.py:6303` at `ca1f0aea`). So:

      * on `in_refund` core takes ``self.env.company.partner_id`` — the
        credit-note bank bug, and the reason the divert exists;
      * on `in_invoice` core already takes ``invoice.partner_id``, which is the
        right side, and it also sets ``invoice.partner_bank_id``;
      * on `out_invoice` / `out_receipt` core takes the company partner and is
        CORRECT — that is our account, for the customer to pay into.

    ⚠️ So "widen it to every inbound move type" literally would be a
    REGRESSION: it would divert the two sale documents core gets right. What is
    widened here is the two GUARDS — no vendor, and a vendor that is one of our
    own companies — to every purchase document, while a correctly-resolved
    vendor on an `in_invoice` keeps core's behaviour unchanged, including the
    ``partner_bank_id`` core sets.
    """

    def test_no_bank_row_when_the_vendor_is_our_own_company(self):
        bill = self._draft_bill(self.company_partner)

        self.EdiCommon._import_partner_bank(bill, [TRAP_IBAN])

        self.assertFalse(
            self._bank_rows_for(self.company_partner),
            "a vendor's IBAN is never recorded against our own company",
        )
        # ⚠️ NOT ``assertFalse(bill.partner_bank_id)``, and the first draft of
        # this test said exactly that and failed on `res.partner.bank(2,)`.
        # This fixture's `partner_id` IS the company partner, so core's
        # `_compute_partner_bank_id` legitimately offers that partner's own
        # existing account — which is the compute doing its job, not this hook
        # writing anything. What the hook must not do is put the DOCUMENT's
        # number there.
        self.assertNotEqual(
            bill.partner_bank_id.sanitized_acc_number,
            sanitize_account_number(TRAP_IBAN),
            "the number printed on the document never reaches partner_bank_id",
        )

    def test_no_bank_row_and_no_crash_when_the_bill_has_no_vendor(self):
        """🔴 A LOCK, NOT A REPAIR — AND IT WAS WRITTEN AS A REPAIR.

        The claim this test was written to prove: once the vendor is refused,
        ``invoice.partner_id`` is empty, core does
        ``create({'acc_number': …, 'partner_id': partner.id})`` with
        ``partner.id`` False, ``res.partner.bank.partner_id`` is ``required``,
        so the decode fails and the root fix alone breaks every bill printing
        an IBAN.

        **It PASSED against 19.0.3.22.0.** Core never reaches the create:
        ``partner_bank.partner_id == partner`` compares two EMPTY recordsets,
        which is True, so core assigns the empty recordset and returns
        (`account_edi_common.py:581-586` at `ca1f0aea`). The claim was read off
        the source and the source was not run — this project's signature
        mistake, so it is written down rather than quietly deleted.

        The test stays because the OUTCOME is the one we want and nothing else
        asserted it, and because the same situation on the search-plan core
        reads as if it would raise (`_find_or_create_bank_account` has no
        empty-partner branch) — which is a reading, not a measurement, and this
        bench cannot settle it.
        """
        bill = self._draft_bill(None)

        self.EdiCommon._import_partner_bank(bill, [TRAP_IBAN])

        self.assertFalse(
            self.Bank.with_context(active_test=False).search(
                [("sanitized_acc_number", "=", sanitize_account_number(TRAP_IBAN))]
            ),
            "no vendor means no account holder, so nothing is created",
        )
        self.assertFalse(bill.partner_bank_id)

    # -- controls ------------------------------------------------------

    def test_control_a_genuine_vendors_iban_is_still_recorded(self):
        vendor = self._genuine_vendor()
        bill = self._draft_bill(vendor)

        self.EdiCommon._import_partner_bank(bill, [OTHER_IBAN])

        rows = self._bank_rows_for(vendor, OTHER_IBAN)
        self.assertEqual(len(rows), 1, "the vendor's own IBAN still lands on the vendor")
        self.assertEqual(
            bill.partner_bank_id,
            rows,
            "and core still fills partner_bank_id on a bill we pay",
        )

    def test_control_a_received_credit_note_still_goes_to_the_vendor(self):
        """The original credit-note bank case, unchanged by the widening."""
        vendor = self._genuine_vendor()
        note = self._draft_bill(vendor, move_type="in_refund")

        self.EdiCommon._import_partner_bank(note, [OTHER_IBAN])

        self.assertEqual(
            len(self._bank_rows_for(vendor, OTHER_IBAN)),
            1,
            "a received credit note's account is the vendor's",
        )
        self.assertFalse(
            self._bank_rows_for(self.company_partner, OTHER_IBAN),
            "and never ours",
        )
