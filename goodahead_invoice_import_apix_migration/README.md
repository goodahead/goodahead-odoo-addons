# Invoice Import — APIX migration tool (`goodahead_invoice_import_apix_migration`)

Moves a company, one at a time, from an **earlier APIX connector** to the APIX
receiving add-on (`goodahead_invoice_import_apix`) without losing an invoice
or importing one twice: carry the APIX credentials over, watch a **shadow
run**, **switch to live**, and **switch back** if needed.

The tool knows no connector itself. Each earlier connector is a **migration
source** supplied by a small **adapter add-on** that installs itself next to
that connector:

| Earlier connector | Adapter | Source code |
|---|---|---|
| Community APIX modules (`connector_apix`) | `goodahead_invoice_import_apix_migration_community` (LGPL-3; depends on the AGPL-3 community modules) | `community` |
| Another third-party APIX connector | planned (18.0) | — |

## Which add-ons to install

| You need | Install | What comes with it |
|---|---|---|
| APIX e-invoices | `goodahead_invoice_import_apix` | `goodahead_invoice_import`, `goodahead_invoice_import_finvoice`, `goodahead_apix` |
| Moving a company over from an earlier APIX connector | nothing by hand where an adapter exists: it installs itself where the receiving add-on and its connector are both installed, and brings this add-on | this add-on |

While an adapter is installed (or once a connection was migrated), the
receiving radio on the APIX connection is read-only: going live and back
happen here, so the earlier connector is always paused first.

## Configuration

None of its own. The tool works on the company's APIX connection
(`goodahead_apix`), which must exist first.

## Using the tool

Open it from the APIX connection form (**Migration tool**) or from
*Accounting ▸ Configuration ▸ E-invoicing ▸ APIX migration tool*. Accounting administrators only,
one company at a time.

1. **Detect.** The tool asks every installed source what it has for the
   company and shows the environment and Business ID it found. If several
   earlier connectors have credentials for the company, choose the one to
   migrate from.
2. **Carry over.** The environment, transfer credentials and Business ID are
   copied into the receiving add-on's connection, which starts in **shadow**
   and remembers where it came from (*Migrated from*). No web password is ever
   read or copied. With nothing to carry over, connect by hand instead.
3. **Shadow run.** Leave it running alongside the earlier connector — at least
   a week is recommended — and open the shadow results. The tool counts what
   would be imported, what would be a duplicate, what was imported before,
   what would be possibly lost, what would fail, and what is not classified
   yet. It warns before a week has passed or while packages are unclassified.
4. **Switch to live.** Needs a working connection and every source reporting
   that its connector is the version it supports. Every source pauses its
   connector for this company (for the community modules: their listing and
   re-fetch skip the company, queued downloads are cancelled). The shadow
   results are cleared and a fresh run evaluates every package again:
   packages APIX already shows as received are recognised — from the earlier
   connector's own record when it keeps one, otherwise from the existing
   bills — and the rest become *possibly lost* for an Accountant. Packages
   that arrived in between are imported once.
5. **Switch back** (if needed). Refused while any mark-received is still
   unconfirmed or a package of the company is being imported. Otherwise the
   tool lists the unsettled packages the earlier connector will import, and
   on confirmation turns receiving off and has every source resume its
   connector with the credentials it kept.

## Writing an adapter for another connector

The interface, on `goodahead.apix.connection`:

- register: `_migration_sources()` (append your code), `_migration_source_label(code)`;
- required: `_migration_<code>_detect(company)`, `_migration_<code>_status()`,
  `_migration_<code>_pause(company)`, `_migration_<code>_resume(company)`;
- optional: `_migration_<code>_find_bill(company, storage_id)`.

Make the adapter `auto_install` on `goodahead_invoice_import_apix` and the
connector. Never touch the connector's sending, never mark anything received
at APIX, never read a web password, never commit.
`goodahead_invoice_import_apix_migration_community` is the reference
implementation; this add-on's `tests/common.py` shows how to test with
test-only sources.

## Security

- Accounting administrators only, and only for companies they belong to (an
  access check in every step and a record rule on the tool).
- The tool never shows the transfer key; sources hand over the four
  credential fields only, and a source that returns anything else is refused.

## Known limitations

- An earlier connector without an adapter is invisible: the receiving add-on
  cannot tell it is there.
- A download the earlier connector already started when the company was
  paused cannot be stopped; the duplicate rule and the recognition still keep
  it from becoming a second bill.

## Licence

LGPL-3.0 or later. Copyright Goodahead Oy — https://www.goodahead.com/
