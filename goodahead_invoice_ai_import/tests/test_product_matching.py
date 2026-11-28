# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The product plan narrowing, and the proof it changes the outcome.

🔴 WHAT IS ACTUALLY BEING TESTED — read this before adding a case.

On the SEARCH-PLAN build, core already has a correct, vendor-scoped
supplierinfo match at slot 5, and `_import_ubl_invoice_line_add_product_values`
already hands it `vendor_partner_id`. This module adds NO matching there. What
it does is narrow the plan when a purchase document's vendor is known, to
supplierinfo + barcode, dropping core's `default_code` (15), `name` (20) and
the predictive step.

🆕 On the LEGACY build (19.0.3.8.0, the dual-core design) the decision is the same and
the seam is `product.product._get_product_domain_search_order`, because that is
where THAT core keeps the plan. One difference matters: that core has no
supplierinfo step at all, so the module supplies the domain — copied verbatim
from the other core's step, with
`test_supplierinfo_is_scoped_to_the_same_row_not_merely_the_product` as the
tripwire. Everything else, including the company scoping and the two-pass loop,
stays core's. See `models/product_product.py`.

So the only behaviour that can discriminate is the FALL-THROUGH: a vendor code
with **no supplierinfo row**, where core keeps going and lands on one of our own
products whose internal reference happens to be that same string.

⚠️ AN EARLIER VERSION OF THIS FILE TESTED THE WRONG THING AND COULD NOT FAIL.
It built a product WITH a supplierinfo row plus a decoy, and asserted the right
product won — but core's own slot-5 supplierinfo step finds that product too, so
the test passed identically with the override deleted. Its "control" patched in
a hand-written three-step plan (barcode / default_code / name) that no shipped
19.0 core has had since 2026-05-04: a control over a fiction. The hook was live;
the evidence was circular. Both are replaced below.

The discriminating test is
`test_an_unmatched_vendor_code_does_not_fall_through_to_our_default_code`, and
its control asserts that core's REAL plan picks the decoy. It and
`test_a_mapped_vendor_code_still_resolves_through_supplierinfo` are UNGATED on
purpose: they state the module's promise, not one build's answer to it, and
they were the two red tests the dual-core design was written as.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
)

from .profiles import skip_method_unless_profile

# Core's own classes, imported so a control can call core's implementation
# directly. Importing the CLASS (not going through the registry) is what makes
# it possible to ask "what would core alone have done?" from inside a tree where
# this module is installed.
from odoo.addons.account_edi_ubl_cii.models.account_edi_ubl import (
    AccountEdiUBL as CoreAccountEdiUBL,
)
from odoo.addons.account_edi_ubl_cii.models.account_edi_common import (
    AccountEdiCommon as CoreAccountEdiCommon,
)

# The name core gives the product search plan on THIS build, asked of core's own
# classes (the registry has both names, through this module). Core 5baa557
# (2026-09-01) moved it from `account.edi.ubl._import_ubl_retrieve_products_
# search_plan` to `account.edi.common._import_retrieve_products_search_plan`.
if hasattr(CoreAccountEdiCommon, "_import_retrieve_products_search_plan"):
    PLAN_HOOK = "_import_retrieve_products_search_plan"
    CORE_PLAN_OWNER = CoreAccountEdiCommon
else:
    PLAN_HOOK = "_import_ubl_retrieve_products_search_plan"
    CORE_PLAN_OWNER = CoreAccountEdiUBL

# A code we DO have a supplierinfo row for.
MAPPED_CODE = "ZZ-ART-7788"
# A code we have NO supplierinfo row for, which also happens to be the internal
# reference of an unrelated product of ours. This is the collision.
UNMAPPED_CODE = "ZZ-ART-9999"


