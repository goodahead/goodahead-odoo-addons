# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The Finvoice values of an invoice or credit note.

``_apix_finvoice_values()`` turns the invoice into the plain values dict the
base writer (``goodahead_einvoice_base.lib.finvoice.write``) reads. Every
sender detail comes from the invoice's own company, never the active one
; the buyer is the invoice contact with its commercial customer as the
fallback per field; every line carries its real quantity, the Rec 20 code and
readable name of its unit, and the base quantity of a line priced per several
units. A unit without a code never blocks: the line goes with its
name and the unit is listed in ``units_without_code``.

Amounts are positive for credit notes too: the writer negates them.
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

from odoo import models
from odoo.tools import html2plaintext

from odoo.addons.goodahead_einvoice_base.lib.finvoice import values as finvoice_values
from odoo.addons.goodahead_einvoice_base.lib.finvoice import gtin, vat, writer
from odoo.addons.goodahead_einvoice_base.lib.finvoice import references as finvoice_references
from odoo.addons.goodahead_einvoice_base.lib.finvoice import rows as finvoice_rows

SENDER_INTERMEDIATOR = "APIX"
#: Invoice lines that go as text rows. Version-specific: 19.0 adds
#: sub-sections (``line_subsection``); the 18.0 / 16.0 ports have only
#: ``line_section`` and ``line_note`` and drop it from this tuple.
TEXT_ROW_TYPES = ("line_section", "line_note")
#: The order references the invoice values carry (AC-09 - AC-11, AC-27).
REFERENCE_KEYS = ("order_number", "seller_reference", "buyer_reference")
#: Finvoice delivery name, street, post code and town: at least 2 characters.
DELIVERY_MIN_LENGTH = 2


