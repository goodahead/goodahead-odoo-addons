# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Product matching narrowed for purchase documents with a known vendor.

When the vendor is known, product matching is narrowed to supplierinfo and
barcode, dropping core's `default_code` and `name` steps. On Odoo 18.0 builds
before April 2026 this is `product.product._retrieve_product` (see
`models/product_product.py`), fed by the vendor context
`account.edi.common._import_invoice_lines` sets. On later 18.0 builds it is
`account.edi.common._import_retrieve_products_search_plan` with this module's
own supplierinfo step; on 19.0, `_import_ubl_retrieve_products_search_plan`.

The discriminating case is the fall-through: a vendor code with no supplierinfo
row that equals the internal reference of an unrelated product of ours. The two
behavioural tests are deliberately not profile-gated.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

# Core's classes, imported directly so a control can call core's
# implementation while this module is installed.
from odoo.addons.account_edi_ubl_cii.models.account_edi_common import (
    AccountEdiCommon as CoreAccountEdiCommon,
)
from odoo.addons.account_edi_ubl_cii.models.account_edi_ubl import (
    AccountEdiUBL as CoreAccountEdiUBL,
)
from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
    PROFILE_SEARCH_PLAN_18,
)

from .profiles import skip_method_unless_profile

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
    # 18.0 from April 2026 on: the plan is built by `account.edi.common`
    # ------------------------------------------------------------------

    def _plan_names_18(self, collected_values, ours=True):
        values = {"company": self.company, **collected_values}
        edi = self.env["account.edi.xml.ubl_bis3"]
        if ours:
            edi = edi.with_context(ai_invoice_import=True)
        return [
            getattr(step, "__name__", "")
            for step in edi._import_retrieve_products_search_plan(values)
        ]

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN_18)
    def test_18_plan_is_untouched_on_somebody_else_s_document(self):
        """A genuine supplier e-invoice keeps core's product lookup on 18.0."""
        names = self._plan_names_18(
            {
                "odoo_document_type": "purchase",
                "customer_values": {"customer": self.vendor},
            },
            ours=False,
        )
        self.assertIn("_import_retrieve_product_from_default_code", names)

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN_18)
    def test_18_plan_is_our_supplierinfo_and_barcode_only(self):
        names = self._plan_names_18(
            {
                "odoo_document_type": "purchase",
                "customer_values": {"customer": self.vendor},
            }
        )
        self.assertEqual(
            names,
            [
                "_import_retrieve_product_from_ai_supplierinfo",
                "_import_retrieve_product_from_barcode",
            ],
            "18.0 has no supplierinfo step of its own, so ours stands in for "
            "it, followed by barcode",
        )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN_18)
    def test_18_plan_is_untouched_without_a_vendor(self):
        names = self._plan_names_18(
            {"odoo_document_type": "purchase", "customer_values": {}}
        )
        self.assertIn("_import_retrieve_product_from_default_code", names)

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN_18)
    def test_18_plan_is_untouched_on_a_sale_document(self):
        names = self._plan_names_18(
            {
                "odoo_document_type": "sale",
                "customer_values": {"customer": self.vendor},
            }
        )
        self.assertIn("_import_retrieve_product_from_default_code", names)

    def _step(self, partner, default_code=None, barcode=None):
        return self.env["product.product"]._import_retrieve_product_from_ai_supplierinfo(
            {
                "default_code": default_code,
                "barcode": barcode,
                "invoice_predictive": {"partner": partner},
            }
        )

    def test_our_supplierinfo_step_is_row_scoped_to_the_line_s_vendor(self):
        """The step reads the vendor core put on the line and scopes vendor and
        code to one supplierinfo row. Portable: it is our own method."""
        contact = self.env["res.partner"].create(
            {"name": "ZZ Product Vendor AP", "parent_id": self.vendor.id}
        )
        self.assertEqual(
            self._step(contact, default_code=MAPPED_CODE),
            {
                "criteria": [
                    {
                        "domain": self.env[
                            "product.product"
                        ]._ai_vendor_supplierinfo_domain(
                            self.vendor.id, (MAPPED_CODE, None)
                        )
                    }
                ]
            },
            "a child contact is scoped to its commercial partner's rows",
        )

    def test_our_supplierinfo_step_declines_without_a_vendor_or_a_code(self):
        self.assertEqual(self._step(self.env["res.partner"], MAPPED_CODE), {})
        self.assertEqual(self._step(None, MAPPED_CODE), {})
        self.assertEqual(self._step(self.vendor), {})

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN_18)
    def test_18_control_core_alone_would_have_picked_the_decoy(self):
        """With the override disabled, 18.0 core's real plan picks the decoy."""
        Bis3 = type(self.env["account.edi.xml.ubl_bis3"])
        unnarrowed = (
            lambda self, collected_values: (  # noqa: E731
                CoreAccountEdiCommon._import_retrieve_products_search_plan(
                    self, collected_values
                )
            )
        )

        with patch.object(Bis3, "_import_retrieve_products_search_plan", unnarrowed):
            self.assertIn(
                "_import_retrieve_product_from_default_code",
                self._plan_names_18(
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
            "discriminating test was not exercising the collision on this build",
        )

    # ------------------------------------------------------------------
    # `_retrieve_product`, called directly
    # ------------------------------------------------------------------
    #
    # The supplierinfo domain must scope vendor and code to the same row with
    # `any`; two independent dotted leaves would let a product with rows
    # (VendorA, "111") and (VendorB, "XXX") match VendorA quoting "XXX".

    def _retrieve(self, context=None, **vals):
        Product = self.env["product.product"]
        if context:
            Product = Product.with_context(**context)
        return Product._retrieve_product(**vals)

    def test_the_supplierinfo_domain_is_one_row_scoped_leaf(self):
        domain = self.env["product.product"]._ai_vendor_supplierinfo_domain(
            self.vendor.id, (MAPPED_CODE, None)
        )
        self.assertEqual(len(domain), 1, domain)
        path, operator, subdomain = domain[0]
        self.assertEqual((path, operator), ("product_tmpl_id.seller_ids", "any"))
        self.assertIn(("partner_id", "=", self.vendor.id), subdomain)
        self.assertIn(("product_code", "in", [MAPPED_CODE]), subdomain)

    def test_a_known_vendor_never_matches_our_default_code_or_name(self):
        vendor_ctx = {"ai_import_vendor_partner_id": self.vendor.id}

        self.assertFalse(
            self._retrieve(vendor_ctx, default_code=UNMAPPED_CODE),
            "the vendor's code must not be matched against our default_code",
        )
        self.assertFalse(
            self._retrieve(vendor_ctx, name="ZZ Decoy Product"),
            "core's name match is dropped once the vendor is known",
        )
        self.assertEqual(
            self._retrieve(vendor_ctx, default_code=MAPPED_CODE),
            self.mapped_product,
            "the vendor's own supplierinfo row still resolves",
        )

    def test_a_barcode_still_matches_with_a_known_vendor(self):
        self.decoy.barcode = "ZZ-GTIN-1"
        vendor_ctx = {"ai_import_vendor_partner_id": self.vendor.id}

        self.assertEqual(
            self._retrieve(vendor_ctx, default_code=UNMAPPED_CODE, barcode="ZZ-GTIN-1"),
            self.decoy,
            "a GTIN is global, so it is the one key kept",
        )

    def test_without_the_context_core_decides(self):
        """CONTROL: without our context key core's lookup runs, and it picks
        the decoy by `default_code`."""
        self.assertEqual(self._retrieve(default_code=UNMAPPED_CODE), self.decoy)

    def test_a_code_the_vendor_does_not_quote_matches_nothing(self):
        """With no codes and no barcode, nothing matches (no empty `in []`)."""
        vendor_ctx = {"ai_import_vendor_partner_id": self.vendor.id}

        self.assertFalse(self._retrieve(vendor_ctx, name="ZZ Mapped Product"))

    # ------------------------------------------------------------------
    # The relay, and the two things it must not do
    # ------------------------------------------------------------------

    def _vendor_seen_by_import_lines(self, record, ours=True):
        """Run `_import_invoice_lines` with core's body replaced by a spy and
        return the vendor id this module put on the context.

        Core's class is patched (not the registry class) so `super()` in our
        override lands on the spy. `ours=False` drops the `ai_invoice_import`
        key.
        """
        return self._context_seen_by_import_lines(record, ours=ours).get(
            "ai_import_vendor_partner_id"
        )

    def _context_seen_by_import_lines(self, record, ours=True):
        """The same spy, returning both narrowing context keys ("no vendor id"
        and "no vendor" are different states)."""
        seen = []

        def spy(self, invoice, tree, xpath, qty_factor):
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
        with patch.object(CoreAccountEdiCommon, "_import_invoice_lines", spy):
            edi._import_invoice_lines(record, None, "./nothing", 1)
        self.assertEqual(len(seen), 1, "core's _import_invoice_lines was not reached")
        return seen[0]

    # ------------------------------------------------------------------
    # Vendor suppression and product narrowing, composed
    # ------------------------------------------------------------------

    def test_a_suppressed_vendor_does_not_re_arm_cores_default_code_step(self):
        """On a bill with no vendor, only the barcode can match.

        When vendor creation is suppressed, `partner_id` stays empty. Without
        the `ai_import_vendor_unknown` key, core would match the vendor's
        article code against our internal references. A GTIN is a global
        namespace, so barcode is the one key kept.

        Controls: a known vendor still resolves its supplierinfo rows, and a
        document this module did not produce keeps core's lookup.
        """
        homeless = self.env["account.move"].create({"move_type": "in_invoice"})
        self.assertFalse(
            homeless.partner_id,
            "CONTROL: this is the state the create suppression leaves",
        )

        context = self._context_seen_by_import_lines(homeless)
        self.assertFalse(context["ai_import_vendor_partner_id"])
        self.assertTrue(
            context["ai_import_vendor_unknown"],
            "'no vendor id on the context' and 'there is no vendor' are "
            "different states, and only the second one can narrow the lookup",
        )
        self.assertFalse(
            self._retrieve(context, default_code=UNMAPPED_CODE, name="ZZ Decoy Product"),
            "a vendor's article code must not be matched against our own "
            "internal references on a bill whose vendor is unknown",
        )
        self.decoy.barcode = "ZZ-GTIN-2"
        self.assertEqual(
            self._retrieve(context, default_code=UNMAPPED_CODE, barcode="ZZ-GTIN-2"),
            self.decoy,
            "a GTIN is a global namespace and is the one key that survives",
        )

        with_vendor = self._context_seen_by_import_lines(
            self.env["account.move"].create(
                {"move_type": "in_invoice", "partner_id": self.vendor.id}
            )
        )
        self.assertEqual(
            self._retrieve(with_vendor, default_code=MAPPED_CODE),
            self.mapped_product,
            "CONTROL: a known vendor still resolves its own supplierinfo rows",
        )

        not_ours = self._context_seen_by_import_lines(homeless, ours=False)
        self.assertEqual(
            self._retrieve(not_ours, default_code=UNMAPPED_CODE),
            self.decoy,
            "CONTROL: a document this module did not produce keeps core's lookup",
        )

    def test_a_purchase_document_puts_its_vendor_on_the_context(self):
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.vendor.id}
        )

        self.assertEqual(self._vendor_seen_by_import_lines(move), self.vendor.id)

    def test_somebody_else_s_purchase_document_is_left_to_core(self):
        """Without the `ai_invoice_import` key, `_import_invoice_lines` sets no
        vendor context, so genuine supplier e-invoices keep core's lookup.
        """
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.vendor.id}
        )

        self.assertIsNone(
            self._vendor_seen_by_import_lines(move, ours=False),
            "a document this module did not produce keeps core's product lookup",
        )
        # Control: the same document, declared ours, does narrow.
        self.assertEqual(self._vendor_seen_by_import_lines(move), self.vendor.id)

    def test_a_sale_document_puts_nobody_on_the_context(self):
        """On a sale document `partner_id` is the customer, so no vendor scope
        is applied."""
        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.vendor.id}
        )

        self.assertIsNone(self._vendor_seen_by_import_lines(move))

    def test_the_relay_carries_the_commercial_partner_not_the_contact(self):
        """A bill filed against a child contact uses the commercial partner's
        supplierinfo rows."""
        contact = self.env["res.partner"].create(
            {"name": "ZZ Accounts Payable", "parent_id": self.vendor.id}
        )
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": contact.id}
        )

        self.assertEqual(self._vendor_seen_by_import_lines(move), self.vendor.id)

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN, PROFILE_SEARCH_PLAN_18)
    def test_the_context_key_changes_nothing_on_this_build(self):
        """On the search-plan builds the context key has no effect on core's
        `_retrieve_product`.
        """
        self.assertEqual(
            self._retrieve(default_code=UNMAPPED_CODE), self.decoy,
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

        The supplierinfo domain uses `('product_tmpl_id.seller_ids', 'any', [...])`; two
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
