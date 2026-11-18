# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The legacy adapter: `res.partner._retrieve_partner_with_name`.

On the legacy pipeline (Odoo 18.0 and early 19.0 builds)
`res.partner._retrieve_partner` runs VAT, domain, phone/mail and finally
`_retrieve_partner_with_name`, each company-scoped and then with
``company_id = False``. On the search-plan pipeline (newer 19.0 builds) the same
method is a deprecated stub with no core callers, so the override is live on 18.0
and inert there.

Only the business id, phone and domain rules are composed at this slot, the
same three the search-plan adapter inserts. Core has already run its own VAT
step, and the name match at this slot is core's.

Fake ``ZZ…`` names, numbers and domains throughout.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

# Core's class, imported directly so a control can call core's implementation
# while this module is installed.
from odoo.addons.account.models.partner import ResPartner as CoreResPartner

from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_ai_import.models.core_profile import (
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
)

from .profiles import (
    EXPECTED_GATED,
    PROFILE_GATED,
    PROFILE_RAN,
    PROFILE_SKIPPED,
    skip_method_unless_profile,
    skip_unless_profile,
)

#: Core's second pass, where most vendors (no `company_id`) are found.
NO_COMPANY = [("company_id", "=", False)]


class _Boom(Exception):
    """A distinct class, so a guard cannot pass by catching something the ORM
    raises for an unrelated reason."""


def _explode(*args, **kwargs):
    raise _Boom("the whole matcher was consulted at core's name slot")


