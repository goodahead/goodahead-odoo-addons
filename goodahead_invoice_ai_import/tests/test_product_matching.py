# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Product plan narrowing for purchase documents with a known vendor.

When the vendor is known, product matching is narrowed to supplierinfo and
barcode, dropping core's `default_code`, `name` and predictive steps. On the
search-plan build this narrows `_import_ubl_retrieve_products_search_plan`; on
the legacy build it narrows `product.product._get_product_domain_search_order`,
where the module also supplies the supplierinfo domain (see
`models/product_product.py`).

The discriminating case is the fall-through: a vendor code with no supplierinfo
row that equals the internal reference of an unrelated product of ours. The two
behavioural tests are deliberately not profile-gated.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
)

from .profiles import skip_method_unless_profile

# Core's classes, imported directly so a control can call core's
# implementation while this module is installed.
from odoo.addons.account_edi_ubl_cii.models.account_edi_ubl import (
    AccountEdiUBL as CoreAccountEdiUBL,
)
from odoo.addons.account_edi_ubl_cii.models.account_edi_common import (
    AccountEdiCommon as CoreAccountEdiCommon,
)

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

        # The VAT number has a valid Finnish check digit, since `base_vat` may
        # be installed.
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "ZZ Product Vendor Oy",
                "is_company": True,
                "company_registry": "ZZ-PROD-0001",
                "country_id": cls.fi.id,
                "vat": "FI09990071",
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

        # The decoy: not linked to this vendor, but its internal reference
        # equals a code the vendor uses. Core reaches it once supplierinfo
        # declines.
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
                "vat": "FI09990071",
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
        return [
            getattr(step, "__name__", "")
            for step in self.env[
                "account.edi.xml.ubl_bis3"
            ]._import_ubl_retrieve_products_search_plan(collected_values)
        ]

    # ------------------------------------------------------------------
    # The plan shape (a proxy — the behavioural tests are below)
    # ------------------------------------------------------------------

    # The product search plan exists only on the search-plan build, so these
    # are gated per method.
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
    # `product.product._get_product_domain_search_order` is declared by core
    # on both builds with the same shape, and these tests call it directly,
    # so they run on both. They are the only direct tests of
    # `_ai_vendor_supplierinfo_domain`.
    #
    # The supplierinfo domain must scope vendor and code to the same row with
    # `any`; two independent dotted leaves would let a product with rows
    # (VendorA, "111") and (VendorB, "XXX") match VendorA quoting "XXX".

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
        # `Domain.__repr__` is the list form, so this reads the real domain.
        supplierinfo = repr(plan[0][1])
        self.assertIn("product_tmpl_id.seller_ids", supplierinfo)
        self.assertIn("any", supplierinfo)
        self.assertIn("partner_id", supplierinfo)
        self.assertIn("product_code", supplierinfo)
        self.assertIn("barcode", repr(plan[1][1]))
        # The key assertion: core's `default_code` and `name` steps are gone.
        self.assertNotIn("default_code", repr(plan))
        self.assertNotIn("'name'", repr(plan))

    def test_the_legacy_plan_is_untouched_without_a_vendor(self):
        """Without a vendor, core's full plan is returned."""
        plan = self._legacy_plan(default_code=UNMAPPED_CODE, name="ZZ Thing")

        self.assertIn("default_code", repr(plan))

    def test_a_code_the_vendor_does_not_quote_produces_no_supplierinfo_step(self):
        """With no codes, no supplierinfo step is emitted (rather than one with
        an empty `in []`)."""
        plan = self._legacy_plan(vendor=self.vendor, name="ZZ Thing")

        self.assertEqual(plan, [])

    # ------------------------------------------------------------------
    # The relay, and the two things it must not do
    # ------------------------------------------------------------------

    def _vendor_seen_by_import_lines(self, record, tax_type, ours=True):
        """Run `_import_lines` with core's body replaced by a spy and return
        the vendor id this module put on the context.

        Core's class is patched (not the registry class) so `super()` in our
        override lands on the spy. `ours=False` drops the `ai_invoice_import`
        key.
        """
        return self._context_seen_by_import_lines(record, tax_type, ours=ours).get(
            "ai_import_vendor_partner_id"
        )

    def _context_seen_by_import_lines(self, record, tax_type, ours=True):
        """The same spy, returning both narrowing context keys ("no vendor id"
        and "no vendor" are different states)."""
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
    # Vendor suppression and product narrowing, composed
    # ------------------------------------------------------------------

    def test_a_suppressed_vendor_does_not_re_arm_cores_default_code_step(self):
        """On a bill with no vendor, the plan is barcode only.

        When vendor creation is suppressed, `partner_id` stays empty. Without
        the `ai_import_vendor_unknown` key, core's full plan would match the
        vendor's article code against our internal references. A GTIN is a
        global namespace, so barcode is the one step kept.

        Controls: a known vendor still gets its supplierinfo step, and a
        document this module did not produce keeps core's full plan.
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
            "'no vendor id on the context' and 'there is no vendor' are "
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
            "a vendor's article code must not be matched against our own "
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
        """Without the `ai_invoice_import` key, `_import_lines` sets no vendor
        context, so genuine supplier e-invoices (e.g. from
        `purchase_edi_ubl_bis3`) keep core's product plan.
        """
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.vendor.id}
        )

        self.assertIsNone(
            self._vendor_seen_by_import_lines(move, "purchase", ours=False),
            "a document this module did not produce keeps core's product plan",
        )
        # Control: the same document, declared ours, does narrow.
        self.assertEqual(
            self._vendor_seen_by_import_lines(move, "purchase"), self.vendor.id
        )

    def test_a_sale_document_puts_nobody_on_the_context(self):
        """On a sale document `partner_id` is the customer, so no vendor scope
        is applied."""
        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.vendor.id}
        )

        self.assertIsNone(self._vendor_seen_by_import_lines(move, "sale"))

    def test_the_relay_carries_the_commercial_partner_not_the_contact(self):
        """A bill filed against a child contact uses the commercial partner's
        supplierinfo rows."""
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
        """On the search-plan build the context key has no effect on core's
        `_retrieve_product`.
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
    # The discriminating pair
    # ------------------------------------------------------------------

    def test_an_unmatched_vendor_code_does_not_fall_through_to_our_default_code(self):
        """A vendor code without a supplierinfo row imports with no product.

        Core would fall through to our own `default_code` and put an unrelated
        product on the bill. Adding the supplierinfo row once fixes all later
        bills.
        """
        move = self._decode(self._extracted(UNMAPPED_CODE))

        self.assertFalse(
            self._product_on_line(move),
            "an unmapped vendor code must match nothing, not guess",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_control_core_alone_would_have_picked_the_decoy(self):
        """With the override disabled, core's real plan picks the decoy.

        The patch target is `account.edi.xml.ubl_bis3`, the registry class the
        decoder actually uses.
        """
        Bis3 = type(self.env["account.edi.xml.ubl_bis3"])
        # Call core's function directly; `super(Bis3, self)` would walk back
        # into this module's narrowed implementation.
        unnarrowed = (
            lambda self, collected_values: (  # noqa: E731
                CoreAccountEdiUBL._import_ubl_retrieve_products_search_plan(
                    self, collected_values
                )
            )
        )

        with patch.object(
            Bis3, "_import_ubl_retrieve_products_search_plan", unnarrowed
        ):
            # Check the narrowing is really off before drawing conclusions.
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
        """A mapped vendor code still resolves through supplierinfo.

        On the legacy build the supplierinfo domain is this module's own (see
        `models/product_product.py`).
        """
        move = self._decode(self._extracted(MAPPED_CODE))

        self.assertEqual(self._product_on_line(move), self.mapped_product)

    def test_supplierinfo_is_scoped_to_the_same_row_not_merely_the_product(self):
        """Vendor and code must match on the same supplierinfo row.

        Core uses `('product_tmpl_id.seller_ids', 'any', [...])`; two
        independent dotted leaves would match a product where another vendor
        uses the quoted code.
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
