# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import models
from odoo.osv import expression


class ProductProduct(models.Model):
    """Narrow core's product lookup to the known vendor on AI-decoded bills.

    The supplierinfo domain must stay row-scoped: vendor and code have to
    match on the same ``product.supplierinfo`` row, or a product with rows
    (VendorA, "111") and (VendorB, "XXX") would match VendorA quoting "XXX".
    """

    _inherit = "product.product"

    def _ai_vendor_supplierinfo_domain(self, vendor_partner_id, product_codes):
        """Row-scoped supplierinfo domain, or None.

        Kept as one ``any`` leaf so it cannot be "simplified" into two dotted
        leaves, which would lose the same-row scoping.
        """
        codes = [code for code in product_codes if code]
        if not (vendor_partner_id and codes):
            return None
        return [
            (
                "product_tmpl_id.seller_ids",
                "any",
                [("partner_id", "=", vendor_partner_id), ("product_code", "in", codes)],
            )
        ]

    def _import_retrieve_product_from_ai_supplierinfo(self, product_values):
        """Search-plan step: the vendor's own article codes, on 18.0 builds
        from April 2026 on.

        Those builds resolve products through a search plan but, unlike 19.0,
        have no vendor-scoped supplierinfo step, so this is our equivalent of
        19.0's ``_import_retrieve_product_from_supplierinfo``. The vendor is the
        one core put on the line for its own predictive step; the codes are
        the seller's item id and the GTIN. Declines (``{}``) without both.
        """
        predictive = product_values.get("invoice_predictive") or {}
        vendor = (predictive.get("partner") or self.env["res.partner"]).commercial_partner_id
        domain = self._ai_vendor_supplierinfo_domain(
            vendor.id,
            (product_values.get("default_code"), product_values.get("barcode")),
        )
        if domain is None:
            return {}
        return {"criteria": [{"domain": domain}]}

    def _retrieve_product(
        self, name=None, default_code=None, barcode=None, company=None, extra_domain=None
    ):
        """Narrow core's lookup when the vendor of an AI-decoded bill is known.

        Core matches ``default_code`` — here the vendor's
        ``SellersItemIdentification`` — against our own ``default_code``, then
        falls back to the name. With a known vendor the lookup becomes
        supplierinfo (scoped to that vendor, both codes) then barcode; ``name``
        matches are dropped. With an unknown vendor only barcode remains, since
        a GTIN is global. Finding nothing is the correct answer; a supplierinfo
        row fixes it for every later bill.

        The context keys are set by ``account.edi.common._import_invoice_lines``
        for AI-decoded purchase documents only.
        """
        vendor_partner_id = self.env.context.get("ai_import_vendor_partner_id")
        vendor_unknown = self.env.context.get("ai_import_vendor_unknown")
        if not vendor_partner_id and not vendor_unknown:
            return super()._retrieve_product(
                name=name,
                default_code=default_code,
                barcode=barcode,
                company=company,
                extra_domain=extra_domain,
            )

        domains = []
        supplierinfo = self._ai_vendor_supplierinfo_domain(
            vendor_partner_id, (default_code, barcode)
        )
        if supplierinfo is not None:
            domains.append(supplierinfo)
        if barcode:
            domains.append([("barcode", "=", barcode)])
        if not domains:
            return self.env["product.product"]

        # Same company scoping as core: the company's own products first, then
        # shared ones.
        company = company or self.env.company
        for company_domain in (
            [
                *self.env["res.partner"]._check_company_domain(company),
                ("company_id", "!=", False),
            ],
            [("company_id", "=", False)],
        ):
            products = self.env["product.product"].search(
                expression.AND(
                    [expression.OR(domains), company_domain, extra_domain or []]
                )
            )
            for domain in domains:
                if matched := products.filtered_domain(domain):
                    return matched[0]
        return self.env["product.product"]
