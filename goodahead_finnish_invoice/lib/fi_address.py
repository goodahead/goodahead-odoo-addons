# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Deciding how a document's billing and delivery addresses are printed.

Shared by the invoice and the proforma so the two documents make the same
call: a customer holding both should not find the addresses laid out one way
on the proforma and another way on the invoice.
"""

# The fields the contact widget actually puts on the page. Comparing these
# rather than the records means two partner rows that happen to describe the
# same place collapse to one block, which is the point — the reader cares
# where the goods go, not how many contacts Odoo keeps for it.
ADDRESS_FIELDS = ("street", "street2", "zip", "city")


def same_place(billing, delivery):
    """True when the delivery block would just repeat the billing block."""
    if not delivery or delivery == billing:
        return True
    if (delivery.country_id, delivery.state_id) != (
        billing.country_id,
        billing.state_id,
    ):
        return False
    return all(
        (delivery[field] or "").strip() == (billing[field] or "").strip()
        for field in ADDRESS_FIELDS
    )


def delivery_name(delivery, billing):
    """The name line for the delivery block.

    Odoo's contact widget prints ``display_name``, which prefixes a child
    contact with its parent — "Solo Sokos Hotel Turun Seurahuone, Receptionist
    Roger" — so a delivery site under the same customer repeats, in full, the
    company name the billing block beside it already states, and at a width
    that pushes the two columns into each other.

    Within one company the site's own name is what identifies the place.
    Across companies the whole name is needed to say whose place it is.
    """
    if delivery.commercial_partner_id == billing.commercial_partner_id:
        return delivery.name or delivery.display_name
    return delivery.display_name