class AccountMove(models.Model):
    _inherit = "account.move"

    def _apix_finvoice_values(self, attempt_id=None, timestamp=None):
        """The contract §6.2 values dict of this invoice, plus
        ``units_without_code`` (readable names, AC-49) which the writer
        ignores. ``attempt_id`` makes the message identifier unique per
        attempt (``<move id>-<attempt id>``)."""
        self.ensure_one()
        move = self.sudo()
        sender = self.env["res.partner"]._apix_sender_values(move.company_id, move.partner_bank_id)
        vat_info = move._apix_vat_info()
        lines, units_without_code = move._apix_finvoice_lines(vat_info)
        invoice = move._apix_finvoice_invoice()
        references = move._apix_finvoice_references()
        invoice.update({key: references[key] for key in REFERENCE_KEYS})
        invoice["totals"]["rows"] = sum(line["untaxed"] for line in lines
                                        if line.get("kind", "product") == "product")
        values = {
            "message": {
                # Without an attempt (developer check): attempt 0.
                "identifier": f"{move.id}-{attempt_id or 0}",
                "timestamp": timestamp or datetime.datetime.now(datetime.UTC),
                "sender_edicode": sender["edicode"],
                "sender_intermediator": SENDER_INTERMEDIATOR,
            },
            "receiver": move._apix_finvoice_receiver(),
            "seller": move._apix_finvoice_seller(sender),
            "buyer": move._apix_finvoice_buyer(),
            "invoice": invoice,
            "lines": lines,
            "taxes": move._apix_finvoice_taxes(vat_info),
            "epi": move._apix_finvoice_epi(sender),
            "units_without_code": units_without_code,
        }
        delivery = move._apix_finvoice_delivery()
        if delivery["state"] == "sent":
            values["delivery"] = delivery["values"]
        return values

    # ------------------------------------------------------------------
    # Parties
    # ------------------------------------------------------------------

    def _apix_finvoice_receiver(self):
        recipient = self._apix_recipient_values()
        return {
            "edicode": recipient["edicode"],
            "operator_identifier": recipient["operator_identifier"],
        }

    def _apix_finvoice_seller(self, sender):
        company = self.company_id
        return {
            "name": sender["name"],
            "business_id": company.company_registry or "",
            "vat": company.vat or "",
            "street": sender["street"],
            "zip": sender["zip"],
            "city": sender["city"],
            "country_code": sender["country_code"],
            "edicode": sender["edicode"],
            "iban": sender["iban"],
            "bic": sender["bic"],
        }

    def _apix_finvoice_buyer(self):
        """The buying organisation is the commercial customer; the address
        and identifiers come from the invoice contact, field by field, with
        the commercial customer as fallback; a separate invoice contact is
        named as the contact person."""
        recipient = self._apix_recipient_values()
        contact = self.partner_id
        commercial = contact.commercial_partner_id
        return {
            "name": commercial.name or recipient["name"],
            "business_id": recipient["business_id"],
            "vat": recipient["vat"],
            "street": recipient["street"],
            "zip": recipient["zip"],
            "city": recipient["city"],
            "country_code": recipient["country_code"],
            "contact_name": contact.name if contact != commercial else None,
            "email": recipient["email"] or None,
        }

    # ------------------------------------------------------------------
    # Invoice details, lines and VAT
    # ------------------------------------------------------------------

    def _apix_finvoice_invoice(self):
        credit_note = self.move_type == "out_refund"
        rounding = self._apix_finvoice_rounding()
        untaxed = self.amount_untaxed - rounding
        note = html2plaintext(self.narration or "").strip() if self.narration else ""
        invoice_type = "credit_note" if credit_note else "invoice"
        return {
            "type": invoice_type,
            # AC-20: the international invoice type next to the Finvoice one.
            "type_code_un": writer.INVOICE_TYPES_UN[invoice_type],
            "number": self.name,
            "date": _iso(self.invoice_date),
            "due_date": _iso(self.invoice_date_due),
            "currency": self.currency_id.name,
            # Only a credit note that reverses an invoice names it (AC-44,
            # AC-60); whether that invoice was ever sent does not matter.
            "original_number": (credit_note and self.reversed_entry_id.name) or None,
            "payment_reference": self.payment_reference or None,
            "note": note or None,
            "totals": {
                "untaxed": untaxed,
                "tax": self.amount_tax,
                "total": untaxed + self.amount_tax,
                "rounding": rounding,
            },
        }

    def _apix_finvoice_rounding(self):
        """The cash rounding added as its own line, in the invoice's sense."""
        lines = self.line_ids.filtered(lambda line: line.display_type == "rounding")
        return sum(lines.mapped("amount_currency")) * self.direction_sign

    def _apix_finvoice_product_lines(self):
        return self.invoice_line_ids.filtered(lambda line: line.display_type == "product")

    def _apix_vat_info(self):
        """``{tax id: tax._einvoice_vat()}`` of every tax on the invoice's
        lines, read once per invoice (finvoice content ADR-0003), cut down
        to what the message can carry (``vat.sendable_vat``; review F3) with
        ``left_out`` naming anything dropped."""
        taxes = self._apix_finvoice_product_lines().tax_ids.flatten_taxes_hierarchy()
        info = {}
        for tax in taxes:
            values = tax._einvoice_vat()
            info[tax.id] = values | vat.sendable_vat(
                values["reason_lines"], values["exemption_code"], values["category"])
        return info

    @staticmethod
    def _apix_line_vat_taxes(line, vat_info):
        """The line's taxes as ``vat.resolve_line`` reads them."""
        return [
            {
                "tax_id": tax.id,
                "name": tax.name,
                "rate": tax.amount if tax.amount_type == "percent" else 0.0,
                "category": (vat_info.get(tax.id) or {}).get("category"),
                "percent": tax.amount_type == "percent",
                "left_out": (vat_info.get(tax.id) or {}).get("left_out") or [],
            }
            for tax in line.tax_ids.flatten_taxes_hierarchy()
        ]

    def _apix_finvoice_lines(self, vat_info=None):
        """``(lines, units_without_code)``: the invoice's lines in their order
        - product rows, and its notes and section headings as text rows
        - with a stated row discount and a valid trade
        item number."""
        vat_info = self._apix_vat_info() if vat_info is None else vat_info
        rows = []
        without_code = []
        for line in self.invoice_line_ids.sorted(lambda line: (line.sequence, line.id)):
            if line.display_type in TEXT_ROW_TYPES:
                texts = finvoice_rows.text_row_texts(line.name)
                if texts:
                    rows.append({"kind": "text", "texts": texts})
                continue
            if line.display_type != "product":
                continue
            quantity, code, unit_name, base_quantity = _line_unit(line)
            if line.product_uom_id and not code and unit_name not in without_code:
                without_code.append(unit_name)
            product = line.product_id
            name = product.name if product else line.name
            discount = self._apix_finvoice_discount(line, quantity, base_quantity)
            rows.append({
                "kind": "product",
                "code": product.default_code or "",
                "name": name or "",
                "description": _description(line),
                "gtin": product.barcode if gtin.is_valid_gtin(product.barcode) else None,
                "quantity": quantity,
                "rec20_code": code,
                "unit_name": unit_name,
                "base_quantity": base_quantity,
                # Before discount when the discount is stated, else
                # net of discount; VAT excluded, per the line's own unit.
                "price_unit": float(discount.pop("gross_price")) if discount
                else (line.price_subtotal / line.quantity if line.quantity else line.price_unit),
                "discount": discount,
                "tax_percent": _tax_percent(line),
                # ADR-0005: the category of the line's highest-rate tax.
                "tax_code": vat.resolve_line(
                    self._apix_line_vat_taxes(line, vat_info))["category"],
                "untaxed": line.price_subtotal,
                "tax": line.price_total - line.price_subtotal,
                "total": line.price_total,
            })
        return rows, without_code

    def _apix_finvoice_discount(self, line, quantity, base_quantity):
        """``{gross_price, percent, amount}`` of a discounted line, or None
        (net price, as before): the gross price is the line's unit price
        without VAT (price-included taxes removed by the ERP's own tax
        computation at discount 0), stated in five decimals; the amount
        makes the row add up to its own amount."""
        if not line.discount or line.discount <= 0 or not line.quantity:
            return None
        # `round_base=False`: the ERP would round the base to cents (0.99 with
        # 24 % included is 0.80, not 0.79839), which a large quantity turns
        # into a wrong gross row (review F1).
        # Odoo 18.0 also rounds the base per line inside the tax computation
        # unless it computes globally: `rounding_method="round_globally"`.
        taxes = line.tax_ids.with_context(round_base=False).compute_all(
            line.price_unit, line.currency_id, 1.0, line.product_id, line.partner_id,
            is_refund=line.move_id.move_type == "out_refund", rounding_method="round_globally")
        gross = Decimal(repr(taxes["total_excluded"])).quantize(
            Decimal("0.00001"), rounding=ROUND_HALF_UP)
        return finvoice_rows.discount_row(
            quantity, gross, base_quantity, line.discount, line.price_subtotal)

    def _apix_finvoice_taxes(self, vat_info=None):
        """The VAT breakdown: one entry per rate and VAT category
        of the lines, the base from the lines, the VAT from the invoice's tax
        lines (globally rounded) shared over the entries its tax is in; a
        no-VAT entry carries its reason and exemption code. Fixed-amount
        taxes keep their own entries as before (spec §3)."""
        vat_info = self._apix_vat_info() if vat_info is None else vat_info
        lines = [
            {"untaxed": line.price_subtotal, "taxes": self._apix_line_vat_taxes(line, vat_info)}
            for line in self._apix_finvoice_product_lines()
        ]
        amounts = {}
        fixed = {}
        for line in self.line_ids.filtered(lambda line: line.display_type == "tax"):
            tax = line.tax_line_id
            amount = line.amount_currency * self.direction_sign
            if tax.amount_type == "percent":
                amounts[tax.id] = amounts.get(tax.id, 0.0) + amount
                continue
            fixed[tax.amount] = fixed.get(tax.amount, 0.0) + amount
        reasons = {
            tax_id: {"reason_lines": info["reason_lines"],
                     "exemption_code": info["exemption_code"]}
            for tax_id, info in vat_info.items()
        }
        entries = [
            {key: entry[key] for key in
             ("percent", "base", "amount", "reason_lines", "exemption_code")}
            | {"code": entry["category"]}
            for entry in vat.group_breakdown(lines, amounts, reasons)
        ]
        entries += [
            {"percent": rate, "base": 0.0, "amount": amount, "code": None,
             "reason_lines": [], "exemption_code": None}
            for rate, amount in fixed.items()
        ]
        return entries

    def _apix_finvoice_epi(self, sender):
        reference = self._apix_finvoice_payment_reference()
        return {
            "date": _iso(self.invoice_date),
            "reference": "",
            "bic": sender["bic"],
            "beneficiary_name": sender["name"],
            "iban": sender["iban"],
            "remittance_reference": reference or None,
            "amount": self.amount_total,
            "due_date": _iso(self.invoice_date_due or self.invoice_date),
            # AC-20: SEPA credit transfer in euros, else a plain credit transfer.
            "payment_means_code": writer.payment_means_code(self.currency_id.name),
        }

    # ------------------------------------------------------------------
    # References and the delivery party (ADR-0007; AC-09 - AC-15, AC-27)
    # ------------------------------------------------------------------

    def _apix_finvoice_references(self):
        """The order references the message states, and why one is left out.

        ``order_number`` - the customer's: the customer reference less our
        echoed order numbers; a credit note made by reversal takes the
        reversed invoice's. ``seller_reference`` - ours: the source document
        as written, withheld when it would pass as a payment reference and the
        message carries none. An over-long one is left out, never cut
        (``*_problem`` = ``too_long``)."""
        self.ensure_one()
        source = self
        if self.move_type == "out_refund" and self.reversed_entry_id:
            source = self.reversed_entry_id
        order_number, order_problem = finvoice_references.customer_order_number(
            source.ref, source.invoice_origin)
        ours, ours_problem = finvoice_references.our_order_number(self.invoice_origin)
        payment_reference = self._apix_finvoice_payment_reference()
        withheld = not finvoice_references.seller_reference_allowed(ours, payment_reference)
        return {
            "order_number": order_number,
            "order_number_problem": order_problem,
            "seller_reference": None if withheld else ours,
            "seller_reference_problem": ours_problem,
            "seller_reference_withheld": withheld,
            "buyer_reference": self.einvoice_buyer_reference or None,
        }

    def _apix_finvoice_payment_reference(self):
        """The payment reference the message carries (a valid one only)."""
        reference = self.payment_reference or ""
        if not finvoice_values.payment_reference_is_valid(reference):
            return None
        return reference

    def _apix_finvoice_delivery(self):
        """``{state, missing, values}``: the delivery party.

        ``none`` - no delivery address or the invoice contact itself;
        ``same`` - another contact at the invoice contact's postal address
        (street, second street line, post code, town, country compared
        without regard to case and spacing); ``incomplete`` - a different
        address that lacks a name of 2 characters, a street, a post code or
        a town (``missing`` names them), left out with a readiness warning;
        ``sent`` - ``values`` go in the message."""
        self.ensure_one()
        shipping = self.partner_shipping_id
        result = {"state": "none", "missing": [], "values": None}
        if not shipping or shipping == self.partner_id:
            return result
        if _postal_key(shipping) == _postal_key(self.partner_id):
            return dict(result, state="same")
        name = (shipping.name or shipping.commercial_partner_id.name or "").strip()
        fields = {"name": name, "street": (shipping.street or "").strip(),
                  "zip": (shipping.zip or "").strip(), "city": (shipping.city or "").strip()}
        missing = [key for key, value in fields.items() if len(value) < DELIVERY_MIN_LENGTH]
        if missing:
            return dict(result, state="incomplete", missing=missing)
        values = dict(fields, country_code=shipping.country_id.code or None)
        # review F6: the second street line is compared, so it goes too (the
        # second DeliveryStreetName, at least 2 characters).
        street2 = " ".join((shipping.street2 or "").split())
        if len(street2) >= DELIVERY_MIN_LENGTH:
            values["street2"] = street2
        return dict(result, state="sent", values=values)


