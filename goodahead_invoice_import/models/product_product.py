# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import models
from odoo.fields import Domain


class ProductProduct(models.Model):
    """The legacy core's product plan, narrowed to the vendor (the dual-core design).

    🔴 THE TWIN OF `account.edi.ubl._import_ubl_retrieve_products_search_plan`,
    AND THE SAME ARGUMENT. On both builds the fix for a vendor's article code
    matching OUR internal reference is to decide WHICH OF CORE'S STEPS RUN, not
    to add a match of our own. Read that method's docstring first — the
    problem, the two steps that survive, and why finding nothing is the correct
    answer are all stated there and are not repeated here.

    🔴 WHY THIS SEAM AND NOT THE ONE the dual-core design STEP 6 NAMES — the fourth instance
    of the plan's own standing pattern (§4.3(a), §4.3(b), §6). Step 6 says
    *"restore `_import_lines` / `_import_product`"*, because those are the two
    methods the source project's fork overrides. `_import_product` is the wrong
    one of the two. On the legacy core it is four words long —
    ``return self.env['product.product']._retrieve_product(**product_vals)``
    (`account_edi_ubl_cii/models/account_edi_common.py:910-911` at `ca1f0aea`)
    — and everything that matters happens one level down, in `_retrieve_product`
    (`account/models/product.py:285`), which asks
    `_get_product_domain_search_order` WHICH DOMAINS TO TRY and then runs them
    itself, twice, under its own company scoping. Overriding `_import_product`
    means re-implementing that company scoping by hand and throwing core's loop
    away; overriding the plan method means core keeps running its own loop and
    only the list of domains changes. **The plan clusters by intent; the chain
    cares where a rule sits.**

    🔴 AND IT IS NATURALLY INERT ON THE OTHER BUILD, which the other spelling
    is NOT. *(measured)* `_get_product_domain_search_order` exists on the
    search-plan core too (`account/models/product.py:538` at `189df353`) and
    has ZERO callers there — `_retrieve_product` was rewritten to drive
    `_import_retrieve_product` with a search plan instead. A tree-wide grep over
    that core finds one other override of it, in `sale_edi_ubl`, equally dead.
    So this override is live on one build and inert on the other by CORE's
    doing, exactly like the legacy name-slot adapter in `res_partner.py`, and
    with no profile flag to get wrong. The `_import_product` spelling would
    NOT have been inert: on the search-plan build that method is still live for
    Factur-X and UBL 2.0, so injecting a vendor there would have switched on
    core's supplierinfo step for US-deployment documents that do not use it today
    — a US-deployment behaviour change, in a step whose whole brief is not to make
    one.

    ⚠️ THE DOMAIN IS CORE'S, DELIBERATELY. It is copied from the search-plan
    core's `product.product._import_retrieve_product_from_supplierinfo`
    (`account/models/product.py:317` at `189df353`) and must stay a copy,
    because the legacy core has no supplierinfo step at all to call. The
    row-scoping is the part that must never be rewritten by hand: a product may
    carry several `product.supplierinfo` rows, and the vendor and the code have
    to match on the SAME row. Two independent dotted leaves
    (``seller_ids.partner_id`` and ``seller_ids.product_code``) are ANDed on the
    PRODUCT over an x2many, so a product with rows (VendorA, "111") and
    (VendorB, "XXX") matches VendorA quoting "XXX" — the cross-vendor collision
    this whole feature exists to prevent. A previous revision of this module
    shipped exactly that bug (see the `19.0.1.3.0` block in `__manifest__.py`).
    `tests/test_product_matching.
    test_supplierinfo_is_scoped_to_the_same_row_not_merely_the_product` is the
    tripwire, and it is ungated so it runs on both builds.

    ⚠️ The source project's fork does NOT carry the two-leaf bug — it searches
    `product.supplierinfo` directly, which is row-scoped by construction. It
    carries three smaller ones instead, none of them copied here: it takes
    ``record.partner_id.id`` rather than the commercial partner, it narrows on
    any document with a partner rather than on purchases only, and it replaces
    core's `_retrieve_product` wholesale, losing core's company scoping.
    """

    _inherit = "product.product"

    def _ai_vendor_supplierinfo_domain(self, vendor_partner_id, product_codes):
        """Core's `any`-scoped supplierinfo domain, or None.

        Factored out so the row-scoping is one expression with one test on it,
        rather than a line inside a longer method that a later edit can
        "simplify" into two leaves without anything noticing.
        """
        codes = [code for code in product_codes if code]
        if not (vendor_partner_id and codes):
            return None
        return Domain(
            "product_tmpl_id.seller_ids",
            "any",
            [("partner_id", "=", vendor_partner_id), ("product_code", "in", codes)],
        )

    def _get_product_domain_search_order(self, **vals):
        """Narrow core's legacy product plan when the vendor is known.

        Core's own order here is barcode (5), ``default_code`` (10), ``name``
        exact (15), ``name ilike`` (20) — and on this core ``default_code`` is
        the VENDOR's code (``SellersItemIdentification``, see
        `account_edi_xml_ubl_20._get_product_xpaths`), matched against OUR
        `product.default_code`. Two namespaces that merely share a string space.

        When the vendor is known the plan becomes, and becomes only:

            1. supplierinfo   scoped to this vendor, on one row, both codes
            2. barcode        a GTIN/EAN is global, so no vendor scope

        which is the same pair, in the same order, that the search-plan half
        leaves standing. Finding nothing is the correct answer: somebody adds
        the `product.supplierinfo` row once and every later bill matches.

        🔴 AND WHEN THE VENDOR IS UNKNOWN THE PLAN IS BARCODE AND NOTHING ELSE
        (found in review). The create suppression leaves
        `invoice.partner_id` empty on exactly the documents it hands to a human,
        so `_import_lines` sets `ai_import_vendor_unknown` instead of an id —
        and this method used to read that as "not our document" and return core's
        full plan, `default_code` included. On this core `default_code` IS the
        vendor's `SellersItemIdentification`, so a vendor nobody recognised got
        their article codes matched against our internal references and the
        lines priced from whatever happened to carry the string. With no vendor
        the only honest answer is NO PRODUCT; `barcode` survives because a GTIN
        is a global namespace and identifies the same article whoever quotes it.
        The supplierinfo step cannot be built without a partner id and
        `_ai_vendor_supplierinfo_domain` already returns None for one, so the
        narrowing needs no second branch — which is why the two states are
        distinguished at the RELAY and not here.

        ⚠️ BOTH CODES GO INTO THE SUPPLIERINFO SEARCH, because core does that
        on the other build: its step reads ``sellers_item_id``,
        ``standard_item_id`` and ``buyers_item_id``. This core's line xpaths
        offer two of those three — ``default_code`` IS
        ``SellersItemIdentification`` and ``barcode`` IS
        ``StandardItemIdentification`` — and there is no ``buyers_item_id`` to
        read. So the mapping is as close as the two cores allow, and a vendor
        whose supplierinfo row records a GTIN still matches.

        ⚠️ ``name`` IS DROPPED, and that is the point rather than an oversight.
        Core reaches it at 15 and 20, and a line description is the weakest
        key an invoice carries; on a purchase document with a known vendor it
        is exactly the guess the narrowing exists to refuse.

        ⚠️ The context key is set by `account.edi.common._import_lines`, on an
        AI decode and nowhere else — see the `ai_invoice_import` guard there.
        ⚠️ THE SENTENCE THAT STOOD HERE SAID ONLY "nowhere else", which answers
        *can a non-EDI call trip this* when the question is *WHICH EDI imports
        trip this* (found in review). It read like a scoping guarantee
        and was not one; it is now, at the relay.

        ⚠️ A NEAR MISS, CHECKED RATHER THAN ASSUMED. Core's `sale_edi_ubl` also
        overrides this method and `bisect.insort`s a ``default_code`` domain at
        12 and a ``barcode`` one at 14 — which, above ours in the MRO, would
        put the fall-through straight back into the narrowed plan. *(measured)*
        It cannot: the only producer of ``variant_default_code`` /
        ``variant_barcode`` is `sale_edi_xml_ubl_bis3._get_product_xpaths`, a
        SALE order import, and a sale document never reaches the branch above
        because `_import_lines` sets the key for ``tax_type == 'purchase'``
        only. The two overrides can never both be active on one call.
        """
        vendor_partner_id = self.env.context.get("ai_import_vendor_partner_id")
        vendor_unknown = self.env.context.get("ai_import_vendor_unknown")
        if not vendor_partner_id and not vendor_unknown:
            return super()._get_product_domain_search_order(**vals)

        sorted_domains = []
        supplierinfo = self._ai_vendor_supplierinfo_domain(
            vendor_partner_id, (vals.get("default_code"), vals.get("barcode"))
        )
        if supplierinfo is not None:
            sorted_domains.append((5, supplierinfo))
        if barcode := vals.get("barcode"):
            sorted_domains.append((10, Domain("barcode", "=", barcode)))
        return sorted_domains
