# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""19.0.1.8.0 — ``seller_vat`` is stored normalised (upper case, no
whitespace; `lib.import_record.normalize_vat`), because the duplicate rule now
looks an earlier record up by equality. Records written before keep the VAT as
the invoice printed it; this brings them in line.

The company of the vendor-data suggestions and candidates is a stored related
field: the ORM computes it for the existing rows when the add-on is upgraded,
so nothing is done for it here.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        r"""
        UPDATE goodahead_invoice_import
           SET seller_vat = NULLIF(upper(regexp_replace(seller_vat, '\s', '', 'g')), '')
         WHERE seller_vat IS NOT NULL
           AND seller_vat IS DISTINCT FROM
               NULLIF(upper(regexp_replace(seller_vat, '\s', '', 'g')), '')
        """
    )
