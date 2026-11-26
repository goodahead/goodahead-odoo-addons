# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Install hook (promoted from the feature's staged migrations 02 and 03).

1. **Sent before install**: customer invoices and credit
   notes the ERP had already marked sent count as already sent, channel
   unknown. The ERP's own sent mark cannot tell an email from a download, and
   a needless confirmation is the cheaper error than sending an old invoice
   twice. ``is_move_sent`` is read here exactly once; after installation it
   is never read again.
2. **Transmit-method back-fill**: existing customer
   documents get a transmit method by the rule a new invoice uses — the
   contact's, else the commercial customer's channel for the invoice's
   company (``apix`` -> einvoice, ``apix_letter`` -> letter, ``email`` ->
   email; any other value counts as not set); else e-invoice when the contact
   (else the commercial customer) has an e-invoice address; else email. A
   company-level default of the field (``ir.default``) is not consulted.

🔴 NOTHING UNTIL SEND: set-based SQL that fills only this add-on's two
columns — no attempt, attachment, job or APIX call, and no per-record loop.

Down: both columns are dropped with the add-on on uninstall; nothing else is
written, so there is nothing to undo.
"""

CUSTOMER_TYPES_SQL = "('out_invoice', 'out_refund')"
CHANNEL_TO_METHOD = {"apix": "einvoice", "apix_letter": "letter", "email": "email"}


def mark_sent_before_install(cr):
    """Mark the customer documents the ERP had marked sent; returns the count."""
    cr.execute(
        f"""
        UPDATE account_move
           SET sent_before_install = TRUE
         WHERE move_type IN {CUSTOMER_TYPES_SQL}
           AND is_move_sent IS TRUE
           AND sent_before_install IS NOT TRUE
        """
    )
    return cr.rowcount


def _sending_method_expr(cr, alias):
    """The partner's channel for the move's company: the ERP stores the
    company-dependent field as jsonb keyed by company id (18.0+); a plain
    text column (older layouts) is read directly."""
    cr.execute(
        """
        SELECT data_type FROM information_schema.columns
         WHERE table_name = 'res_partner' AND column_name = 'invoice_sending_method'
        """
    )
    row = cr.fetchone()
    if not row:
        return "NULL"
    if row[0] == "jsonb":
        return f"({alias}.invoice_sending_method ->> m.company_id::text)"
    return f"{alias}.invoice_sending_method"


def _mapped(expr):
    whens = " ".join(
        f"WHEN '{channel}' THEN '{method}'" for channel, method in CHANNEL_TO_METHOD.items()
    )
    return f"(CASE {expr} {whens} ELSE NULL END)"


def backfill_transmit_method(cr):
    """Give customer documents without a transmit method the default one;
    returns the count. A method already set is kept."""
    contact_channel = _mapped(_sending_method_expr(cr, "p"))
    commercial_channel = _mapped(_sending_method_expr(cr, "c"))
    cr.execute(
        f"""
        UPDATE account_move m
           SET transmit_method = COALESCE(
                   {contact_channel},
                   {commercial_channel},
                   CASE WHEN COALESCE(NULLIF(p.edicode, ''), NULLIF(c.edicode, '')) IS NOT NULL
                        THEN 'einvoice' ELSE 'email' END
               )
          FROM res_partner p
          LEFT JOIN res_partner c ON c.id = p.commercial_partner_id
         WHERE p.id = m.partner_id
           AND m.move_type IN {CUSTOMER_TYPES_SQL}
           AND m.transmit_method IS NULL
        """
    )
    updated = cr.rowcount
    # A customer document without a customer: email, as for a customer with
    # neither a channel nor an address.
    cr.execute(
        f"""
        UPDATE account_move
           SET transmit_method = 'email'
         WHERE move_type IN {CUSTOMER_TYPES_SQL}
           AND transmit_method IS NULL
        """
    )
    return updated + cr.rowcount


def post_init_hook(env):
    mark_sent_before_install(env.cr)
    backfill_transmit_method(env.cr)
