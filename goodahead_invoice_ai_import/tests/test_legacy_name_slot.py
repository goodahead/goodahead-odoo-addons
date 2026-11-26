# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The legacy adapter: `res.partner._retrieve_partner_with_name` (the dual-core design).

🔴 WHAT THIS FILE IS FOR. Until now this module had NO live hook on the older
19.0 core. `_ai_find_vendor` is defined there and callable there, and nothing
called it: four of the five EDI overrides are `_import_ubl_*` steps that core
never runs, and `_retrieve_partner` was deliberately left alone. Installed,
green, never run — the silent no-op the dual-core design exists to prevent. This is the hook.

WHERE IT SITS, AND WHY THAT IS THE WHOLE DESIGN. On the legacy core
`res.partner._retrieve_partner` (`account/models/partner.py:958`) runs
``search_with_vat -> search_with_domain -> search_with_phone_mail ->
search_with_name``, each twice (company-scoped, then ``company_id = False``), and
`:976` calls `_retrieve_partner_with_name` as the LAST live step. On the
search-plan core the same method is a deprecated stub with zero core callers. So
one override is live on one build and inert on the other, by core's own doing
rather than by a flag of ours. Both halves are asserted here.

🔴 WHAT IS COMPOSED AT THAT SLOT IS NOT THE WHOLE MATCHER, AND THAT IS THE
POINT OF HALF THESE TESTS. `_ai_find_vendor` also carries a VAT rule and a NAME
rule. Core owns both at this position — it has already run its own VAT step
twice by the time the name slot is reached, and the name match at this slot IS
core's. Composing the whole matcher here would put a case-insensitive VAT retry
and our own (`parent_id = False`, `supplier_rank`-ordered) name rule ahead of
core's name step, which changes which vendor a bill is filed against. The
adapter therefore runs exactly the three rules the search-plan adapter inserts
at the same relative position: business id, phone, email/website domain.

Fake ``ZZ…`` names, numbers and domains throughout, so a DB-touching assertion
can never collide with a vendor already in the database.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

