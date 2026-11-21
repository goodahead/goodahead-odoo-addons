# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import clean_context

from odoo.addons.base.models.res_bank import sanitize_account_number

_logger = logging.getLogger(__name__)


class AccountEdiCommon(models.AbstractModel):
    """Partner, bank and product hooks of Odoo 18.0's UBL import.

    On 18.0 builds before April 2026 every UBL and Factur-X document is
    imported through this model's legacy methods. Later 18.0 builds import UBL
    through the search plan (``account_edi_ubl.py``) and only take the product
    plan from here (``_import_retrieve_products_search_plan``).
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

        Core creates a partner here when nothing matches and both ``name`` and
        ``vat`` are set. Its match step has no such condition and can resolve
        the seller to the company's own partner (e.g. by phone), so the
        retrieval runs first on every AI decode. On a legitimate match core's
        own result is returned.

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

        # Say on the bill that a create was suppressed.
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

        On ``in_refund`` core takes the company partner and would attach the
        supplier's IBAN to it (or un-archive one). We record it on the vendor
        instead and let ``_compute_partner_bank_id`` pick it up. On other
        purchase documents core uses the vendor; see ``_ai_import_vendor_bank``
        for the one case where its lookup goes wrong.

        The two guards (no IBAN on our own company or users, a received credit
        note's IBAN on the vendor) protect every purchase e-invoice, AI or
        not; on 18.0 every UBL, Factur-X and Peppol bill passes through here.
        The stray-copy rule for bills (``_ai_import_vendor_bank``) is for AI
        decodes only; genuine bills otherwise keep core's rule.
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

        # Same reason as core: a default from the context must not leak into
        # a bank account created during import.
        Bank = self.env["res.partner.bank"].with_env(
            self.env(context=clean_context(self.env.context))
        )

        if invoice.move_type != "in_refund":
            if not self.env.context.get("ai_invoice_import"):
                return super()._import_partner_bank(invoice, bank_details)
            return self._ai_import_vendor_bank(invoice, partner, bank_details, Bank)

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

    def _ai_import_vendor_bank(self, invoice, partner, bank_details, Bank):
        """Core's bill rule, minus stray copies on our own company partner.

        Core looks the number up across every partner and, when the row it
        finds belongs to someone other than the vendor, neither uses it nor
        creates the vendor's own. A copy an earlier import left on our own
        company partner would therefore strip the account off every later bill
        from that vendor. Rows on our own company partner or its contacts are
        not owners, so they are ignored. Rows on anybody else (another vendor,
        an employee, a portal user) keep core's refusal: the account is neither
        used nor created. Otherwise core's rule: use the vendor's row
        (re-activated if archived), else create one. AI decodes only.
        """
        numbers = [
            number
            for number in dict.fromkeys(map(sanitize_account_number, bank_details))
            if number
        ]
        if not numbers:
            return
        Partner = self.env["res.partner"]
        rows = Bank.with_context(active_test=False).search(
            [
                ("company_id", "in", [False, invoice.company_id.id]),
                ("sanitized_acc_number", "in", numbers),
            ]
        )
        ours = set(Partner._ai_company_partner_tree_ids(invoice.company_id))
        rows = rows.filtered(lambda bank: bank.partner_id.id not in ours)
        to_create = []
        for number in numbers:
            holders = rows.filtered(lambda bank: bank.sanitized_acc_number == number)
            own = holders.filtered(
                lambda bank: bank.partner_id.commercial_partner_id == partner
            )[:1]
            if own:
                if not own.active:
                    own.active = True
                invoice.partner_bank_id = own
                return
            if not holders:
                to_create.append({"acc_number": number, "partner_id": partner.id})
        if to_create:
            invoice.partner_bank_id = Bank.create(to_create)[0]

    def _import_retrieve_products_search_plan(self, collected_values):
        """Stop a vendor's article code from matching our internal reference,
        on 18.0 builds from April 2026 on.

        Those builds import UBL through the search-plan pipeline (see
        ``account_edi_ubl.py``), but build the product plan here and have no
        supplierinfo step. For a purchase with a known vendor the plan becomes
        our vendor-scoped supplierinfo step, then barcode: the same narrowing
        as ``account.edi.ubl._import_ubl_retrieve_products_search_plan`` on
        19.0. Core would otherwise match the vendor's code against our own
        ``default_code`` and fall through to a name match. Finding nothing is
        the correct answer; a supplierinfo row fixes it for every later bill.

        AI imports only, as on older 18.0 builds: a genuine supplier e-invoice
        keeps core's product lookup. (On 19.0 the narrowing applies to every
        purchase document.)

        Older 18.0 builds do not have this method; there, the narrowing is done
        by ``_import_invoice_lines`` and ``product.product._retrieve_product``.
        """
        plan = super()._import_retrieve_products_search_plan(collected_values)

        if not self.env.context.get("ai_invoice_import"):
            return plan
        if collected_values.get("odoo_document_type") != "purchase":
            return plan

        # Resolved earlier by `_import_ubl_retrieve_customer`. Without a vendor
        # our supplierinfo step would decline, so the plan is left as is.
        vendor = (collected_values.get("customer_values") or {}).get("customer")
        if not vendor:
            return plan

        ProductProduct = self.env["product.product"]
        return [
            ProductProduct._import_retrieve_product_from_ai_supplierinfo,
            ProductProduct._import_retrieve_product_from_barcode,
        ]

    def _import_invoice_lines(self, invoice, tree, xpath, qty_factor):
        """Put the vendor on the context so product matching can be narrowed to it.

        Core's ``_retrieve_product`` never sees the document, so the context is
        the only channel to ``product.product._retrieve_product``. Only for AI
        purchase decodes.
        """
        vendor = invoice.partner_id.commercial_partner_id
        if (
            self.env.context.get("ai_invoice_import")
            and invoice.journal_id.type == "purchase"
        ):
            # "No vendor id" and "known to have no vendor" are different states;
            # only the second narrows the search, so a bill whose vendor creation
            # was suppressed does not fall back to matching our default_code.
            self = self.with_context(
                ai_import_vendor_partner_id=vendor.id if vendor else False,
                ai_import_vendor_unknown=not vendor,
            )
        return super()._import_invoice_lines(invoice, tree, xpath, qty_factor)
