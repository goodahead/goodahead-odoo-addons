# Invoice Sending through APIX — migration bridge (`goodahead_invoice_send_apix_migration`)

The sending side of the APIX migration tool. It moves a company's sending,
one company at a time, from an **earlier APIX sending setup** to
`goodahead_invoice_send_apix` without losing a customer's e-invoice details
and without sending an old invoice again — and it makes sure a company never
has two senders.

No adapter for an earlier setup is part of this add-on: each earlier setup
brings its own (for the community APIX modules
`goodahead_invoice_send_apix_migration_community`, installed automatically).

## Install order

1. Keep the earlier setup installed and sending.
2. Install `goodahead_invoice_send_apix`. With the receiving migration tool
   (`goodahead_invoice_import_apix_migration`) installed, this bridge and the
   earlier setup's adapter install themselves next to it. Sending through
   APIX stays off.
3. Carry the company's APIX connection over (the receiving step of the
   migration tool), then **Carry over sending** for the company.
4. Switch sending on (APIX connection ▸ *Sending*). Repeat per company.

An earlier connector the receiving migration tool detects for the company
(its transfer credentials are found) but that has no sending adapter may
still send that company's invoices: switching sending on is refused for that
company, naming the connector. The ways out: remove that connector's APIX
credentials for the company (detection reads archived records too, so
archiving is not enough), uninstall the connector, or install its sending
adapter. **Switch sending back** undoes the stop for one company while
sending here is still off.

## The sending step (migration tool)

The APIX migration tool (*Accounting ▸ Configuration ▸ E-invoicing ▸ APIX migration tool*, accounting
administrators), button **Sending**. For the
company it shows, per earlier sending setup: whether it still sends, and how
many customers with e-invoice details, customer channels, invoice transmit
methods and sent invoices it would carry over. **Carry over sending** writes
them and stops the earlier setup sending for the company ("You can now switch
sending on"); customers whose operator is not on the operator list are named
and left as they are. **Switch sending back** lets the earlier setup send
again, only while sending through this add-on is off for the company.

The step first needs the company's APIX connection (the receiving
carry-over). When the receiving tool found an earlier connector that has no
sending adapter, it says so and carries nothing over.

## What it does

- **Earlier-senders guard.** Switching this add-on's sending on for a company
  is refused while an earlier setup still sends for it; the message names the
  setup and points to the migration tool. A setup whose adapter is incomplete
  counts as still sending, and so does a connector the receiving migration
  tool detects for the company that has no sending adapter at all (it may
  still send; the message names it and the ways out: remove its APIX
  credentials for the company, uninstall it, or install its sending adapter).
- **Carry over** (Administrators of the company): copies, for that company
  only, the customers' e-invoice addresses and operators (an operator not on
  the operator list is reported, never guessed), the customers' channels
  (e-invoice / printed letter / email via APIX), the invoices' transmit
  methods, and one "sent by the earlier setup" entry in the delivery history
  of every invoice it had sent (date, batch id, document id, credits where
  known) — so sending one again asks for confirmation. Then it stops the
  earlier setup sending for the company. Running it again creates no
  duplicates.
- **Switch sending back**: lets the earlier setup send for the company again,
  only while this add-on's sending is off for it. Nothing carried over is
  undone.

## Sending sources

The bridge knows no connector. A migration source of the receiving migration
tool (`goodahead_invoice_import_apix_migration`) becomes a **sending source**
when its adapter add-on implements, on `goodahead.apix.connection`:

| Method | Returns / does |
|---|---|
| `_migration_<code>_sending_status(company)` | `("sending" \| "stopped" \| "none", reason)`; read-only |
| `_migration_<code>_sending_read(company)` | `{recipients, customer_channels, invoice_methods, sent_invoices}` — exactly these keys; read-only |
| `_migration_<code>_sending_stop(company)` | stop sending for the company; idempotent |
| `_migration_<code>_sending_resume(company)` | the mirror of stop; idempotent |

Adapters never write partner fields, transmit methods or the delivery
history themselves, never call APIX and never touch another company.

## Install

Installs itself wherever `goodahead_invoice_send_apix` and
`goodahead_invoice_import_apix_migration` are both installed. Depends on no
AGPL add-on; only an adapter may depend on its connector.

## Configuration

None. The sending step is part of the APIX migration tool.

## Tests

`--test-tags /goodahead_invoice_send_apix_migration`; the sources are
test-only fakes (`tests/common.py`), all data invented, no APIX call.

## Licence

LGPL-3.0 or later. Author: Goodahead Oy.
