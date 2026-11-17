# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountEdiUbl(models.AbstractModel):
    """Hooks of the search-plan UBL import pipeline (newer 19.0 builds).

    Newer builds route ``account.edi.xml.ubl_bis3`` through
    ``account.edi.ubl._ubl_import_invoice``, which resolves partners and
    products through search plans instead of ``account.edi.common``. Older
    builds use the hooks in ``account_edi_common.py``.
    """

    _inherit = "account.edi.ubl"

    def _import_ubl_retrieve_partner_bank(self, collected_values):
        """Never record a received credit note's bank account as one of ours.

        Core treats ``in_refund`` like ``out_invoice`` and looks for the IBAN
        under our own partner, but a supplier's credit note carries the
        supplier's account. On ``in_refund`` the account is recorded on the
        vendor and ``partner_bank_id`` is left to ``_compute_partner_bank_id``.

        The guards apply to every purchase document. They matter twice here:
        core's customer search plan matches on bank account numbers, so a stray
        IBAN on the company partner would be read back as a vendor match.
        """
        invoice = collected_values["invoice"]
        if invoice.move_type not in ("in_invoice", "in_refund", "in_receipt"):
            return super()._import_ubl_retrieve_partner_bank(collected_values)

        # The key is part of the pipeline's contract, so every path out of
        # this method must leave it set, including the early returns.
        bank_values = collected_values["partner_bank_values"]
        resolved = (collected_values.get("customer_values") or {}).get("customer")
        if not resolved:
            resolved = invoice.partner_id
        if not resolved:
            bank_values["partner_banks"] = self.env["res.partner.bank"]
            return
        # Scoped to the document's company; env.company may differ in a queue job.
        if self.env["res.partner"]._ai_impossible_vendor_reason(
            resolved, company=collected_values["company"]
        ):
            bank_values["partner_banks"] = self.env["res.partner.bank"]
            return

        if invoice.move_type != "in_refund":
            return super()._import_ubl_retrieve_partner_bank(collected_values)

        partner_bank_values = collected_values["partner_bank_values"]
        # Empty: this document yields no account for ``partner_bank_id``.
        partner_bank_values["partner_banks"] = self.env["res.partner.bank"]

        # The partner is only staged in ``to_write`` at this point, so read it
        # from ``collected_values`` like core does; the record field is a fallback.
        partner = (collected_values.get("customer_values") or {}).get("customer")
        if not partner:
            partner = invoice.partner_id
        if not partner:
            return

        company = collected_values["company"]
        logs = collected_values["logs"]
        for account_number in partner_bank_values["account_numbers"]:
            try:
                self.env["res.partner.bank"]._find_or_create_bank_account(
                    account_number=account_number,
                    partner=partner,
                    company=company,
                )
            except UserError as e:
                logs.append(_("The bank account couldn't be fetched: %s", str(e)))

    def _import_ubl_retrieve_customer(self, collected_values):
        """Refuse a vendor that is one of our own companies or users.

        Core's plan (VAT, endpoint, bank account, email, phone, name) can land
        on the company partner. This runs after the whole plan and before the
        create and bank steps, so one override covers every step. Both
        ``customer_values['customer']`` and ``to_write['partner_id']`` are
        cleared so the rest of the pipeline agrees on the outcome.
        """
        result = super()._import_ubl_retrieve_customer(collected_values)
        if not self.env.context.get("ai_invoice_import"):
            return result
        # Purchase only: on a sale document a sister company may well be the
        # customer in a multi-company database.
        if collected_values.get("odoo_document_type") != "purchase":
            return result

        customer_values = collected_values.get("customer_values") or {}
        customer = customer_values.get("customer")
        if not customer:
            return result
        Partner = self.env["res.partner"]
        # Another company's partner is allowed: that is the intercompany case.
        reason = Partner._ai_impossible_vendor_reason(
            customer, company=collected_values["company"]
        )
        if not reason:
            return result

        _logger.warning(
            "AI invoice import: the seller on this document resolved to %s "
            "(refused: %s); the bill was left without a vendor.",
            customer.display_name,
            reason,
        )
        customer_values["customer"] = Partner
        collected_values.get("to_write", {}).pop("partner_id", None)
        collected_values["logs"].append(
            Partner._ai_refused_vendor_message(customer, reason)
        )
        return result

    def _import_ubl_retrieve_customer_search_plan(self, collected_values):
        """Add our own steps to core's plan: business id, phone, email/web domain.

        They go below core's exact-match steps (an exact key beats a normalised
        one) and immediately above the name step, which is an exact
        case-insensitive comparison and the weakest key on an invoice. Each
        step resolves to a single partner or declines, because core runs a
        step's domain with ``limit=1`` and would silently pick one of several.
        """
        plan = super()._import_ubl_retrieve_customer_search_plan(collected_values)
        ResPartner = self.env["res.partner"]
        steps = [
            ResPartner._import_retrieve_customer_from_ai_business_id,
            ResPartner._import_retrieve_customer_from_ai_phone,
            ResPartner._import_retrieve_customer_from_ai_domain,
        ]

        # Insert before the name step rather than at a fixed index, so a core
        # change that adds another exact-match step above `name` keeps these
        # below it instead of silently floating to the wrong place.
        name_step = ResPartner._import_retrieve_customer_from_name
        plan = list(plan)
        for index, entry in enumerate(plan):
            if getattr(entry, "__name__", None) == name_step.__name__:
                return plan[:index] + steps + plan[index:]

        # No name step (a core we have not seen): appending is the conservative
        # placement — they can only run when everything else has already failed.
        _logger.info(
            "AI import: core's customer search plan has no name step; "
            "appending the business-id, phone and domain fallbacks at the end "
            "instead."
        )
        return plan + steps

    def _import_ubl_create_missing_customer(self, collected_values):
        """Never create a vendor, or write a VAT onto one, in our own decodes.

        Core creates a partner when nothing matched and a VAT is printed, and
        also when the matched partner's VAT differs from the document's. It
        also fills an empty VAT on a matched partner; tax ids and bank
        accounts are never set automatically, as changing them is the classic
        invoice-fraud vector. Genuine structured e-invoices keep core's behaviour.
        """
        if not self.env.context.get("ai_invoice_import"):
            return super()._import_ubl_create_missing_customer(collected_values)
        if collected_values.get("odoo_document_type") != "purchase":
            return super()._import_ubl_create_missing_customer(collected_values)

        if (collected_values.get("customer_values") or {}).get("customer"):
            return
        name = (collected_values.get("customer_values") or {}).get("name")
        if name:
            collected_values["logs"].append(
                _(
                    "No existing vendor matched '%s'. The bill was left without "
                    "a vendor rather than creating one — see the note in the "
                    "chatter for what was read off the document and which "
                    "existing vendors come closest.",
                    name,
                )
            )

    def _import_ubl_retrieve_products_search_plan(self, collected_values):
        """Stop a vendor's article code from matching our internal reference.

        For a purchase with a known vendor, the plan is narrowed to core's
        vendor-scoped supplierinfo step and the barcode step. Core would
        otherwise fall through to ``default_code`` and match the vendor's code
        against our own references. Finding nothing is the correct answer; a
        supplierinfo row fixes it for every later bill.

        Do not replace core's supplierinfo step with a local domain: dotted
        leaves over ``seller_ids`` are not scoped to the same row and would
        reintroduce cross-vendor matches.
        """
        plan = super()._import_ubl_retrieve_products_search_plan(collected_values)

        if collected_values.get("odoo_document_type") != "purchase":
            return plan

        # Resolved earlier by `_import_ubl_retrieve_customer`. Without a vendor
        # core's supplierinfo step would decline, so the plan is left as is.
        vendor = (collected_values.get("customer_values") or {}).get("customer")
        if not vendor:
            return plan

        ProductProduct = self.env["product.product"]
        return [
            ProductProduct._import_retrieve_product_from_supplierinfo,
            ProductProduct._import_retrieve_product_from_barcode,
        ]
