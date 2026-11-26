# Goodahead Finnish Invoice (`goodahead_finnish_invoice`)

The customer invoice PDF laid out as a Finnish invoice, with the Finnish bank
bar code (virtuaaliviivakoodi), so a Finnish customer can pay straight from it.

- An invoice detail table: number, order number, dates, payment terms.
- Line amounts both excluding and including VAT, and a VAT breakdown per rate.
- Payment details as a table: bank, IBAN, BIC, reference, due date, amount.
- The bank bar code, versions 4 and 5, on invoices that still have something
  to pay.
- The late payment interest (Viivästyskorko) and complaint period
  (Huomautusaika) of the company, when set.

## Install

Put the add-on on the add-ons path and install *Goodahead Finnish Invoice*
from Apps. It depends only on `account`; the Finnish localisation provides the
Finnish payment reference standards used for the bar code.

## Which documents it applies to

Customer invoices, credit notes and receipts of companies whose fiscal country
is Finland. The report replaces the standard one through the standard
`account.move._get_name_invoice_report` hook, so the ordinary Print and Send
buttons produce it. Every other company, vendor bills and every other report
are untouched.

When the APIX sending add-on (`goodahead_invoice_send_apix`) is installed, this
PDF is also the image every e-invoice carries: the printed invoice and the
e-invoice image are the same document.

## Setup

1. **Company bank account.** Add the company's Finnish IBAN (and BIC) as a bank
   account of the company. The invoice's own recipient bank account is used;
   when an invoice has none, the company's first bank account is printed.
2. **Payment reference.** On the sales journal (*Advanced Settings →
   Communication Standard*), choose *Finnish Standard Reference* or *Finnish
   Creditor Reference (RF)* (from the Finnish localisation). The bar code is
   left off when the invoice's payment reference is neither.
3. **Late payment interest and complaint period** (optional): *Settings →
   Companies → (company) → General Information → Finnish Invoice*. Both are
   free text, printed as written (for example `7,50 %` and `8 pv`), and left
   off the invoice when empty.
4. **Language.** The layout's labels are translated to Finnish; print in
   Finnish for a Finnish customer.

Nothing else to configure: installing the add-on switches the layout on for
every Finnish company.

## Usage

Print or send a customer invoice or credit note of a Finnish company as usual
(*Print* or *Send*). The PDF uses this layout, and an unpaid customer invoice
carries the bank bar code under the payment details.

## Bank bar code

Both versions are exactly 54 digits:

- **Version 4** (Finnish reference number): `4`, the IBAN without `FI`
  (16 digits), euros (6) and cents (2), three zeros, the reference
  right-aligned in 20 digits, and the due date as `YYMMDD`.
- **Version 5** (RF creditor reference): `5`, the IBAN without `FI`, euros and
  cents, the RF check digits (2), the reference without `RF` and its check
  digits right-aligned in 21 digits, and the due date.

A missing due date is encoded as six zeros. The bar code is printed only on a
customer invoice (never a credit note) that still has something to pay, in
euros (the invoice's or the company's currency), into a Finnish IBAN, with a
valid reference and an amount of at most 999 999,99. When any of this does
not hold, the invoice prints without a bar code and the reason is logged
(without the account number).

## Tests

The bar code and address helpers run without the ERP:

    python -m unittest discover -s goodahead_finnish_invoice/tests -t goodahead_finnish_invoice

and, with the report test, in the ERP's test runner with
`--test-tags /goodahead_finnish_invoice`.

## Licence

LGPL-3.0 or later. Author: Goodahead Oy.
