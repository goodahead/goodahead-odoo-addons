# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The vendor matcher on `res.partner`, reachable without any core import pipeline.

1. `res.partner._ai_find_vendor` works with every EDI entry point replaced by a
   raiser, so it works on either import pipeline.
2. Each matching rule has one implementation: stubbing the shared helper changes
   the answer of both the search-plan step and `_ai_find_vendor`.
3. `account.move._ai_match_existing_vendor` delegates to `_ai_find_vendor`.

Fake `ZZ…` names and numbers avoid matching existing partners.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


class _Boom(Exception):
    """Raised by the EDI guard below; distinct from anything the ORM raises."""


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

        cls.liberty = cls.Partner.create(
            {
                "name": "ZZ Liberty Import",
                "is_company": True,
                "supplier_rank": 9,
                "phone": "+1 212-555-0112",
                "email": "info@zzlibertyimport.com",
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
        """The matcher is defined by this module on `res.partner`, the model both
        pipelines use during partner retrieval."""
        klass = next(
            k
            for k in type(self.Partner).__mro__
            if "_ai_find_vendor" in vars(k)
        )
        self.assertTrue(
            (klass.__module__ or "").startswith(
                "odoo.addons.goodahead_invoice_ai_import"
            ),
            "_ai_find_vendor must be defined by this module, not inherited from "
            "something that happens to carry the name: %s" % klass.__module__,
        )

    # ------------------------------------------------------------------
    # The five rules
    # ------------------------------------------------------------------

    def test_it_matches_on_vat(self):
        self.assertEqual(
            self.Partner._ai_find_vendor({"name": "ZZ Anything", "vat": "zz 998 877 66"}),
            self.vatted,
        )

    def test_it_matches_on_a_business_id_scoped_to_its_country(self):
        """Separators are ignored: `ZZ FI 0042` and `ZZFI0042` are the same number."""
        self.assertEqual(
            self.Partner._ai_find_vendor(
                {"name": "ZZ Anything", "business_id": "ZZ FI 0042", "country_code": "FI"}
            ),
            self.oy,
        )

    def test_a_business_id_without_a_country_matches_nothing(self):
        """A registration number is only unique within its country."""
        self.assertFalse(
            self.Partner._ai_find_vendor(
                {"name": "ZZ Anything", "business_id": "ZZ FI 0042"}
            )
        )

    def test_it_matches_on_a_normalised_phone(self):
        self.assertEqual(
            # A national-form number needs its country to be normalised.
            self.Partner._ai_find_vendor(
                {
                    "name": "ZZ Anything",
                    "phone": "212.555.0112",
                    "country_code": "US",
                }
            ),
            self.liberty,
        )

    def test_it_matches_on_an_email_domain(self):
        self.assertEqual(
            self.Partner._ai_find_vendor(
                {"name": "ZZ Anything", "email": "purchasing@zzlibertyimport.com"}
            ),
            self.liberty,
        )

    def test_the_three_added_keys_are_tried_in_order_registry_phone_domain(self):
        """The module's own keys are tried in the order registry, phone, domain.

        Each key points at a different partner, so the answer names the rule that
        won. Removing the registry falls to the phone partner, then to the domain one.
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
            "a national registration number is an EXACT key and outranks a "
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
            self.Partner._ai_find_vendor({"name": "zz liberty import"}), self.liberty
        )

    def test_a_document_that_identifies_nobody_matches_nobody(self):
        """Control: a seller that identifies no partner matches nothing."""
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
        """On a non-AI document the legacy adapter passes an empty seller."""
        self.assertFalse(self.Partner._ai_find_vendor({}))
        self.assertFalse(self.Partner._ai_find_vendor(None))

    # ------------------------------------------------------------------
    # No core seam is involved
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
        """The matcher works with every EDI entry point raising, so it does not
        depend on either import pipeline."""
        guards = self._edi_guard()
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        self.assertEqual(
            self.Partner._ai_find_vendor(
                {
                    "name": "ZZ Anything",
                    "phone": "212.555.0112",
                    "country_code": "US",
                }
            ),
            self.liberty,
        )

    def test_the_booby_trap_really_goes_off(self):
        """Control: the guard really raises."""
        guards = self._edi_guard()
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        with self.assertRaises(_Boom):
            self.env["account.edi.ubl"]._import_ubl_create_missing_customer({})


@tagged("post_install", "-at_install")
class TestOneBodyPerRule(TransactionCase):
    """Each matching rule has one implementation; the search-plan steps wrap it.

    Each test stubs the shared helper and asserts that both the plan step and
    `_ai_find_vendor` change their answer.
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

        # Both callers pass `country_code`; the stub accepts it.
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
        """`account.move._ai_match_existing_vendor` delegates to `_ai_find_vendor`."""
        decoy = self.decoy

        def _stub(self, seller, company=None):
            return decoy

        with patch.object(type(self.Partner), "_ai_find_vendor", _stub):
            self.assertEqual(
                self.AM._ai_match_existing_vendor({"name": "ZZ Irrelevant"}), decoy
            )


@tagged("post_install", "-at_install")
class TestSellerContextRelay(TransactionCase):
    """The extracted seller dict rides on the context for adapters on either pipeline.

    The single-key relays read by the search-plan steps are kept alongside it.
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
        """Outside this module's import nothing is on the context, so `{}` comes back."""
        self.assertEqual(self.Partner._ai_seller_from_context(), {})
        self.assertEqual(
            self.Partner.with_context(ai_seller="not a dict")._ai_seller_from_context(),
            {},
        )