@tagged("post_install", "-at_install")
class TestProductMatching(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.fi = cls.env.ref("base.fi")

        # ⚠️ `FI00000166` passes the Finnish Y-tunnus check digit; the fixture
        # used to say `…77`, which does not. On the search-plan bench nothing
        # noticed, because `base_vat` is not installed there. On the legacy
        # bench it is, and `_check_vat` raised a `ValidationError` inside
        # `setUpClass` — taking all fourteen tests in this class down with a
        # message about a VAT number, in a file about product matching.
        # A fixture that is only valid where nobody checks it is not a fixture.
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "ZZ Product Vendor Oy",
                "is_company": True,
                "company_registry": "ZZ-PROD-0001",
                "country_id": cls.fi.id,
                "vat": "FI00000166",
            }
        )

        # Mapped: our product, linked to the vendor by their article code.
        cls.mapped_product = cls.env["product.product"].create(
            {"name": "ZZ Mapped Product", "default_code": "OUR-INTERNAL-1"}
        )
        cls.env["product.supplierinfo"].create(
            {
                "partner_id": cls.vendor.id,
                "product_tmpl_id": cls.mapped_product.product_tmpl_id.id,
                "product_code": MAPPED_CODE,
            }
        )

        # 🔴 The decoy. No supplierinfo row anywhere links it to this vendor;
        # its own internal reference merely equals a code the vendor uses. Core
        # reaches it at slot 15 once supplierinfo has declined.
        cls.decoy = cls.env["product.product"].create(
            {"name": "ZZ Decoy Product", "default_code": UNMAPPED_CODE}
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extracted(self, product_code):
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-PROD-1",
            "issue_date": "2026-09-09",
            "currency": "EUR",
            "seller": {
                "name": "ZZ Product Vendor Oy",
                "vat": "FI00000166",
                "business_id": "ZZ-PROD-0001",
                "country_code": "FI",
            },
            "buyer": {"name": self.company.name, "country_code": "FI"},
            "lines": [
                {
                    "product_code": product_code,
                    "name": "Something the vendor calls their own thing",
                    "quantity": 2.0,
                    "unit_code": "C62",
                    "price_unit": 10.0,
                    "line_total": 20.0,
                    "tax_percent": 0.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 0.0,
                    "taxable_amount": 20.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "totals": {
                "line_extension": 20.0,
                "tax_exclusive": 20.0,
                "tax_inclusive": 20.0,
                "payable": 20.0,
            },
        }

    def _decode(self, extracted):
        """Run ONLY the decoder — no `_ai_post_process`."""
        AccountMove = self.env["account.move"]
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.company.id)], limit=1
        )
        xml_bytes = AccountMove._ai_generate_ubl_from_extracted(extracted, self.company)
        move = AccountMove.create(
            {"move_type": "in_invoice", "journal_id": journal.id}
        )
        error = AccountMove._ai_decode_ubl_into_move(move, xml_bytes, extracted)
        self.assertFalse(error, error)
        return move

    def _product_on_line(self, move):
        lines = move.invoice_line_ids.filtered(
            lambda line: line.display_type not in ("line_section", "line_note")
        )
        self.assertTrue(lines, "the decoder produced no invoice line")
        return lines[0].product_id

    def _plan_names(self, collected_values):
        decoder = self.env["account.edi.xml.ubl_bis3"]
        return [
            getattr(step, "__name__", "")
            for step in getattr(decoder, PLAN_HOOK)(collected_values)
        ]

    # ------------------------------------------------------------------
    # The plan shape (a proxy — the behavioural tests are below)
    # ------------------------------------------------------------------

    # 🔴 The next four read or patch
    # `account.edi.ubl._import_ubl_retrieve_products_search_plan`, which core
    # declares only on the search-plan build. There is no product search plan
    # on the legacy build to narrow, so `super()` raises there and no portable
    # spelling exists. Gated per method, because the two BEHAVIOURAL tests
    # below them are not gated and must keep running on both — see their notes.
    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_plan_keeps_cores_supplierinfo_and_barcode_only(self):
        names = self._plan_names(
            {
                "odoo_document_type": "purchase",
                "customer_values": {"customer": self.vendor},
            }
        )
        self.assertEqual(
            names,
            [
                "_import_retrieve_product_from_supplierinfo",
                "_import_retrieve_product_from_barcode",
            ],
            "the plan must be exactly core's two safe steps, in that order",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_plan_is_untouched_without_a_vendor(self):
        names = self._plan_names(
            {"odoo_document_type": "purchase", "customer_values": {}}
        )
        self.assertIn("_import_retrieve_product_from_default_code", names)

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_plan_is_untouched_on_a_sale_document(self):
        names = self._plan_names(
            {
                "odoo_document_type": "sale",
                "customer_values": {"customer": self.vendor},
            }
        )
        self.assertIn("_import_retrieve_product_from_default_code", names)

    # ------------------------------------------------------------------
    # The same decision, the other build's seam
    # ------------------------------------------------------------------
    #
    # 🔴 The next three read `product.product._get_product_domain_search_order`,
    # which is where the legacy core keeps the product plan.
    #
    # 🔴 THEY USED TO BE GATED `legacy`, AND THAT WAS a review finding.
    # The reason given was that nothing CALLS this method on the search-plan
    # build — true, and beside the point: these three do not need a caller,
    # they call it themselves. Core DECLARES it on both builds with the same
    # body and the same return shape (`account/models/product.py:538` at
    # `189df353`, `:291` at `ca1f0aea`), and this module's override reads only
    # the context key and `vals`. So they are profile-agnostic, and they are
    # the ONLY direct tests of `_ai_vendor_supplierinfo_domain`
    # (`models/product_product.py:85-89`) — the hand-copy of core's `any`-scoped
    # row domain. `test_supplierinfo_is_scoped_to_the_same_row_not_merely_the_
    # product` is named as that copy's tripwire, but it drives `_decode`, which
    # on the search-plan build reaches CORE's supplierinfo step and never the
    # copy. Gated, the `19.0.1.3.0` two-leaf cross-vendor collision could be
    # reintroduced with the US bench reporting 0 failed.

    # 🔴 THE TRIPWIRE ON THE ONE HAND-COPIED DOMAIN, run on BOTH benches.
    #
    # Two independent dotted leaves (`seller_ids.partner_id` and
    # `seller_ids.product_code`) are ANDed on the PRODUCT over an x2many, so a
    # product carrying rows (VendorA, "111") and (VendorB, "XXX") matches
    # VendorA quoting "XXX". That bug shipped at `19.0.1.3.0`. Core avoids it
    # with `any`, scoping both leaves to ONE supplierinfo row, and this is the
    # assertion that says the copy still does.

    def _legacy_plan(self, vendor=None, **vals):
        Product = self.env["product.product"]
        if vendor is not None:
            Product = Product.with_context(ai_import_vendor_partner_id=vendor.id)
        return Product._get_product_domain_search_order(**vals)

    def test_the_legacy_plan_keeps_supplierinfo_and_barcode_only(self):
        plan = self._legacy_plan(
            vendor=self.vendor,
            default_code=UNMAPPED_CODE,
            barcode="ZZ-GTIN-1",
            name="Something the vendor calls their own thing",
        )

        self.assertEqual(
            [priority for priority, _domain in plan],
            [5, 10],
            "the plan must be exactly supplierinfo then barcode: %r" % (plan,),
        )
        # `Domain.__repr__` is the old-style list, so this reads the real
        # domain rather than a name the test made up.
        supplierinfo = repr(plan[0][1])
        self.assertIn("product_tmpl_id.seller_ids", supplierinfo)
        self.assertIn("any", supplierinfo)
        self.assertIn("partner_id", supplierinfo)
        self.assertIn("product_code", supplierinfo)
        self.assertIn("barcode", repr(plan[1][1]))
        # 🔴 The real assertion is the ABSENCE: core reaches `default_code` at
        # slot 10 and `name` at 15/20, and those are the guesses being refused.
        self.assertNotIn("default_code", repr(plan))
        self.assertNotIn("'name'", repr(plan))

    def test_the_legacy_plan_is_untouched_without_a_vendor(self):
        """CONTROL. Without a vendor there is no vendor scope to apply, and
        narrowing would only make matching worse — so core's plan is returned
        whole, `default_code` and all. It also proves the test above is
        measuring the narrowing rather than an empty core."""
        plan = self._legacy_plan(default_code=UNMAPPED_CODE, name="ZZ Thing")

        self.assertIn("default_code", repr(plan))

    def test_a_code_the_vendor_does_not_quote_produces_no_supplierinfo_step(self):
        """With no codes at all there is nothing to scope, so the supplierinfo
        step is not emitted — rather than emitted with an empty `in []`, which
        matches everything's opposite and would be a silent decline."""
        plan = self._legacy_plan(vendor=self.vendor, name="ZZ Thing")

        self.assertEqual(plan, [])

    # ------------------------------------------------------------------
    # The relay, and the two things it must not do
    # ------------------------------------------------------------------

    def _vendor_seen_by_import_lines(self, record, tax_type, ours=True):
        """Run `_import_lines` with core's body replaced by a spy, and report
        the vendor id this module put on the context for it.

        Patching CORE's class (not the registry class) is what makes `super()`
        inside our override land on the spy — the same trick as
        `test_control_core_alone_would_have_picked_the_decoy` above, and for
        the same reason: a `super(...)` walk from the registry class comes
        straight back to our own implementation.

        `ours=False` drops the `ai_invoice_import` key, i.e. asks what a
        document this module did not produce gets.
        """
        return self._context_seen_by_import_lines(record, tax_type, ours=ours).get(
            "ai_import_vendor_partner_id"
        )

    def _context_seen_by_import_lines(self, record, tax_type, ours=True):
        """The same spy, reporting the WHOLE narrowing context rather than one
        key — because since a review finding the relay carries two facts, and
        "no vendor id" and "no vendor" are different states."""
        seen = []

        def spy(
            self,
            record,
            tree,
            xpath,
            document_type=False,
            tax_type=False,
            qty_factor=1,
        ):
            seen.append(
                {
                    key: self.env.context.get(key)
                    for key in (
                        "ai_import_vendor_partner_id",
                        "ai_import_vendor_unknown",
                    )
                }
            )
            return [], []

        edi = self.env["account.edi.xml.ubl_bis3"]
        if ours:
            edi = edi.with_context(ai_invoice_import=True)
        with patch.object(CoreAccountEdiCommon, "_import_lines", spy):
            edi._import_lines(record, None, "./nothing", tax_type=tax_type)
        self.assertEqual(len(seen), 1, "core's _import_lines was not reached")
        return seen[0]

    # ------------------------------------------------------------------
    # 🔴 A REVIEW FINDING — THE TWO SEAMS, COMPOSED
    # ------------------------------------------------------------------

    def test_a_suppressed_vendor_does_not_re_arm_cores_default_code_step(self):
        """🔴 A REVIEW FINDING. Two correct steps composing into a wrong
        outcome, and nothing tested the PAIR.

        On a document that reaches the create suppression and finds no vendor,
        `account.edi.common._import_partner` returns an EMPTY `res.partner` and
        core's caller does `invoice.partner_id = partner.id` on the next line —
        so `partner_id` is False. `_import_lines` then found no commercial
        partner and set no context key, and
        `product.product._get_product_domain_search_order` fell through to
        core's FULL plan, including `default_code` — which on the legacy core
        IS the vendor's `SellersItemIdentification`
        (`account_edi_xml_ubl_20._get_product_xpaths`).

        So on exactly the bills this module deliberately hands to a human — the
        ones from a vendor it does not recognise — a vendor's own article code
        was matched against OUR internal references, and the line was priced
        from whatever product happened to carry that string. That is the
        cross-namespace collision step 6 exists to prevent, re-armed by step 4.

        It is not a regression against `origin/master` — on master the legacy
        core was unsupported, and before the suppression core would have
        invented the vendor instead. It is an emergent property of two steps
        that were each right on their own, which is why only a test of the pair
        can hold it.

        🔴 WITH NO VENDOR THE ONLY HONEST ANSWER IS "NO PRODUCT", and `barcode`
        is the one exception: a GTIN is a global namespace, so it identifies the
        same article whoever quotes it. The plan is therefore barcode and
        nothing else.

        CONTROL 1: the same document WITH a vendor still gets the supplierinfo
        step, so a narrowing that had simply started refusing everything could
        not pass.
        CONTROL 2: a document this module did NOT produce still gets core's full
        plan, `default_code` and all — the guard from review is not
        widened by this.
        """
        homeless = self.env["account.move"].create({"move_type": "in_invoice"})
        self.assertFalse(
            homeless.partner_id,
            "CONTROL: this is the state the create suppression leaves",
        )

        context = self._context_seen_by_import_lines(homeless, "purchase")
        self.assertFalse(context["ai_import_vendor_partner_id"])
        self.assertTrue(
            context["ai_import_vendor_unknown"],
            "🔴 'no vendor id on the context' and 'there is no vendor' are "
            "different states, and only the second one can narrow the plan",
        )

        plan = (
            self.env["product.product"]
            .with_context(**context)
            ._get_product_domain_search_order(
                default_code=UNMAPPED_CODE, barcode="ZZ-GTIN-1", name="ZZ Thing"
            )
        )
        self.assertNotIn(
            "default_code",
            repr(plan),
            "🔴 a vendor's article code must not be matched against our own "
            "internal references on a bill whose vendor is unknown: %r" % (plan,),
        )
        self.assertNotIn("'name'", repr(plan))
        self.assertIn(
            "barcode",
            repr(plan),
            "a GTIN is a global namespace and is the one key that survives",
        )

        with_vendor = self._context_seen_by_import_lines(
            self.env["account.move"].create(
                {"move_type": "in_invoice", "partner_id": self.vendor.id}
            ),
            "purchase",
        )
        self.assertIn(
            "product_tmpl_id.seller_ids",
            repr(
                self.env["product.product"]
                .with_context(**with_vendor)
                ._get_product_domain_search_order(default_code=UNMAPPED_CODE)
            ),
            "CONTROL: a known vendor still narrows to its own supplierinfo rows",
        )

        not_ours = self._context_seen_by_import_lines(homeless, "purchase", ours=False)
        self.assertIn(
            "default_code",
            repr(
                self.env["product.product"]
                .with_context(**not_ours)
                ._get_product_domain_search_order(default_code=UNMAPPED_CODE)
            ),
            "CONTROL: a document this module did not produce keeps core's plan",
        )

    def test_a_purchase_document_puts_its_vendor_on_the_context(self):
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.vendor.id}
        )

        self.assertEqual(
            self._vendor_seen_by_import_lines(move, "purchase"), self.vendor.id
        )

    def test_somebody_else_s_purchase_document_is_left_to_core(self):
        """🔴 a review finding — RED FIRST.

        `_import_lines` was the one override in this module with no
        `ai_invoice_import` guard, and on the legacy core it has three live
        callers that are nothing to do with this module — including
        `purchase_edi_ubl_bis3`, which is `installed` in the source project's
        database and passes `tax_type='purchase'` hard-coded. Every one of them
        had core's `default_code` / `name` / `name ilike` steps removed, so a
        line on a genuine supplier UBL whose vendor has no supplierinfo row
        silently got no product.

        A registered e-invoice is not a model's reading of a PDF.
        """
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.vendor.id}
        )

        self.assertIsNone(
            self._vendor_seen_by_import_lines(move, "purchase", ours=False),
            "a document this module did not produce keeps core's product plan",
        )
        # CONTROL: the very same document, declared ours, DOES narrow — so the
        # assertion above is about the guard and not about a broken fixture.
        self.assertEqual(
            self._vendor_seen_by_import_lines(move, "purchase"), self.vendor.id
        )

    def test_a_sale_document_puts_nobody_on_the_context(self):
        """🔴 On a sale document `partner_id` is the CUSTOMER. Scoping product
        matching to a customer's `product.supplierinfo` rows would match
        nothing at all — a whole customer invoice imported with no products on
        it, silently. The search-plan half narrows on
        `collected_values['odoo_document_type']`; this is the same
        discriminator, read from the argument core passes."""
        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.vendor.id}
        )

        self.assertIsNone(self._vendor_seen_by_import_lines(move, "sale"))

    def test_the_relay_carries_the_commercial_partner_not_the_contact(self):
        """A bill filed against a child contact must still find the parent's
        supplierinfo rows. The source project's fork passes
        `record.partner_id.id` and does not."""
        contact = self.env["res.partner"].create(
            {"name": "ZZ Accounts Payable", "parent_id": self.vendor.id}
        )
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": contact.id}
        )

        self.assertEqual(
            self._vendor_seen_by_import_lines(move, "purchase"), self.vendor.id
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_the_context_key_changes_nothing_on_this_build(self):
        """🔴 INERTNESS AS A MEASUREMENT, NOT A READING.

        `_import_lines` IS called on this build — by Factur-X and UBL 2.0
        documents — so it really does set the key here. The claim that this
        costs the US deployment nothing rests on `_get_product_domain_search_order`
        having no callers on this core, which is a claim about a tree. This is
        a claim about a run: with the key set, core's own `_retrieve_product`
        still returns the decoy, exactly as it does without it.
        """
        Product = self.env["product.product"]

        self.assertEqual(
            Product.with_context(
                ai_import_vendor_partner_id=self.vendor.id
            )._retrieve_product(default_code=UNMAPPED_CODE),
            self.decoy,
        )
        self.assertEqual(
            Product._retrieve_product(default_code=UNMAPPED_CODE),
            self.decoy,
            "the control — this core's answer is the same either way",
        )

    # ------------------------------------------------------------------
    # 🔴 The discriminating pair: this is the evidence
    # ------------------------------------------------------------------

    def test_an_unmatched_vendor_code_does_not_fall_through_to_our_default_code(self):
        """The whole point of the override, stated as behaviour.

        The vendor quotes a code we have no supplierinfo row for. Core would
        carry on to slot 15 and match our own `product.default_code`, putting an
        unrelated product on the bill at the vendor's price. Narrowed, the line
        imports with its text and NO product — which is the correct answer:
        somebody adds the supplierinfo row once and every later bill matches.

        Delete the override and this test fails with the decoy on the line.

        ✅ GREEN ON BOTH BENCHES SINCE 19.0.3.8.0 (the dual-core design). It was red
        on the legacy bench and deliberately left so: *(measured)* the line
        imported with `product.product(708,)`, the decoy, because that core
        resolves products through `account.edi.common._import_product` ->
        `product.product._retrieve_product`, whose plan
        (`_get_product_domain_search_order`) has no supplierinfo step at all
        and reaches `default_code` at slot 10. The narrowing now lives there
        too — see `models/product_product.py`, including why that seam and not
        the `_import_product` one the plan named.
        """
        move = self._decode(self._extracted(UNMAPPED_CODE))

        self.assertFalse(
            self._product_on_line(move),
            "an unmapped vendor code must match nothing, not guess",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_control_core_alone_would_have_picked_the_decoy(self):
        """The control, over core's REAL plan — not a hand-written one.

        Without this, the test above could pass merely because the decoy was
        unreachable, and would prove nothing. Here the override is disabled so
        core's own unmodified plan runs, and it picks the decoy. That is the
        behaviour being prevented, demonstrated rather than asserted.

        ⚠️ The patch target is `account.edi.xml.ubl_bis3`, not
        `account.edi.ubl`: the chain `account.edi.ubl` -> `ubl_20` -> `ubl_21`
        -> `ubl_bis3` gives each step its own `_name`, hence its own registry
        class, and the decoder that runs is `ubl_bis3`. Patching the base model
        silently patches a class nothing in this flow calls.
        """
        Bis3 = type(self.env["account.edi.xml.ubl_bis3"])
        # Call CORE's function object directly, so the control tracks whatever
        # core's plan actually is rather than a list written out here.
        #
        # ⚠️ `super(Bis3, self)` does NOT work for this and the first attempt
        # used it. Odoo's registry class has every contributing module class
        # among its bases, so the MRO walk starts at those — including this
        # module's — and comes straight back to the narrowed implementation.
        # The guard below is what caught it; without the guard this test would
        # have "passed" by asserting on the narrowed plan's own output.
        core_plan = getattr(CORE_PLAN_OWNER, PLAN_HOOK)
        unnarrowed = (
            lambda self, collected_values: core_plan(  # noqa: E731
                self, collected_values
            )
        )

        with patch.object(Bis3, PLAN_HOOK, unnarrowed):
            # Guard the guard: prove the narrowing really is off before drawing
            # any conclusion from what follows.
            self.assertIn(
                "_import_retrieve_product_from_default_code",
                self._plan_names(
                    {
                        "odoo_document_type": "purchase",
                        "customer_values": {"customer": self.vendor},
                    }
                ),
                "the patch did not take effect, so this is not a control at all",
            )
            move = self._decode(self._extracted(UNMAPPED_CODE))

        self.assertEqual(
            self._product_on_line(move),
            self.decoy,
            "control failed: core's own plan did not pick the decoy, so the "
            "test above was not exercising the collision it claims to",
        )

    # ------------------------------------------------------------------
    # The narrowing must not break the case that SHOULD match
    # ------------------------------------------------------------------

    def test_a_mapped_vendor_code_still_resolves_through_supplierinfo(self):
        """Core's step does the matching; narrowing must not cost us that.

        ✅ GREEN ON BOTH BENCHES SINCE 19.0.3.8.0 — the other half of the same
        gap (the dual-core design). *(measured)* the line used to come back with NO
        product at all on the legacy bench: that core's product plan has no
        supplierinfo step, so a vendor code that is correctly mapped simply did
        not match. 🔴 On THIS build the supplierinfo domain is OURS (copied
        from the other core's step), which is the one place in this feature
        where the two builds do not share a body — `models/product_product.py`
        says why they cannot, and this test plus
        `test_supplierinfo_is_scoped_to_the_same_row_not_merely_the_product`
        are the tripwires that keep the copy honest.
        """
        move = self._decode(self._extracted(MAPPED_CODE))

        self.assertEqual(self._product_on_line(move), self.mapped_product)

    def test_supplierinfo_is_scoped_to_the_same_row_not_merely_the_product(self):
        """🔴 The reason this module no longer ships its own domain.

        A product may carry several supplierinfo rows. The vendor and the code
        must match on the SAME row — a domain spelling them as two independent
        dotted leaves ANDs them on the product instead, so a product with rows
        (VendorA, "111") and (VendorB, "XXX") matches VendorA quoting "XXX".
        That is the cross-vendor collision this feature exists to prevent, and a
        previous revision of this module reintroduced it.

        Core gets it right with `('product_tmpl_id.seller_ids', 'any', [...])`.
        This test pins the behaviour so a future local 'optimisation' of the
        domain cannot quietly undo it.
        """
        other_vendor = self.env["res.partner"].create(
            {"name": "ZZ Other Vendor Oy", "is_company": True, "country_id": self.fi.id}
        )
        shared = self.env["product.product"].create({"name": "ZZ Shared Product"})
        # Our vendor knows it as "111"; the OTHER vendor knows it as the code
        # our vendor is about to quote.
        self.env["product.supplierinfo"].create(
            {
                "partner_id": self.vendor.id,
                "product_tmpl_id": shared.product_tmpl_id.id,
                "product_code": "ZZ-SHARED-111",
            }
        )
        self.env["product.supplierinfo"].create(
            {
                "partner_id": other_vendor.id,
                "product_tmpl_id": shared.product_tmpl_id.id,
                "product_code": "ZZ-SHARED-XXX",
            }
        )

        # Our vendor quotes the OTHER vendor's code for that product.
        move = self._decode(self._extracted("ZZ-SHARED-XXX"))

        self.assertNotEqual(
            self._product_on_line(move),
            shared,
            "vendor and code must match on the same supplierinfo row",
        )
        self.assertFalse(self._product_on_line(move))