@tagged("post_install", "-at_install")
class TestTheNameSlotAdapter(TransactionCase):
    """Direct calls; meaningful on both builds since the method exists on both
    with the same signature.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.us = cls.env.ref("base.us")

        # Reachable by phone and web domain, but not by core's name step.
        cls.vendor = cls.Partner.create(
            {
                "name": "ZZ Slot Vendor Holdings",
                "is_company": True,
                "supplier_rank": 7,
                "phone": "+1 718-555-0177",
                "email": "ap@zzslotvendor.example",
                "country_id": cls.us.id,
            }
        )

    def _seller(self, **over):
        seller = {
            "name": "ZZ PRINTED NAME NOBODY HAS",
            "country_code": "US",
        }
        seller.update(over)
        return seller

    def _ai(self, seller):
        return self.Partner.with_context(ai_invoice_import=True, ai_seller=seller)

    # ------------------------------------------------------------------
    # It fires, and it finds what core's name step cannot
    # ------------------------------------------------------------------

    def test_a_vendor_reachable_only_by_phone_is_returned_at_the_name_slot(self):
        """The same phone in another format finds the vendor even though the
        printed name differs from the ledger name."""
        found = self._ai(self._seller(phone="718.555.0177"))._retrieve_partner_with_name(
            "ZZ PRINTED NAME NOBODY HAS", NO_COMPANY
        )
        self.assertEqual(found, self.vendor)

    def test_a_vendor_reachable_only_by_email_domain_is_returned(self):
        found = self._ai(
            self._seller(email="billing@zzslotvendor.example")
        )._retrieve_partner_with_name("ZZ PRINTED NAME NOBODY HAS", NO_COMPANY)
        self.assertEqual(found, self.vendor)

    def test_core_alone_would_not_have_found_it(self):
        """Without our context, core's own name step finds nothing."""
        self.assertFalse(
            self.Partner._retrieve_partner_with_name(
                "ZZ PRINTED NAME NOBODY HAS", NO_COMPANY
            )
        )

    def test_someone_else_s_document_is_left_to_core(self):
        """Without the `ai_invoice_import` context key, other documents (Peppol,
        Factur-X, other modules) get core's behaviour."""
        found = self.Partner.with_context(
            ai_seller=self._seller(phone="718.555.0177")
        )._retrieve_partner_with_name("ZZ PRINTED NAME NOBODY HAS", NO_COMPANY)
        self.assertFalse(found, "no context key, no adapter")

    # ------------------------------------------------------------------
    # Never worse than core
    # ------------------------------------------------------------------

    def test_an_extra_domain_that_excludes_our_match_falls_through_to_core(self):
        """A match that fails the caller's `extra_domain` is not returned."""
        found = self._ai(self._seller(phone="718.555.0177"))._retrieve_partner_with_name(
            "ZZ PRINTED NAME NOBODY HAS", [("company_id", "!=", False)]
        )
        self.assertFalse(found)

    def test_a_seller_that_identifies_nobody_returns_exactly_what_core_returns(self):
        """When our rules decline, core's answer is returned unchanged."""
        core = self.Partner._retrieve_partner_with_name(
            "ZZ Slot Vendor Holdings", NO_COMPANY
        )
        ours = self._ai(self._seller(phone="+1 309-555-0199"))._retrieve_partner_with_name(
            "ZZ Slot Vendor Holdings", NO_COMPANY
        )
        self.assertEqual(ours, core)
        self.assertEqual(ours, self.vendor, "control: core's own name step does match here")

    def test_the_name_step_itself_is_narrowed_to_the_other_build_s_exact_match(self):
        """The adapter's name step uses ``=ilike`` (exact, case-insensitive),
        like the search-plan pipeline; the legacy core's own step uses a
        substring ``ilike``.

        See `res.partner._ai_retrieve_partner_with_exact_name`.
        """
        containing = self.Partner.create(
            {"name": "ZZ Narrowed Supplies International", "is_company": True}
        )

        found = self._ai(
            self._seller(name="ZZ Narrowed Supplies")
        )._retrieve_partner_with_name("ZZ Narrowed Supplies", NO_COMPANY)

        self.assertFalse(
            found, "a name that is only a substring must not decide the bill"
        )
        # Control on the fixture: a substring rule would find exactly one row.
        self.assertEqual(
            self.Partner.search(
                [("name", "ilike", "ZZ Narrowed Supplies")] + NO_COMPANY
            ),
            containing,
            "control: a substring rule here resolves to exactly one partner",
        )

    # ------------------------------------------------------------------
    # The other direction of the narrowing
    # ------------------------------------------------------------------

    def _near_name_pair(self):
        """One partner matching the printed name exactly, one it is a prefix
        of."""
        exact = self.Partner.create(
            {"name": "ZZ Zeta Test Oy", "is_company": True, "supplier_rank": 3}
        )
        longer = self.Partner.create(
            {
                "name": "ZZ Zeta Test Oy Branch",
                "is_company": True,
                "supplier_rank": 9,
            }
        )
        # Control on the fixture: a substring search returns both.
        self.assertEqual(
            self.Partner.search(
                [("name", "ilike", "ZZ Zeta Test Oy")] + NO_COMPANY
            ),
            exact + longer,
        )
        return exact, longer

    def test_the_narrowed_step_decides_a_near_name_pair(self):
        """An exact name beside a longer one is decided.

        ``=ilike`` matches a subset of ``ilike``, and the caller accepts only
        ``len(partner) == 1``, so a pair the substring step refused as
        ambiguous is decided, as it already is on the search-plan build.
        """
        exact, _longer = self._near_name_pair()

        ours = self._ai(
            self._seller(name="ZZ Zeta Test Oy")
        )._retrieve_partner_with_name("ZZ Zeta Test Oy", NO_COMPANY)

        self.assertEqual(
            ours,
            exact,
            "an exact name beside a longer one must decide, as it already does "
            "on the search-plan build",
        )

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_this_core_refuses_the_near_name_pair_that_ours_decides(self):
        """Core alone on the legacy build refuses the pair.

        If a future core decides it by itself, this fails and flags the
        override as no longer needed.
        """
        self._near_name_pair()

        self.assertFalse(
            self.Partner._retrieve_partner(name="ZZ Zeta Test Oy"),
            "control: this core refuses the pair, which is why the slot is "
            "narrowed at all",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_this_core_already_decides_the_near_name_pair(self):
        """The search-plan core already picks the exact-name partner (``=ilike``
        with ``limit=1``), so the legacy slot now gives the same answer.
        """
        exact, _longer = self._near_name_pair()

        self.assertEqual(
            self.Partner._retrieve_partner(name="ZZ Zeta Test Oy"),
            exact,
            "control: this core decides the pair by itself",
        )

    def test_two_partners_with_the_SAME_exact_name_are_still_refused(self):
        """Two exact-name matches are still returned unresolved.

        The search-plan core would pick the higher `supplier_rank`; declining
        is the safer direction and sends the bill to the candidate cards.
        """
        low = self.Partner.create(
            {"name": "ZZ Twinned Exactly", "is_company": True, "supplier_rank": 1}
        )
        high = self.Partner.create(
            {"name": "ZZ Twinned Exactly", "is_company": True, "supplier_rank": 9}
        )

        ours = self._ai(
            self._seller(name="ZZ Twinned Exactly")
        )._retrieve_partner_with_name("ZZ Twinned Exactly", NO_COMPANY)

        self.assertNotEqual(len(ours), 1)
        self.assertIn(low, ours)
        self.assertIn(high, ours)

    def test_an_empty_name_still_returns_core_s_answer(self):
        """An empty name returns core's answer (None), not a recordset."""
        self.assertEqual(
            self._ai(self._seller())._retrieve_partner_with_name("", NO_COMPANY),
            self.Partner._retrieve_partner_with_name("", NO_COMPANY),
        )

    # ------------------------------------------------------------------
    # What is not composed at this slot: both tests fail if the adapter
    # calls `_ai_find_vendor`.
    # ------------------------------------------------------------------

    def test_our_name_rule_does_not_run_at_core_s_name_slot(self):
        """Two top-level partners sharing a name are returned unresolved.

        `_ai_find_vendor`'s own name rule would pick by `supplier_rank`; that
        rule must not run here, because the caller treats a single record as
        the decision."""
        # Low rank created first, so "first created" and "highest rank" differ.
        low = self.Partner.create(
            {"name": "ZZ Twinned Trading", "is_company": True, "supplier_rank": 1}
        )
        high = self.Partner.create(
            {"name": "ZZ Twinned Trading", "is_company": True, "supplier_rank": 9}
        )
        # `_ai_find_vendor` raises for the duration.
        with patch.object(type(self.Partner), "_ai_find_vendor", _explode):
            ours = self._ai(
                self._seller(name="ZZ Twinned Trading")
            )._retrieve_partner_with_name("ZZ Twinned Trading", NO_COMPANY)

        self.assertNotEqual(
            len(ours),
            1,
            "an ambiguous name must reach core's caller unresolved, because "
            "`len(partner) == 1` is how that caller decides",
        )
        self.assertIn(low, ours)
        self.assertIn(high, ours)
        # Control: the whole matcher does resolve it, by supplier_rank.
        self.assertEqual(
            self.Partner._ai_find_vendor({"name": "ZZ Twinned Trading"}),
            high,
            "control: `_ai_find_vendor` resolves by supplier_rank — which is "
            "exactly the rule that must not run at core's name slot",
        )

    def test_our_vat_rule_does_not_run_at_core_s_name_slot(self):
        """VAT is not retried here; core already ran its own VAT step."""
        self.Partner.create(
            {
                "name": "ZZ Vatted Slot Co",
                "is_company": True,
                "supplier_rank": 4,
                "vat": "ZZ4455667788",
                "country_id": self.us.id,
            }
        )
        found = self._ai(
            self._seller(vat="zz4455667788")
        )._retrieve_partner_with_name("ZZ PRINTED NAME NOBODY HAS", NO_COMPANY)
        self.assertFalse(found, "VAT is core's step, at core's position")

    # ------------------------------------------------------------------
    # The shared composition — one body, two adapters
    # ------------------------------------------------------------------

    def test_both_adapters_compose_the_same_three_rules(self):
        """The legacy adapter goes through the shared
        `_ai_vendor_from_seller_keys` composition."""
        with patch.object(
            type(self.Partner),
            "_ai_vendor_from_seller_keys",
            lambda self, seller, company=None: self.env["res.partner"],
        ):
            found = self._ai(
                self._seller(phone="718.555.0177")
            )._retrieve_partner_with_name("ZZ PRINTED NAME NOBODY HAS", NO_COMPANY)
        self.assertFalse(found, "the adapter must go through the shared composition")

    def test_the_shared_composition_leaves_vat_and_name_to_core(self):
        """The shared composition matches by phone but not by name."""
        self.assertEqual(
            # A national-form phone is normalised under the seller's country.
            self.Partner._ai_vendor_from_seller_keys(
                {"phone": "718.555.0177", "country_code": "US"}
            ),
            self.vendor,
        )
        self.assertFalse(
            self.Partner._ai_vendor_from_seller_keys(
                {"name": "ZZ Slot Vendor Holdings"}
            ),
            "the name rule belongs to core at this position",
        )

    def test_the_whole_matcher_still_carries_both(self):
        """`_ai_find_vendor`, used post-decode, keeps its name and VAT
        rules."""
        self.assertEqual(
            self.Partner._ai_find_vendor({"name": "ZZ Slot Vendor Holdings"}),
            self.vendor,
        )


@tagged("post_install", "-at_install")
class TestBothProfilesAreCovered(TransactionCase):
    """Checks that profile-gated tests really ran or skipped on this build.

    ``test_sequence`` puts this class last, since Odoo's loader sorts on it and
    the run-time registries are only complete at the end of the run.
    """

    def test_the_gates_actually_fired_on_this_bench(self):
        live = self.env["account.move"]._ai_core_profile()
        self.assertIn(live, (PROFILE_SEARCH_PLAN, PROFILE_LEGACY))
        other = PROFILE_LEGACY if live == PROFILE_SEARCH_PLAN else PROFILE_SEARCH_PLAN

        self.assertTrue(
            PROFILE_RAN[live],
            "no test gated on %r EXECUTED, so this run proves nothing about "
            "the build it was run on" % live,
        )
        self.assertFalse(
            PROFILE_RAN[other],
            "a test gated on %r executed on a %r build: %s"
            % (other, live, sorted(PROFILE_RAN[other])),
        )
        self.assertTrue(
            PROFILE_SKIPPED[other],
            "nothing gated on %r was skipped, so the two benches did not "
            "disagree about anything" % other,
        )

    def test_every_gate_was_reached_and_decided(self):
        """Each gated test either ran or skipped — none was quietly dropped
        from the suite by a tag, a `--test-tags` filter or a missing import."""
        for profile in (PROFILE_SEARCH_PLAN, PROFILE_LEGACY):
            self.assertEqual(
                PROFILE_RAN[profile] | PROFILE_SKIPPED[profile],
                PROFILE_GATED[profile],
                "these %r-gated tests were never reached: %s"
                % (
                    profile,
                    sorted(
                        PROFILE_GATED[profile]
                        - PROFILE_RAN[profile]
                        - PROFILE_SKIPPED[profile]
                    ),
                ),
            )

    def test_the_number_of_gated_tests_is_the_number_recorded(self):
        """The gated-test count matches `EXPECTED_GATED` in `profiles.py`.

        A dropped test module or deleted decorator shrinks every run-time
        registry together, so only a written-down number notices.
        """
        for profile in (PROFILE_SEARCH_PLAN, PROFILE_LEGACY):
            self.assertEqual(
                len(PROFILE_GATED[profile]),
                EXPECTED_GATED[profile],
                "%r gates %d tests, EXPECTED_GATED says %d: %s"
                % (
                    profile,
                    len(PROFILE_GATED[profile]),
                    EXPECTED_GATED[profile],
                    sorted(PROFILE_GATED[profile]),
                ),
            )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_a_search_plan_gate_in_this_very_class_fired(self):
        """A gate inside this class proves the registries were written during
        this run."""
        self.assertIn(
            "TestBothProfilesAreCovered."
            "test_a_search_plan_gate_in_this_very_class_fired",
            PROFILE_RAN[PROFILE_SEARCH_PLAN],
        )

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_a_legacy_gate_in_this_very_class_fired(self):
        self.assertIn(
            "TestBothProfilesAreCovered.test_a_legacy_gate_in_this_very_class_fired",
            PROFILE_RAN[PROFILE_LEGACY],
        )

    def test_this_build_records_a_profile_the_gates_know(self):
        self.assertIn(
            self.env["account.move"]._ai_core_profile(),
            (PROFILE_SEARCH_PLAN, PROFILE_LEGACY),
            "the bench must know which bench it is before anything gated on it "
            "is believed",
        )


# Odoo 18.0's test metaclass resets `test_sequence` to 0 when the class is
# created, so a class attribute is ignored; set it afterwards instead.
TestBothProfilesAreCovered.test_sequence = 1000


class _ImportHarness(TransactionCase):
    """One real import with only the Claude call mocked.

    The right vendor is reachable only by the printed phone (in another format).
    The decoy is reachable by the legacy core's substring name step: "ZZ Hook
    Supplies" finds "ZZ Hook Supplies International". The adapter has to win at
    core's name slot, because a wrong match leaves `partner_id` set and skips
    the module's post-decode recovery.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.Partner = cls.env["res.partner"]
        cls.us = cls.env.ref("base.us")
        cls.vendor = cls.Partner.create(
            {
                "name": "ZZ Hook Vendor Holdings",
                "is_company": True,
                "supplier_rank": 6,
                "phone": "+1 718-555-0166",
                "country_id": cls.us.id,
            }
        )
        cls.decoy = cls.Partner.create(
            {
                "name": "ZZ Hook Supplies International",
                "is_company": True,
                "supplier_rank": 1,
                "country_id": cls.us.id,
            }
        )

    def _extracted(self):
        """No VAT on the document: the legacy core creates a vendor from
        `name and vat`, and this test is about matching only."""
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-HOOK-1",
            "issue_date": "2026-08-06",
            "currency": "USD",
            "seller": {
                "name": "ZZ Hook Supplies",
                "country_code": "US",
                "phone": "718.555.0166",
            },
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "US",
            },
            "lines": [
                {
                    "product_code": "ZZ-1",
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

    def _run_import(self):
        extracted = self._extracted()
        attachment = self.env["ir.attachment"].create(
            {
                "name": "zz-hook.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        record = self.env["ai.invoice.import"].create(
            {
                "name": "zz-hook.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": attachment.id,
            }
        )
        result = {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            "xml_bytes": generate_peppol_invoice_xml(extracted),
        }
        with patch.object(
            type(self.env["account.move"]),
            "_ai_extract_and_generate_ubl",
            return_value=result,
        ):
            outcome = record._process_import()
        return record, outcome

    def _counting_name_slot(self):
        """Wrap `_retrieve_partner_with_name` and record every entry."""
        partner_cls = type(self.env["res.partner"])
        original = partner_cls._retrieve_partner_with_name
        calls = []

        def counting(self, name, extra_domain, *args, **kwargs):
            calls.append(name)
            return original(self, name, extra_domain, *args, **kwargs)

        return calls, patch.object(partner_cls, "_retrieve_partner_with_name", counting)


@skip_unless_profile(PROFILE_SEARCH_PLAN)
@tagged("post_install", "-at_install")
class TestTheNameSlotIsInertOnTheSearchPlanBuild(_ImportHarness):
    """On the search-plan build a full import never enters the name slot."""

    def test_a_full_import_never_enters_the_name_slot(self):
        calls, counting = self._counting_name_slot()
        with counting:
            record, outcome = self._run_import()
        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertEqual(
            calls, [], "the legacy adapter must never be entered on this build"
        )
        # Control: the import ran and matched through the search plan.
        self.assertEqual(record.move_id.partner_id, self.vendor)


@skip_unless_profile(PROFILE_LEGACY)
@tagged("post_install", "-at_install")
class TestTheNameSlotFiresOnTheLegacyBuild(_ImportHarness):
    """On the legacy build the name slot is the module's live hook."""

    def test_a_full_import_enters_the_name_slot_and_beats_core_s_name_match(self):
        calls, counting = self._counting_name_slot()
        with counting:
            record, outcome = self._run_import()
        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertTrue(calls, "core must reach the name slot on this build")
        self.assertEqual(
            record.move_id.partner_id,
            self.vendor,
            "the bill must be filed against the vendor the phone number "
            "identifies, not the one the printed name resembles",
        )

    def test_core_s_own_name_step_files_the_bill_against_the_wrong_vendor(self):
        """With core's own name step patched back in, the substring match files
        the bill against the decoy.

        Patching in core's class function removes both of this module's
        additions at the slot (the three keys and the ``=ilike`` narrowing).
        """
        with patch.object(
            type(self.env["res.partner"]),
            "_retrieve_partner_with_name",
            lambda self, name, extra_domain: (
                CoreResPartner._retrieve_partner_with_name(self, name, extra_domain)
            ),
        ):
            record, outcome = self._run_import()
        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertEqual(
            record.move_id.partner_id,
            self.decoy,
            "control: with core's own name step restored, its substring match "
            "wins and files the bill against the wrong vendor",
        )


@tagged("post_install", "-at_install")
class TestASubstringNameIsNotAVendor(_ImportHarness):
    """A printed name that is only a substring of a partner's name does not
    decide the vendor, on either build.

    The legacy core's name step uses ``ilike``; a wrong match would leave
    `partner_id` set and suppress the banner, candidate cards and learning.
    The fixture prints no phone, so only core's name step can decide. This
    class is deliberately not profile-gated: the outcome must be the same on
    both builds.
    """

    def _extracted(self):
        extracted = super()._extracted()
        # Without the phone, nothing this module knows identifies the seller.
        extracted["seller"].pop("phone", None)
        return extracted

    def test_a_printed_name_that_only_resembles_an_existing_vendor_does_not_decide(
        self,
    ):
        record, outcome = self._run_import()

        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertFalse(
            record.move_id.partner_id,
            "'ZZ Hook Supplies' is only a SUBSTRING of "
            "'ZZ Hook Supplies International'. A resemblance is not an "
            "identity, and this module does not file a bill on one",
        )

    def test_the_partner_it_resembles_is_still_offered_as_a_candidate(self):
        """The resembling partner is offered as a candidate instead.

        `_ai_vendor_candidates` searches the same ``ilike``, so nothing is
        lost; this is also the control for the test above.
        """
        record, _outcome = self._run_import()

        self.assertIn(
            self.decoy,
            record.move_id.ai_vendor_candidate_ids.partner_id,
            "the partner the name resembles must be offered, not discarded",
        )
