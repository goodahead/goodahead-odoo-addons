# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import clean_context

from odoo.addons.base.models.res_bank import sanitize_account_number

_logger = logging.getLogger(__name__)


class AccountEdiCommon(models.AbstractModel):
    """Partner and bank hooks of the older UBL import pipeline.

    Newer 19.0 builds route Peppol BIS 3 through ``account.edi.ubl`` (see
    ``account_edi_ubl.py``), so for those documents these overrides are
    dormant. They stay because older 19.0 builds still use this path, and
    Factur-X and UBL 2.0 documents still reach it on newer builds.
    """

    _inherit = "account.edi.common"

    @api.model
    def _ai_core_peppol_domain(self, kwargs):
        """Core's Peppol-identity domain, rebuilt from core's own kwargs.

        This duplicates a few lines of core; a test with a vendor reachable
        only by its Peppol pair keeps the two in sync.
        """
        eas = kwargs.get("peppol_eas")
        endpoint = kwargs.get("peppol_endpoint")
        if eas and endpoint:
            return [("peppol_eas", "=", eas), ("peppol_endpoint", "=", endpoint)]
        return False

    def _import_partner(self, company_id, name, phone, email, vat, **kwargs):
        """Never create a vendor, and never match an impossible one, in our decodes.

        The older core creates a partner here when nothing matches and both
        ``name`` and ``vat`` are set. Its match step has no such condition and
        can resolve the seller to the company's own partner (e.g. by phone), so
        the retrieval runs first on every AI decode. On a legitimate match we
        still call ``super()`` so core can fill an empty address.

        Nulling ``vat`` or ``name`` to disarm the create would also disarm the
        match, which is why this is a wrapper. The early return must be a
        ``(recordset, logs)`` tuple with an empty ``res.partner``, because the
        caller assigns ``partner.id`` straight away.
        """
        if not self.env.context.get("ai_invoice_import"):
            return super()._import_partner(
                company_id, name, phone, email, vat, **kwargs
            )

        Partner = self.env["res.partner"]
        partner = Partner.with_company(company_id)._retrieve_partner(
            name=name,
            phone=phone,
            email=email,
            vat=vat,
            domain=self._ai_core_peppol_domain(kwargs),
        )

        # A document's vendor is never one of our own companies or users. This
        # is scoped to AI decodes because in a multi-company database a genuine
        # structured document from a sister company is legitimate. Use the
        # ``company_id`` argument: the queue job's env.company may differ.
        reason = Partner._ai_impossible_vendor_reason(partner, company=company_id)
        if reason:
            _logger.warning(
                "AI invoice import: the seller on this document resolved to "
                "%s (refused: %s); the bill was left without a vendor.",
                partner.display_name,
                reason,
            )
            return Partner, [Partner._ai_refused_vendor_message(partner, reason)]

        # Core's own create guard: where it cannot fire, core's behaviour is right.
        if not (name and vat):
            return super()._import_partner(
                company_id, name, phone, email, vat, **kwargs
            )

        if partner:
            return super()._import_partner(
                company_id, name, phone, email, vat, **kwargs
            )

        # Replace core's "could not retrieve partner" log, which would hide
        # that a create was suppressed. Same wording as the newer-core path.
        _logger.info(
            "AI invoice import: no existing vendor matched %r; core's create "
            "was suppressed and the bill left without a vendor.",
            name,
        )
        return self.env["res.partner"], [
            _(
                "No existing vendor matched '%s'. The bill was left without a "
                "vendor rather than creating one — see the note in the chatter "
                "for what was read off the document and which existing vendors "
                "come closest.",
                name,
            )
        ]

    def _import_partner_bank(self, invoice, bank_details):
        """Never record a received credit note's bank account as one of ours.

        On ``in_refund`` the older core takes the company partner and would
        attach the supplier's IBAN to it (or un-archive one). We record it on
        the vendor instead and let ``_compute_partner_bank_id`` pick it up.
        On other purchase documents core already uses the vendor, so only the
        guards are added. Newer cores refuse company accounts in
        ``_find_or_create_bank_account``; the older one has no such protection.
        """
        if invoice.move_type not in ("in_invoice", "in_refund", "in_receipt"):
            return super()._import_partner_bank(invoice, bank_details)

        partner = invoice.partner_id.commercial_partner_id
        if not partner:
            return
        # The sender resolved to our own company or user (mis-resolved vendor,
        # or seller and buyer swapped): do not attach its IBAN there.
        if self.env["res.partner"]._ai_impossible_vendor_reason(
            partner, company=invoice.company_id
        ):
            return

        if invoice.move_type != "in_refund":
            return super()._import_partner_bank(invoice, bank_details)

        # Same reason as core: a default from the context must not leak into
        # a bank account created during import.
        Bank = self.env["res.partner.bank"].with_env(
            self.env(context=clean_context(self.env.context))
        )
        numbers = {sanitize_account_number(number) for number in bank_details}
        numbers.discard(False)
        for number in numbers:
            # Archived ones count: the vendor already has it, archived or not.
            known = Bank.with_context(active_test=False).search_count(
                [
                    ("sanitized_acc_number", "=", number),
                    ("partner_id", "child_of", partner.id),
                ],
                limit=1,
            )
            if known:
                continue
            try:
                Bank.create({"acc_number": number, "partner_id": partner.id})
            except UserError as e:
                invoice._message_log(
                    body=_("The bank account couldn't be fetched: %s", str(e))
                )

    def _import_lines(
        self,
        record,
        tree,
        xpath,
        document_type=False,
        tax_type=False,
        qty_factor=1,
    ):
        """Put the vendor on the context so product matching can be narrowed to it.

        Core's ``_retrieve_product`` never sees the document, so the context is
        the only channel to ``product.product._get_product_domain_search_order``.
        Only for AI purchase decodes: ``record`` may be a purchase or sale
        order, hence the test on ``tax_type`` rather than on the record.
        """
        vendor = record.partner_id.commercial_partner_id
        if self.env.context.get("ai_invoice_import") and tax_type == "purchase":
            # "No vendor id" and "known to have no vendor" are different states;
            # only the second narrows the plan, so a bill whose vendor creation
            # was suppressed does not fall back to matching our default_code.
            self = self.with_context(
                ai_import_vendor_partner_id=vendor.id if vendor else False,
                ai_import_vendor_unknown=not vendor,
            )
        return super()._import_lines(
            record,
            tree,
            xpath,
            document_type=document_type,
            tax_type=tax_type,
            qty_factor=qty_factor,
        )