# ----------------------------------------------------------------------
# Line helpers
# ----------------------------------------------------------------------


def _line_unit(line):
    """``(quantity, rec20 code or None, readable name, base quantity)``.

    A unit with its own code goes as is. A unit without one that is a
    multiple of a coded reference unit (18.0: of its category, e.g. "100
    pcs") goes in the reference unit: the quantity in reference units and the
    price per ``relative_factor`` of them. Anything else goes with its
    name only.
    """
    unit = line.product_uom_id
    if not unit:
        return line.quantity, None, "", 1
    code, name = unit._einvoice_outgoing()
    if code:
        return line.quantity, code, name, 1
    reference, factor = _reference_unit(unit)
    if reference and factor and factor != 1:
        reference_code, reference_name = reference._einvoice_outgoing()
        if reference_code:
            return line.quantity * factor, reference_code, reference_name, factor
    return line.quantity, None, name, 1


def _reference_unit(unit):
    """``(reference unit, factor)``: Odoo 18.0 relates every unit to the
    reference unit of its category (19.0: ``relative_uom_id`` and
    ``relative_factor``); ``factor_inv`` is how many reference units one
    unit holds."""
    if unit.uom_type == "reference":
        return unit.browse(), 1
    reference = unit.search(
        [("category_id", "=", unit.category_id.id), ("uom_type", "=", "reference")], limit=1
    )
    return reference, round(unit.factor_inv, 6)


def _tax_percent(line):
    return sum(
        tax.amount for tax in line.tax_ids.flatten_taxes_hierarchy()
        if tax.amount_type == "percent"
    )


def _description(line):
    """The line's own text when it says more than the product name."""
    product = line.product_id
    if not product or not line.name:
        return None
    if line.name.strip() in (product.name, product.display_name):
        return None
    return line.name


def _iso(value):
    return value.isoformat() if value else None


def _postal_key(partner):
    """The postal address compared for the delivery party: street, second
    street line, post code, town and country, without case and spacing."""
    parts = (partner.street, partner.street2, partner.zip, partner.city,
             partner.country_id.code)
    return tuple(" ".join((part or "").split()).casefold() for part in parts)
