# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The ONE vendor matcher, hoisted onto `res.partner` and reachable without any
core import pipeline at all (the dual-core design).

🔴 WHAT THIS FILE IS FOR, AND IT IS NOT "does matching work". That is already
covered by `test_partner_matching.py` and `test_vendor_identifiers.py`, which
are left alone on purpose — they are the bit-identical-behaviour control for
this change, and a change that needed them edited would not be a hoist.

What is new here is three claims that nothing could make before:

1. **`res.partner._ai_find_vendor` runs with no core seam involved.** Not "it
   happens not to call one today" — the guard below replaces every EDI entry
   point this module owns with a function that raises, and the match still
   succeeds. On the older core (`legacy`) none of those entry points is ever
   called by anything, so a matcher that could only be reached through them
   would be dead code there. That is the silent no-op the dual-core design exists to prevent.
2. **There is exactly ONE body per matching rule.** The three
   `_import_retrieve_customer_from_ai_*` search-plan steps and `_ai_find_vendor`
   now call the same helpers, and the tests prove it by STUBBING the helper and
   watching both callers change their answer. Two spellings of one rule is the
   real portability bug — a fix applied to one is invisible in the other — and
   a test that only checks "both give the right answer today" would not see a
   divergence appear tomorrow.
3. **`account.move._ai_match_existing_vendor` is now a delegation.** It keeps
   its name because a dozen tests and the F27 chatter path call it; it keeps
   its answer because `_ai_find_vendor` IS its old body.

🔴 EVERY TEST HERE CARRIES A CONTROL — something that would FAIL if the thing
under test were stubbed out or made unconditional.

Fake `ZZ…` names and numbers throughout, so a DB-touching assertion can never
collide with a real vendor in a migrated test database.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


class _Boom(Exception):
    """Raised by the EDI guard below. A distinct class so a test cannot pass by
    catching something the ORM raises for an unrelated reason."""


def _explode(*args, **kwargs):
    raise _Boom("an EDI entry point was called")


