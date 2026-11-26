# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import logging

from odoo import api, fields, models

# The bar code's format belongs to the payment rather than to this document —
# the proforma prints the same code — so it lives in lib/fi_bank_barcode.py.
from ..lib.fi_address import delivery_name, same_place
from ..lib.fi_bank_barcode import (
    BARCODE_LENGTH,
    BARCODE_MAX_AMOUNT,
    FI_IBAN_LENGTH,
    build_barcode,
)

_logger = logging.getLogger(__name__)

# A settled invoice has no bar code by design, so its absence is not worth a
# line in the log every time the document is rendered - unlike a missing bank
# account or a malformed reference, which somebody needs to see.
NOTHING_TO_PAY = "nothing left to pay"


class AccountMove(models.Model):
    _inherit = "account.move"

    fi_bank_barcode = fields.Char(
        "Bank Bar Code",
        compute="_compute_fi_bank_barcode",
        help="Finnish bank bar code (virtuaaliviivakoodi) encoding the account, "
        "the amount left to pay, the reference and the due date.",
    )

    # ------------------------------------------------------------------
    # Report selection
    # ------------------------------------------------------------------

    def _get_name_invoice_report(self):
        self.ensure_one()
        if self._fi_uses_finnish_invoice_report():
            return "goodahead_finnish_invoice.report_invoice_document"
        return super()._get_name_invoice_report()

    def _fi_uses_finnish_invoice_report(self):
        """Customer documents of a Finnish company. Vendor bills keep the stock
        report: this layout exists to be paid from, and we do not issue those."""
        self.ensure_one()
        return (
            self.move_type in ("out_invoice", "out_refund", "out_receipt")
            and self.company_id.account_fiscal_country_id.code == "FI"
        )

    # ------------------------------------------------------------------
    # Bank bar code
    # ------------------------------------------------------------------

    @api.depends(
        "move_type",
        "state",
        "amount_total",
        "amount_residual",
        "currency_id",
        "payment_reference",
        "invoice_date_due",
        "partner_bank_id.sanitized_acc_number",
        "company_id",
    )
    def _compute_fi_bank_barcode(self):
        for move in self:
            move.fi_bank_barcode = move._fi_build_bank_barcode()

    def _fi_payment_bank(self):
        """The account the customer pays into.

        Falls back to the company's own account when the invoice carries none:
        older invoices were posted without one, and ``_post`` clears the field
        outright when the account is untrusted and an automated flow did the
        posting. The customer still has to be told where to pay. A credit note
        is not paid by the customer, so it gets no account and no payment slip.
        """
        self.ensure_one()
        if self.move_type != "out_invoice":
            return self.env["res.partner.bank"]
        return self.partner_bank_id or self.company_id.partner_id.bank_ids[:1]

    def _fi_amount_due(self):
        """What is still outstanding, in the invoice's own currency. A draft has
        no residual yet. Taken as an absolute value: a credit note's residual is
        stored negative, and the document states what is owed, not its sign."""
        self.ensure_one()
        amount = self.amount_residual if self.state == "posted" else self.amount_total
        return abs(self.currency_id.round(amount))

    def _fi_payment_date(self):
        """The day the invoice was last paid against, or False while nothing
        has been paid.

        Customers who need a receipt are sent this same document, so an invoice
        that has been paid has to say so on its face rather than only showing
        0,00 due. A part-paid one has to say it too, for the opposite reason:
        its total and its amount due differ, and nothing else on the page
        accounts for the difference.

        The date is the last of the reconciled counterpart items, because that
        is the one that settled the invoice as far as it is settled. Read off
        the journal items rather than ``_get_reconciled_payments()``: that
        helper maps the counterparts to ``origin_payment_id`` and so drops every
        item that is not a payment. An invoice settled half by a payment and
        half by a credit note would still return the payment, and the document
        would claim it was settled on the earlier of the two dates.
        """
        self.ensure_one()
        if self.payment_state not in ("paid", "in_payment", "partial"):
            return False
        dates = self._get_reconciled_amls().mapped("date")
        return max(dates) if dates else self.invoice_date

    def _fi_amount_paid(self):
        """What has been paid against the invoice so far, in its own currency.

        Taken as the total less what is left, not from ``amount_paid``: that
        field belongs to ``account_payment`` and sums ``payment.transaction``
        records only, so it reads 0,00 on everything settled any other way —
        a bank payment, a credit note, a write-off. A receipt whose paid figure
        is 0,00 while its due figure is not would be worse than no figure.
        """
        self.ensure_one()
        return abs(self.currency_id.round(self.amount_total - self.amount_residual))

    def _fi_barcode_currency(self):
        """Which currency ``_fi_barcode_amount`` below is expressed in.

        The two have to be read together - rounding a figure with the wrong
        currency's precision is how a small amount silently becomes zero - so
        the branch is stated once, here, and both callers use it.
        """
        self.ensure_one()
        if self.currency_id.name == "EUR":
            return self.currency_id
        return self.company_currency_id

    def _fi_barcode_amount(self):
        """The same figure in euro, which is the only currency the bar code can
        carry. An invoice in another currency falls back to the company-currency
        equivalent: the bar code pays into a euro account, so that is the amount
        that actually leaves the customer's bank."""
        self.ensure_one()
        if self.currency_id.name == "EUR":
            return self._fi_amount_due()
        signed = (
            self.amount_residual_signed
            if self.state == "posted"
            else self.amount_total_signed
        )
        return abs(self.company_currency_id.round(signed))

    def _fi_bank_barcode_error(self):
        """Return why this move cannot carry a bar code, or None when it can.

        The bar code is defined for euro payments into a Finnish account, so
        anything else is a plain "no bar code" rather than an error worth
        interrupting a print for.
        """
        self.ensure_one()

        if not self._fi_uses_finnish_invoice_report():
            return "not a Finnish customer document"
        if self.move_type != "out_invoice":
            return "only an invoice asks the customer to pay"

        iban = self._fi_payment_bank().sanitized_acc_number or ""
        if not iban:
            return "no recipient bank account"
        # The reason ends up in the log, so it never carries the account number.
        if not iban.startswith("FI") or len(iban) != FI_IBAN_LENGTH:
            return "recipient account is not a Finnish IBAN"

        if "EUR" not in (self.currency_id.name, self.company_currency_id.name):
            return "neither the invoice nor the company is in EUR"

        amount = self._fi_barcode_amount()
        if amount < 0:
            return "amount is negative"
        # A settled invoice goes out as the customer's receipt. A bar code on it
        # would still scan, and their banking app would offer to pay 0,00 into
        # our account - so the receipt carries no code at all, and the legal
        # line below takes the width back.
        if self._fi_barcode_currency().is_zero(amount):
            return NOTHING_TO_PAY
        if amount > BARCODE_MAX_AMOUNT:
            return "amount exceeds the six digits the bar code allows"

        reference = self._fi_barcode_reference()
        if not reference:
            return "no payment reference"
        if reference.startswith("RF"):
            if not reference[2:4].isdigit() or not reference[4:].isdigit():
                return "malformed RF reference %s" % reference
            if len(reference[4:]) > 21:
                return "RF reference %s is too long" % reference
        else:
            if not reference.isdigit():
                return "reference %s is not a Finnish reference number" % reference
            if len(reference) > 20:
                return "reference %s is too long" % reference

        return None

    def _fi_barcode_reference(self):
        self.ensure_one()
        return (self.payment_reference or "").replace(" ", "").upper()

    # ------------------------------------------------------------------
    # Addresses
    # ------------------------------------------------------------------

    def _fi_delivery_partner(self):
        """The delivery address, when it is worth a block of its own.

        Empty when the goods go where the bill goes: one address under two
        headings says less than one address under a heading naming both.
        """
        self.ensure_one()
        if same_place(self.partner_id, self.partner_shipping_id):
            return self.env["res.partner"]
        return self.partner_shipping_id

    def _fi_delivery_name(self):
        self.ensure_one()
        return delivery_name(self.partner_shipping_id, self.partner_id)

    def _fi_build_bank_barcode(self):
        self.ensure_one()
        error = self._fi_bank_barcode_error()
        # A settled invoice is the one case where having no bar code is the
        # intent, so it leaves quietly.
        if error == NOTHING_TO_PAY:
            return False
        if error:
            if self.move_type == "out_invoice" and self.state == "posted":
                _logger.info(
                    "No bank bar code for %s: %s", self.name or self.id, error
                )
            return False

        barcode = build_barcode(
            self._fi_payment_bank().sanitized_acc_number,
            self._fi_barcode_amount(),
            self._fi_barcode_reference(),
            self.invoice_date_due,
        )
        if not barcode:
            _logger.warning(
                "Bank bar code for %s did not come out %s digits — dropping it",
                self.name or self.id,
                BARCODE_LENGTH,
            )
            return False
        return barcode

    # ------------------------------------------------------------------
    # Report helpers
    # ------------------------------------------------------------------

    def _fi_vat_breakdown(self):
        """Net / VAT / gross per VAT rate, for the ALV-erittely table.

        Read off ``tax_totals`` rather than summed from the lines. Odoo rounds
        tax once per tax group over the whole base, so adding up the lines'
        own gross amounts drifts from the invoice total by a few cents — and a
        VAT breakdown that does not tie out to the total is worse than none.
        """
        self.ensure_one()
        rows = []
        for subtotal in (self.tax_totals or {}).get("subtotals") or []:
            for group in subtotal.get("tax_groups") or []:
                net = group["base_amount_currency"]
                vat = group["tax_amount_currency"]
                rows.append(
                    {
                        "name": group["group_name"],
                        "net": net,
                        "vat": vat,
                        "gross": net + vat,
                    }
                )
        return rows
