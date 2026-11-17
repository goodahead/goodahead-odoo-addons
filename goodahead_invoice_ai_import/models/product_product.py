# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import models
from odoo.fields import Domain


class ProductProduct(models.Model):
    """Narrow the legacy core's product plan to the known vendor.

    Twin of ``account.edi.ubl._import_ubl_retrieve_products_search_plan`` for
    the search-plan core; that docstring holds the rationale. On the
    search-plan core ``_get_product_domain_search_order`` has no callers, so
    this override is naturally inert there.

    The supplierinfo domain is copied from core and must stay row-scoped:
    vendor and code have to match on the same ``product.supplierinfo`` row, or
    a product with rows (VendorA, "111") and (VendorB, "XXX") would match
    VendorA quoting "XXX".
    """

    _inherit = "product.product"

    def _ai_vendor_supplierinfo_domain(self, vendor_partner_id, product_codes):
        """Core's ``any``-scoped supplierinfo domain, or None.

        Kept as one expression so it cannot be "simplified" into two dotted
        leaves, which would lose the same-row scoping.
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

        On this core ``default_code`` is the vendor's
        ``SellersItemIdentification``, matched against our own
        ``default_code``. With a known vendor the plan becomes supplierinfo
        (scoped to that vendor, both codes) then barcode; ``name`` matches are
        dropped. With an unknown vendor only barcode remains, since a GTIN is
        global. Finding nothing is the correct answer.

        The context keys are set by ``account.edi.common._import_lines`` for
        AI-decoded purchase documents only.
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