@tagged("post_install", "-at_install")
class TestAiFindVendorIsCoreFree(TransactionCase):
    """`res.partner._ai_find_vendor` — the matcher, with no pipeline under it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        cls.us = cls.env.ref("base.us")
        cls.fi = cls.env.ref("base.fi")

        cls.wideworld = cls.Partner.create(
            {
                "name": "ZZ Wide World Import",
                "is_company": True,
                "supplier_rank": 9,
                "phone": "+1 212-555-0112",
                "email": "info@zzwideworldimport.test",
                "country_id": cls.us.id,
            }
        )
        cls.oy = cls.Partner.create(
            {
                "name": "ZZ Vendor Oy",
                "is_company": True,
                "supplier_rank": 3,
                "company_registry": "ZZFI0042",
                "country_id": cls.fi.id,
            }
        )
        cls.vatted = cls.Partner.create(
            {
                "name": "ZZ Vatted Trading",
                "is_company": True,
                "supplier_rank": 2,
                "vat": "ZZ99887766",
                "country_id": cls.us.id,
            }
        )

    # ------------------------------------------------------------------
    # It exists, on res.partner, and it is ours
    # ------------------------------------------------------------------

    def test_the_matcher_lives_on_res_partner_and_in_this_module(self):
        """🔴 The whole point of the hoist. `account.move` is not a model the
        legacy pipeline hands to anybody during partner retrieval; `res.partner`
        is both adapters' own model. A matcher on the wrong model is reachable
        from one pipeline only."""
        klass = next(
            k
            for k in type(self.Partner).__mro__
            if "_ai_find_vendor" in vars(k)
        )
        self.assertTrue(
            (klass.__module__ or "").startswith(
                "odoo.addons.goodahead_invoice_import"
            ),
            "_ai_find_vendor must be defined by this module, not inherited from "
            "something that happens to carry the name: %s" % klass.__module__,
        )

    # ------------------------------------------------------------------
    # The five rules, through the hoisted method
    # ------------------------------------------------------------------

    def test_it_matches_on_vat(self):
        self.assertEqual(
            self.Partner._ai_find_vendor({"name": "ZZ Anything", "vat": "zz 998 877 66"}),
            self.vatted,
        )

    def test_it_matches_on_a_business_id_scoped_to_its_country(self):
        """The printed separators are formatting, not identity — `ZZ FI 0042`
        and `ZZFI0042` are the same registration number."""
        self.assertEqual(
            self.Partner._ai_find_vendor(
                {"name": "ZZ Anything", "business_id": "ZZ FI 0042", "country_code": "FI"}
            ),
            self.oy,
        )

    def test_a_business_id_without_a_country_matches_nothing(self):
        """🔴 THE CONTROL. A national registration number is unique inside its
        country and nowhere else, so without one this is a free-text match on a
        short string across every partner in the database."""
        self.assertFalse(
            self.Partner._ai_find_vendor(
                {"name": "ZZ Anything", "business_id": "ZZ FI 0042"}
            )
        )

    def test_it_matches_on_a_normalised_phone(self):
        self.assertEqual(
            # `country_code` since 19.0.3.11.0: a national-form number means
            # nothing without one, and the legacy bench's installation is
            # Finnish — without it this assertion would mean two different
            # things on the two benches while reading identically.
            self.Partner._ai_find_vendor(
                {
                    "name": "ZZ Anything",
                    "phone": "212.555.0112",
                    "country_code": "US",
                }
            ),
            self.wideworld,
        )

    def test_it_matches_on_an_email_domain(self):
        self.assertEqual(
            self.Partner._ai_find_vendor(
                {"name": "ZZ Anything", "email": "purchasing@zzwideworldimport.test"}
            ),
            self.wideworld,
        )

    def test_the_three_added_keys_are_tried_in_order_registry_phone_domain(self):
        """🔴 THE ONE REAL COVERAGE HOLE A TEST AUDIT FOUND, AND IT
        IS LIVE ON BOTH PROFILES.

        The precedence AMONG this module's own three keys —
        registry → phone → domain, `res_partner.py:292-315` — is the shared body
        both adapters run. It was asserted in exactly one place: a
        `search_plan`-GATED test that reads the search *plan's* ordered list,
        which is a different object. So on the legacy bench nothing checked it
        at all, and on either bench swapping the phone block above the registry
        block would have left every test green while a seller carrying a correct
        `business_id` for A and a phone resolving to B filed the bill against B.

        Each key here points at a DIFFERENT partner, so the answer names the
        rule that won. Ungated, through `_ai_vendor_from_seller_keys` itself —
        the body, not one caller's spelling of it.

        CONTROL: the same seller with the registry key removed must fall to the
        PHONE partner, and with the phone removed too must fall to the DOMAIN
        one. A method that always returned its first argument's registry match
        would fail both.
        """
        by_phone = self.Partner.create(
            {
                "name": "ZZ Precedence Phone Co",
                "is_company": True,
                "phone": "+1 212-555-0119",
                "country_id": self.us.id,
            }
        )
        by_domain = self.Partner.create(
            {
                "name": "ZZ Precedence Domain Co",
                "is_company": True,
                "email": "ap@zzprecedencedomain.com",
                "country_id": self.us.id,
            }
        )
        self.assertNotEqual(self.oy, by_phone, "CONTROL: three distinct partners")
        self.assertNotEqual(by_phone, by_domain)

        seller = {
            "business_id": "ZZ FI 0042",
            "country_code": "FI",
            "phone": "+1 212-555-0119",
            "email": "billing@zzprecedencedomain.com",
        }
        self.assertEqual(
            self.Partner._ai_vendor_from_seller_keys(seller),
            self.oy,
            "🔴 a national registration number is an EXACT key and outranks a "
            "telephone; nothing but this asserts it on either bench",
        )

        without_registry = dict(seller, business_id=None)
        self.assertEqual(
            self.Partner._ai_vendor_from_seller_keys(without_registry),
            by_phone,
            "CONTROL: phone is next, above the domain",
        )

        without_phone = dict(without_registry, phone=None)
        self.assertEqual(
            self.Partner._ai_vendor_from_seller_keys(without_phone),
            by_domain,
            "CONTROL: and the domain is last of the three",
        )

    def test_it_matches_on_the_printed_name_last(self):
        self.assertEqual(
            self.Partner._ai_find_vendor({"name": "zz wide world import"}), self.wideworld
        )

    def test_a_document_that_identifies_nobody_matches_nobody(self):
        """🔴 THE CONTROL for every test above: a matcher that returned "the
        first supplier it could find" would pass all six and file every bill
        against the same vendor."""
        self.assertFalse(
            self.Partner._ai_find_vendor(
                {
                    "name": "ZZ Nobody At All",
                    "phone": "+1 309-555-0144",
                    "email": "ap@zznobodyatall.example",
                }
            )
        )

    def test_an_empty_seller_is_not_an_error(self):
        """The legacy adapter (step 3) calls this with whatever the context
        carries, which on a non-AI document is nothing at all."""
        self.assertFalse(self.Partner._ai_find_vendor({}))
        self.assertFalse(self.Partner._ai_find_vendor(None))

    # ------------------------------------------------------------------
    # 🔴 The claim that needed a new test: no core seam is involved
    # ------------------------------------------------------------------

    def _edi_guard(self):
        """Replace every EDI entry point this module owns with a raiser."""
        move_cls = type(self.AM)
        ubl_cls = type(self.env["account.edi.ubl"])
        common_cls = type(self.env["account.edi.common"])
        return [
            patch.object(move_cls, "_get_edi_decoder", _explode),
            patch.object(move_cls, "_ai_decode_ubl_into_move", _explode),
            patch.object(
                ubl_cls, "_import_ubl_retrieve_customer_search_plan", _explode
            ),
            patch.object(ubl_cls, "_import_ubl_create_missing_customer", _explode),
            patch.object(common_cls, "_import_partner_bank", _explode),
        ]

    def test_the_matcher_runs_with_every_edi_entry_point_booby_trapped(self):
        """🔴 THE TEST STEP 2 EXISTS FOR. On the legacy core none of these five
        is ever reached during a vendor-bill import, so logic that could only be
        got at through them is dead code there — installed, green, never run."""
        guards = self._edi_guard()
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        self.assertEqual(
            # `country_code` since 19.0.3.11.0: a national-form number means
            # nothing without one, and the legacy bench's installation is
            # Finnish — without it this assertion would mean two different
            # things on the two benches while reading identically.
            self.Partner._ai_find_vendor(
                {
                    "name": "ZZ Anything",
                    "phone": "212.555.0112",
                    "country_code": "US",
                }
            ),
            self.wideworld,
        )

    def test_the_booby_trap_really_goes_off(self):
        """🔴 THE CONTROL FOR THE TEST ABOVE, and without it that test proves
        nothing: a guard that patched the wrong class would let anything pass.
        This project's own rule from gate check 1b — a zero is only trustworthy
        when a fixture proves the tool can still fail."""
        guards = self._edi_guard()
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        with self.assertRaises(_Boom):
            self.env["account.edi.ubl"]._import_ubl_create_missing_customer({})


@tagged("post_install", "-at_install")
class TestOneBodyPerRule(TransactionCase):
    """🔴 There must be exactly one implementation of each matching rule, and
    the search-plan steps must be wrappers over it.

    Each test stubs the shared helper and asserts that BOTH callers — the plan
    step and `_ai_find_vendor` — change their answer. A reintroduced second
    spelling makes exactly one of the two stop moving, which is the divergence
    that no "both are right today" test can see.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        cls.decoy = cls.Partner.create(
            {"name": "ZZ Stub Answer", "is_company": True, "supplier_rank": 1}
        )

    def _partner_from_plan(self, plan_values):
        """Resolve a plan step's return value the way core's plan runner does."""
        if not plan_values:
            return self.Partner
        found = self.Partner
        for criteria in plan_values["criteria"]:
            found = self.Partner.search(criteria["domain"], limit=1)
            if found:
                return found
        return found

    def test_the_phone_rule_has_one_body(self):
        decoy = self.decoy

        # `country_code` arrived with 19.0.3.11.0 and BOTH callers pass it, which
        # is the point of the test: a stub that did not accept it would fail
        # with a TypeError from whichever caller still had the old signature.
        def _stub(self, raw_phone, country_code=None):
            return decoy if raw_phone else self.browse()

        with patch.object(type(self.Partner), "_ai_vendor_by_phone", _stub):
            self.assertEqual(
                self._partner_from_plan(
                    self.Partner._import_retrieve_customer_from_ai_phone(
                        {"phone": "anything at all"}
                    )
                ),
                decoy,
                "the search-plan step does not go through _ai_vendor_by_phone",
            )
            self.assertEqual(
                self.Partner._ai_find_vendor({"phone": "anything at all"}),
                decoy,
                "_ai_find_vendor does not go through _ai_vendor_by_phone",
            )

    def test_the_domain_rule_has_one_body(self):
        decoy = self.decoy

        def _stub(self, raw_values):
            return decoy if any(raw_values) else self.browse()

        with patch.object(type(self.Partner), "_ai_vendor_by_domains", _stub):
            self.assertEqual(
                self._partner_from_plan(
                    self.Partner._import_retrieve_customer_from_ai_domain(
                        {"email": "ap@zzanything.example"}
                    )
                ),
                decoy,
                "the search-plan step does not go through _ai_vendor_by_domains",
            )
            self.assertEqual(
                self.Partner._ai_find_vendor({"email": "ap@zzanything.example"}),
                decoy,
                "_ai_find_vendor does not go through _ai_vendor_by_domains",
            )

    def test_the_business_id_rule_has_one_body(self):
        marker = [("name", "=", "ZZ Stub Answer")]

        def _stub(self, business_id, country_code):
            return marker if business_id and country_code else None

        with patch.object(
            type(self.Partner), "_ai_vendor_registry_domain", _stub
        ):
            self.assertEqual(
                self._partner_from_plan(
                    self.Partner.with_context(
                        ai_seller_business_id="ZZ-1", ai_seller_country_code="FI"
                    )._import_retrieve_customer_from_ai_business_id({})
                ),
                self.decoy,
                "the search-plan step does not go through _ai_vendor_registry_domain",
            )
            self.assertEqual(
                self.Partner._ai_find_vendor(
                    {"business_id": "ZZ-1", "country_code": "FI"}
                ),
                self.decoy,
                "_ai_find_vendor does not go through _ai_vendor_registry_domain",
            )

    def test_the_move_level_matcher_is_a_delegation(self):
        """`account.move._ai_match_existing_vendor` keeps its name — a dozen
        tests and the F27 chatter path call it — and must now be nothing but a
        call into `res.partner._ai_find_vendor`."""
        decoy = self.decoy

        def _stub(self, seller, company=None):
            return decoy

        with patch.object(type(self.Partner), "_ai_find_vendor", _stub):
            self.assertEqual(
                self.AM._ai_match_existing_vendor({"name": "ZZ Irrelevant"}), decoy
            )


@tagged("post_install", "-at_install")
class TestSellerContextRelay(TransactionCase):
    """The whole extracted seller dict rides on the context, so an adapter on
    EITHER pipeline can rebuild it without reading core's decoded values.

    The three single-key relays (`ai_seller_business_id`, `ai_seller_country_code`,
    `ai_seller_website`) stay exactly as they are — the search-plan steps read
    them and this change may not touch what they do.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]

    def test_the_seller_dict_round_trips_through_the_context(self):
        seller = {"name": "ZZ Relay", "phone": "212.555.0112", "country_code": "US"}
        self.assertEqual(
            self.Partner.with_context(ai_seller=seller)._ai_seller_from_context(),
            seller,
        )

    def test_no_relay_means_an_empty_dict_not_a_crash(self):
        """🔴 THE CONTROL. `_retrieve_partner_with_name` (step 3's seam) is
        reachable outside our own import, where nothing has put anything on the
        context. Returning `{}` is what lets the adapter decline quietly instead
        of raising on somebody else's document."""
        self.assertEqual(self.Partner._ai_seller_from_context(), {})
        self.assertEqual(
            self.Partner.with_context(ai_seller="not a dict")._ai_seller_from_context(),
            {},
        )
