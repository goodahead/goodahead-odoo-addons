# Invoice Sending through APIX — migration from the community modules (`goodahead_invoice_send_apix_migration_community`)

The community APIX modules (`connector_apix`, `account_edi_finvoice`,
`account_invoice_transmit_method`) as a **sending source** of the APIX
migration tool, source code `community`. It lets the tool's sending step
(`goodahead_invoice_send_apix_migration`) carry a company's sending over to
`goodahead_invoice_send_apix` and **stop the community modules sending for
that company**, so a company never has two senders.

## Install

You do not install it by hand: it installs **automatically** wherever both
`goodahead_invoice_send_apix` and `connector_apix` are installed, and goes
away with the community modules.

**Install order.** Keep the community modules installed until every company
has moved: install `goodahead_invoice_send_apix` (this adapter and the bridge
come with it), carry each company's connection and then its sending over in
the migration tool, and only then switch sending on for it. *Switch sending
back* lets the community modules send for a company again while sending
through `goodahead_invoice_send_apix` is off for it.

## Configuration

None. The adapter has no settings; the migration tool drives it.

## Usage

What the source tells the tool, per company:

| Tool | What the community modules answer or do |
|---|---|
| Status | **Sending** while the company has a community APIX backend; **stopped** after the carry-over (or without a backend while community data of the company is still there to carry); otherwise nothing found. Switching APIX sending on stays refused while the status is sending. |
| Read | Customers' e-invoice addresses and operators; customers' community transmit methods for the company (eInvoice → e-invoice, Printing service → printed letter, Email → email; other methods are not carried); the invoices' transmit methods; and the invoices the community modules sent, with their send date, APIX batch id, document id and credits where the community kept them. |
| Stop (carry-over) | The company is marked stopped: the community "send eInvoice" button refuses for it, community send jobs queued before the stop send nothing (and are cancelled), and posting no longer creates the community Finvoice document for it. |
| Resume (switch back) | The mark is cleared; the community modules send for the company again. |

The adapter only reads the community records and writes its own per-company
mark. It never sends, never calls APIX, never writes customers, transmit
methods or the delivery history itself (the tool does), never touches
another company and never deletes the community's records.

## Tests

`--test-tags /goodahead_invoice_send_apix_migration_community` on a database
with the community modules; the community's APIX call is patched, all data
invented.

## Licence

LGPL-3.0 or later. The add-on runs together with the community APIX modules,
whose licence applies to them: `connector_apix` (AGPL-3) from
https://github.com/tawasta/connector-apix, `account_edi_finvoice` (LGPL-3) from
https://github.com/tawasta/edi, and `account_invoice_transmit_method` (AGPL-3) from
https://github.com/OCA/account-invoicing. The community modules in turn need
`l10n_fi_edicode` from OCA/l10n-finland (https://github.com/OCA/l10n-finland).
No platform add-on depends on it. Author: Goodahead Oy.