# Core's own class, imported so a control can call core's implementation
# directly. Importing the CLASS (not going through the registry) is what makes
# it possible to ask "what would core alone have done?" from inside a tree where
# this module is installed — the same trick `test_product_matching.py` uses.
# ⚠️ The class is named `ResPartner` in `account/models/partner.py` on BOTH
# measured cores, so this import resolves on both; only its body differs.
from odoo.addons.account.models.partner import ResPartner as CoreResPartner

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_import.models.core_profile import (
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

#: Core's second pass — the one almost every vendor is found on, since a vendor
#: normally carries no `company_id`.
NO_COMPANY = [("company_id", "=", False)]


class _Boom(Exception):
    """A distinct class, so a guard cannot pass by catching something the ORM
    raises for an unrelated reason."""


def _explode(*args, **kwargs):
    raise _Boom("the whole matcher was consulted at core's name slot")


@tagged("post_install", "-at_install")
class TestTheNameSlotAdapter(TransactionCase):
    """Direct calls, on BOTH builds.

    `_retrieve_partner_with_name` exists on both cores with the same signature,
    so every assertion here is meaningful on either bench — which is what makes
    the adapter's *rules* testable on the tree it is developed in, while only
    its *liveness* needs the legacy bench.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.us = cls.env.ref("base.us")

        # Reachable by phone and by web domain, and by NOTHING core's name step
        # could find: the printed name on its invoices is not its ledger name.
        cls.vendor = cls.Partner.create(
            {
                "name": "ZZ Slot Vendor Holdings",
                "is_company": True,
                "supplier_rank": 7,
                "phone": "+1 212-555-0177",
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
        """The common case: the printed phone is the same telephone in a different
        format, and the printed name is not the ledger name."""
        found = self._ai(self._seller(phone="212.555.0177"))._retrieve_partner_with_name(
            "ZZ PRINTED NAME NOBODY HAS", NO_COMPANY
        )
        self.assertEqual(found, self.vendor)

    def test_a_vendor_reachable_only_by_email_domain_is_returned(self):
        found = self._ai(
            self._seller(email="billing@zzslotvendor.example")
        )._retrieve_partner_with_name("ZZ PRINTED NAME NOBODY HAS", NO_COMPANY)
        self.assertEqual(found, self.vendor)

    def test_core_alone_would_not_have_found_it(self):
        """🔴 THE CONTROL that makes the two tests above mean something. Without
        our context the identical call is core's own name step, and it finds
        nothing — so the match above is ours, not core's."""
        self.assertFalse(
            self.Partner._retrieve_partner_with_name(
                "ZZ PRINTED NAME NOBODY HAS", NO_COMPANY
            )
        )

    def test_someone_else_s_document_is_left_to_core(self):
        """⚠️ This method is reachable outside our import — a genuine Peppol
        document, a Factur-X bill, another module. The `ai_invoice_import`
        context key is what keeps the adapter to our own documents."""
        found = self.Partner.with_context(
            ai_seller=self._seller(phone="212.555.0177")
        )._retrieve_partner_with_name("ZZ PRINTED NAME NOBODY HAS", NO_COMPANY)
        self.assertFalse(found, "no context key, no adapter")

    # ------------------------------------------------------------------
    # Never worse than core
    # ------------------------------------------------------------------

    def test_an_extra_domain_that_excludes_our_match_falls_through_to_core(self):
        """🔴 Core calls this twice per import, once company-scoped and once with
        ``company_id = False``. Returning a partner that fails the caller's own
        extra_domain would hand `_retrieve_partner` a vendor it had deliberately
        scoped out."""
        found = self._ai(self._seller(phone="212.555.0177"))._retrieve_partner_with_name(
            "ZZ PRINTED NAME NOBODY HAS", [("company_id", "!=", False)]
        )
        self.assertFalse(found)

    def test_a_seller_that_identifies_nobody_returns_exactly_what_core_returns(self):
        """When our rules decline, core's answer is what happens — the same
        object, not an approximation of it."""
        core = self.Partner._retrieve_partner_with_name(
            "ZZ Slot Vendor Holdings", NO_COMPANY
        )
        ours = self._ai(self._seller(phone="+1 309-555-0199"))._retrieve_partner_with_name(
            "ZZ Slot Vendor Holdings", NO_COMPANY
        )
        self.assertEqual(ours, core)
        self.assertEqual(ours, self.vendor, "control: core's own name step does match here")

    def test_the_name_step_itself_is_narrowed_to_the_other_build_s_exact_match(self):
        """🔴 THE DUAL-CORE DESIGN, at the unit level and on BOTH builds.

        The legacy core spells its name step ``('name', 'ilike', name)`` and
        the search-plan core spells it ``('name', '=ilike', name)``. One of
        those files a bill against a partner whose name merely CONTAINS the
        printed one; the other does not. This module makes the slot it owns say
        the same thing on both.

        See `res.partner._ai_retrieve_partner_with_exact_name` for why the
        narrowing is made where the decision is MADE rather than filtered
        afterwards, and why nothing is lost by it.
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
        # 🔴 THE CONTROL, and it is about the FIXTURE rather than about core:
        # a substring rule at this slot would have found exactly one row and
        # therefore decided. Without it the assertion above would also pass
        # against an empty database.
        self.assertEqual(
            self.Partner.search(
                [("name", "ilike", "ZZ Narrowed Supplies")] + NO_COMPANY
            ),
            containing,
            "control: a substring rule here resolves to exactly one partner",
        )

    # ------------------------------------------------------------------
    # 🔴 The OTHER direction of the narrowing — a review finding
    # ------------------------------------------------------------------

    def _near_name_pair(self):
        """One partner whose name the printed name matches EXACTLY, and one it
        is a prefix of. The pair the two cores answer differently."""
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
        # CONTROL ON THE FIXTURE: core's substring spelling really does return
        # BOTH, so the whole case exists. Without this the tests below would
        # also pass against a database holding neither partner.
        self.assertEqual(
            self.Partner.search(
                [("name", "ilike", "ZZ Zeta Test Oy")] + NO_COMPANY
            ),
            exact + longer,
        )
        return exact, longer

    def test_the_narrowed_step_decides_a_near_name_pair(self):
        """🔴 THE DIRECTION THE "NEVER WORSE THAN CORE" PARAGRAPH DID NOT STATE.

        ``=ilike`` matches a strict SUBSET of the rows ``ilike`` matches, so the
        slot can never return a partner the legacy core could not have found.
        What that sentence left out is that the CALLER reads ``len(partner) ==
        1`` (`account/models/partner.py:992` at `ca1f0aea`), so a smaller set
        can be DECIDED where the larger one was refused as ambiguous.

        That is deliberate, and the reason is the one the dual-core design exists for: the
        question is never *does this differ from core*, it is *do the two builds
        now answer the same*. *(measured 2026-10-10, both benches)* they did
        not, and after the narrowing they do — which the two gated controls
        below assert from each side.
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
        """🔴 HALF THE DISAGREEMENT, MEASURED ON THE BENCH THAT HAS IT.

        Core alone on this build returns BOTH rows from its substring step and
        `_retrieve_partner` then declines, so the bill arrives with no vendor.
        That is the behaviour the narrowing replaces, and this test is what
        stops the narrowing becoming dead code nobody notices: if a future core
        change makes this core decide the pair by itself, this fails and says
        so, rather than leaving an override that no longer does anything.
        """
        self._near_name_pair()

        self.assertFalse(
            self.Partner._retrieve_partner(name="ZZ Zeta Test Oy"),
            "control: this core refuses the pair, which is why the slot is "
            "narrowed at all",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_this_core_already_decides_the_near_name_pair(self):
        """🔴 THE OTHER HALF, and the reason the narrowing is not a divergence.

        *(measured)* this core's name step is ``('name', '=ilike', name)`` and
        `_import_retrieve_customer` searches ``limit=1`` with
        ``order='is_company DESC, supplier_rank DESC, …'``
        (`account/models/partner.py:1046-1053` at `189df353`) — it does not
        refuse an ambiguity at all, it picks one. So this build has always filed
        this bill against the exact-name partner, and the legacy slot now gives
        the same answer.
        """
        exact, _longer = self._near_name_pair()

        self.assertEqual(
            self.Partner._retrieve_partner(name="ZZ Zeta Test Oy"),
            exact,
            "control: this core decides the pair by itself",
        )

    def test_two_partners_with_the_SAME_exact_name_are_still_refused(self):
        """🔴 THE RESIDUAL DIVERGENCE, WRITTEN DOWN RATHER THAN DISCOVERED
        LATER (found in review).

        With TWO rows matching the printed name EXACTLY the narrowed slot hands
        both back and `_retrieve_partner`'s ``len == 1`` declines — while the
        search-plan core, searching ``limit=1`` under its own order, picks the
        higher `supplier_rank`. The two builds still differ here.

        It is deliberate and it is the safe direction: declining lands the bill
        in F27's candidate cards, where a person chooses and the choice teaches
        the matcher, rather than filing it against whichever row sorted first.
        Nothing composes an order at this slot — that is §4.3(b)'s rule and it
        still holds.
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
        """Core returns None for an empty name; the adapter must not turn that
        into a recordset, because `_retrieve_partner` tests ``len(partner) == 1``
        on whatever comes back."""
        self.assertEqual(
            self._ai(self._seller())._retrieve_partner_with_name("", NO_COMPANY),
            self.Partner._retrieve_partner_with_name("", NO_COMPANY),
        )

    # ------------------------------------------------------------------
    # 🔴 What is NOT composed at this slot. Both tests below FAIL if the
    # adapter calls `_ai_find_vendor` — which is what the dual-core design proposes.
    # ------------------------------------------------------------------

    def test_our_name_rule_does_not_run_at_core_s_name_slot(self):
        """Two top-level partners share a name, and the adapter must REFUSE to
        pick one — `_retrieve_partner` tests ``len(partner) == 1`` on whatever
        comes back, so handing it a single record IS the decision.

        🔴 THE ASSERTION USED TO BE IDENTITY WITH CORE'S OWN ANSWER, AND
        19.0.3.8.0 RETIRED THAT, which is worth stating rather than quietly
        editing. Since this module narrows core's name step itself
        (`_ai_retrieve_partner_with_exact_name` — ``=ilike`` instead of
        ``ilike``), "what core returns" and "what we return" are no longer the
        same object by construction, and on the search-plan build they never
        were: that core's `_retrieve_partner_with_name` is a deprecated stub
        that delegates to `_retrieve_partner` and resolves to ONE record, while
        the legacy one hands back two for its caller to refuse. Identity with a
        stub that nothing calls is not a property worth protecting; NOT
        RESOLVING THE AMBIGUITY is, and it is the same claim on both builds.

        `_ai_find_vendor`'s own name rule WOULD resolve it: it orders by
        `supplier_rank`, so it picks the second row. The control at the end says
        so, which is what stops this test passing by coincidence."""
        # Created low rank FIRST, so "whichever came first" and "highest
        # supplier_rank" cannot be the same record.
        low = self.Partner.create(
            {"name": "ZZ Twinned Trading", "is_company": True, "supplier_rank": 1}
        )
        high = self.Partner.create(
            {"name": "ZZ Twinned Trading", "is_company": True, "supplier_rank": 9}
        )
        # 🔴 THE FIRST CONTROL, and it can fail: `_ai_find_vendor` raises for the
        # duration. the dual-core design proposes calling exactly that method here; this
        # guard turns "we don't" from a reading of the code into a run.
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
        # 🔴 THE SECOND CONTROL: the whole matcher does resolve it, and to the
        # row our name rule prefers. Without this the assertions above would
        # also pass if nothing anywhere could ever pick a winner.
        self.assertEqual(
            self.Partner._ai_find_vendor({"name": "ZZ Twinned Trading"}),
            high,
            "control: `_ai_find_vendor` resolves by supplier_rank — which is "
            "exactly the rule that must not run at core's name slot",
        )

    def test_our_vat_rule_does_not_run_at_core_s_name_slot(self):
        """Core ran its own VAT step — twice, and with prefix and zero-padding
        variants — long before the name slot. Retrying VAT here would be a rule
        that exists on one build and not the other, which is the defect the dual-core design
        exists to remove."""
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
        """🔴 ONE BODY FOR THE COMPOSITION, not only for each rule. The
        search-plan adapter inserts business id, phone and domain as three plan
        steps; the legacy adapter runs the same three in the same order at the
        same relative position. Stubbing the shared composition must change the
        legacy adapter's answer — otherwise there are two spellings again."""
        with patch.object(
            type(self.Partner),
            "_ai_vendor_from_seller_keys",
            lambda self, seller, company=None: self.env["res.partner"],
        ):
            found = self._ai(
                self._seller(phone="212.555.0177")
            )._retrieve_partner_with_name("ZZ PRINTED NAME NOBODY HAS", NO_COMPANY)
        self.assertFalse(found, "the adapter must go through the shared composition")

    def test_the_shared_composition_leaves_vat_and_name_to_core(self):
        """The composition itself, called directly: the three keys it adds, and
        neither of the two it does not."""
        self.assertEqual(
            # `country_code` since 19.0.3.11.0: a national-form number is
            # normalised under the seller's country, and this bench's
            # installation default is Finnish. The fixture is a US vendor, so
            # the document has to say so — the same thing `_seller()` does for
            # every other test in this class.
            self.Partner._ai_vendor_from_seller_keys(
                {"phone": "212.555.0177", "country_code": "US"}
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
        """🔴 THE CONTROL for the test above. `_ai_find_vendor` is a DIFFERENT
        composition with a different job — it answers on an unknown document,
        post-decode, where no core step runs at all — and it keeps its name and
        VAT rules. If this ever starts failing, the hoist has been flattened."""
        self.assertEqual(
            self.Partner._ai_find_vendor({"name": "ZZ Slot Vendor Holdings"}),
            self.vendor,
        )


@tagged("post_install", "-at_install")
class TestBothProfilesAreCovered(TransactionCase):
    """🔴 Two benches that never disagree are one bench run twice (the dual-core design).

    🔴 AND THE ASSERTION THAT USED TO LIVE HERE COULD NOT FAIL FOR A RUN-LEVEL
    REASON (found in review). It read `PROFILE_GATED`, which the
    decorators fill when Python EVALUATES THE DECORATOR LINE — so it was
    identical on a search-plan bench, on a legacy bench, and on a bench where
    all 28 gated tests skipped because the recorded profile was `unknown`. It
    was satisfied by the decorators being present in the source. The protection
    its docstring claimed (a typo in a profile name) is really provided by the
    `ValueError` in `profiles.py`, which fires at import.

    ``test_sequence`` puts this class LAST in the suite — Odoo's loader sorts on
    exactly that attribute (`odoo/tests/loader.py:107`, identical on both
    measured frameworks) — because a run-time set read half way through a run is
    a half-filled set, and an assertion on one of those is the same kind of
    nothing as the assertion it replaces.
    """

    #: Last. See the class docstring.
    test_sequence = 1000

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
        """🔴 THE ONE ASSERTION THAT MUST BE A WRITTEN-DOWN NUMBER.

        The run-time sets above cannot see a whole test module dropped from
        `tests/__init__.py` or a `@skip_unless_profile` line deleted: the
        decorator then never evaluates, every registry shrinks together, and
        all three agree about a smaller world. A count stated in `profiles.py`
        — and kept equal to the skip counts in `__manifest__.py` — is what
        notices.
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
        """🔴 THE CONTROL ON THE CONTROL. One gate of each profile lives in the
        class that reads the registries, so `PROFILE_RAN` and `PROFILE_SKIPPED`
        are known to be written by something this run executed — not merely to
        be non-empty because some earlier module filled them."""
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


class _ImportHarness(TransactionCase):
    """One real import, both benches: the paid Claude call is the only thing
    mocked, and the extraction identifies its vendor by PHONE alone.

    🔴 THE FIXTURE IS THE ARGUMENT. Two partners exist. The right one is
    reachable only by the printed phone number, in a different format from the
    stored one. The decoy is reachable by core's own legacy name step, which is
    ``('name', 'ilike', name)`` — a SUBSTRING match — so the printed
    "ZZ Hook Supplies" finds "ZZ Hook Supplies International" and `_retrieve_partner`
    accepts it, because exactly one row came back.

    That is why "the adapter fills an empty slot" is NOT what these tests
    assert. Without it the bill is filed against the WRONG vendor and the
    module's post-decode safety net never runs, because that net is gated on an
    empty `partner_id`. The adapter has to WIN at core's name slot, not clean up
    after it.
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
                "phone": "+1 212-555-0166",
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
        """🔴 NO VAT ON THE DOCUMENT, deliberately: the legacy core creates a
        vendor from `name and vat` (`account_edi_common.py:545`), and this test
        is about matching, not about the create-suppression that the dual-core design
        owns. A US bill printing no tax id is also the common case — most
        US vendor bills print none."""
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-HOOK-1",
            "issue_date": "2026-08-06",
            "currency": "USD",
            "seller": {
                "name": "ZZ Hook Supplies",
                "country_code": "US",
                "phone": "212.555.0166",
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
        record = self.env["goodahead.invoice.import"].create(
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
    """🔴 Inert here is a MEASUREMENT, not a reading.

    On this core `_retrieve_partner_with_name` is a deprecated stub with no core
    callers, so the override cannot change anything. "Nothing calls it" is a
    claim about a tree; this is a claim about a run.
    """

    def test_a_full_import_never_enters_the_name_slot(self):
        calls, counting = self._counting_name_slot()
        with counting:
            record, outcome = self._run_import()
        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertEqual(
            calls, [], "the legacy adapter must never be entered on this build"
        )
        # 🔴 THE CONTROL: the import really ran, and really matched, through the
        # search plan. Without this the empty list above would also be what a
        # broken import produced.
        self.assertEqual(record.move_id.partner_id, self.vendor)


@skip_unless_profile(PROFILE_LEGACY)
@tagged("post_install", "-at_install")
class TestTheNameSlotFiresOnTheLegacyBuild(_ImportHarness):
    """🔴 The half that could not be written before this step: the module now
    has a live hook on the legacy core."""

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
        """🔴 THE CONTROL, and the reason the adapter exists.

        With this module's whole name-slot override replaced by CORE's own
        implementation, the same import reaches core's `name ilike` step,
        matches the decoy, and nothing anywhere disagrees — the module's
        post-decode safety net is gated on an EMPTY `partner_id`, so a wrong
        match silences it.

        ⚠️ IT USED TO STUB `_ai_vendor_from_seller_keys` ONLY, which stopped
        being a control the moment 19.0.3.8.0 narrowed the name step itself: the
        stub disarmed our three keys and left our `=ilike` narrowing in place,
        so "core's own name step" was not what ran. Patching in core's class
        function covers BOTH of this module's additions at this slot and tracks
        whatever core's rule actually is, rather than a sentence written here.
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
    """🔴 THE DUAL-CORE DESIGN — THE GAP THAT HAD NO STEP. Both builds, one assertion.

    *(measured, 2026-10-10)* core's name step is spelled differently on the two
    19.0 builds this module targets:

      * search-plan — `res.partner._import_retrieve_customer_from_name` searches
        ``[('name', '=ilike', name)]``: an EXACT, case-insensitive match
        (`account/models/partner.py:1013` at `189df353`).
      * legacy — `res.partner._retrieve_partner_with_name` searches
        ``[('name', 'ilike', name)]``: a SUBSTRING match
        (`account/models/partner.py:956` at `ca1f0aea`), accepted by
        `_retrieve_partner` whenever exactly one row comes back.

    So on the legacy build a printed vendor name that is merely a SUBSTRING of
    one existing partner's name wins, confidently. And because this module's
    whole recovery path — the banner, the candidate cards, the chatter note,
    the learning — is gated on an EMPTY `partner_id`, a wrong match does not
    add a row: it SILENCES all of it. F27's promise ("no vendor is invented,
    and an unrecognised one is visible") is not kept on that build, and the dual-core design
    step 4 cannot fix it, because suppressing a CREATE cannot suppress a MATCH.

    🔴 THE FIXTURE PRINTS NO PHONE, deliberately. The harness's vendor is
    reachable only by phone, so dropping it leaves NO key of ours able to fire
    and puts core's name step alone in the frame. That is the only way to
    measure core's name step rather than our adapter.

    THIS CLASS IS NOT PROFILE-GATED, and that is the point: one fixture, one
    assertion, the SAME expected outcome on both builds. Two benches that never
    disagree are one bench run twice — but two benches that disagree about
    whether a vendor was invented are a defect, not a profile.
    """

    def _extracted(self):
        extracted = super()._extracted()
        # 🔴 The one change from the harness. With the phone gone, nothing this
        # module knows can identify the seller, so whatever decides is core's.
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
        """🔴 THE ARGUMENT THAT NOTHING IS LOST, AND THE CONTROL.

        Narrowing the decision does not throw the information away: every
        partner core's substring step could have matched is already in the set
        `account.move._ai_vendor_candidates` offers under *"similar name"* —
        it searches the same ``('name', 'ilike', name)``. So the change moves
        this partner from a SILENT DECISION to a VISIBLE CANDIDATE, which is
        the vocabulary F27 already put in this module.

        It is also the control for the test above: without it, an import that
        failed outright would produce an empty `partner_id` too and the
        assertion would pass for the wrong reason.
        """
        record, _outcome = self._run_import()

        self.assertIn(
            self.decoy,
            record.move_id.ai_vendor_candidate_ids.partner_id,
            "the partner the name resembles must be offered, not discarded",
        )
