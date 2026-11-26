# Architecture — the invoice import platform

This document describes how `goodahead_invoice_import` (the core) and the add-ons
built on it fit together (written for Odoo 19.0; the 18.0 port differs only where the CHANGELOG says so). Decisions are named after the
architecture decision records (ADRs) kept in the development repository; their
titles are quoted in *italics*.

## 1. Goals

1. **Integrity** — no lost invoice, no bill created twice, no vendor created or
   overwritten by an import.
2. **Safe evolution** — databases running the AI import upgrade through the
   split with nothing lost; the code ports to 18.0 and 16.0 by changing only
   version-specific adapters.
3. **Operability** — every incoming invoice is visible with its status; failures
   and outages reach an Administrator as one to-do each.

## 2. The add-ons

*Split the import into a core and four source or connection add-ons.*

```
goodahead_invoice_import                 core, no AI (LGPL-3)
├── goodahead_invoice_ai_import          source: PDF read by AI (needs goodahead_claude)
├── goodahead_invoice_import_finvoice    source: Finvoice by upload or email
goodahead_apix                           shared per-company APIX connection (no import dependency)
goodahead_invoice_import_apix            APIX receiving: core + Finvoice + APIX connection
goodahead_invoice_import_apix_migration  migration tool: moves a company over from an earlier APIX
                                         connector; registry of migration sources
goodahead_invoice_import_apix_migration_community
                                         source `community`: the community APIX modules
                                         (LGPL-3, depends on the AGPL-3 community modules;
                                         auto-installed next to connector_apix)
```

Dependencies point one way: sources depend on the core; the core depends only on
Odoo (`account`, `account_edi_ubl_cii`, `mail`, `queue_job`). The APIX
connection is separate so a later sending add-on can share it.

Each add-on keeps a pure-Python layer in `lib/` with no Odoo import (APIX client,
Finvoice parser and mapping, invoice-data validation, lifecycle rules, listing
window arithmetic). *Keep a pure-Python heart and isolate version-specific code*:
only the models, views and the core profile change between Odoo versions.

## 3. Core concepts

### Import record — `goodahead.invoice.import`

One record per incoming invoice (one per invoice in a multi-invoice Finvoice
file; one per APIX package). Fields that matter: `company_id`, `source`
(upload, email, apix), `reader` (which source add-on reads it), `state`,
`failure_kind` (data or interrupted), `extraction_data` (the invoice data as
JSON), `move_id`, `duplicate_of_move_id`, close reason, closed by and date.

### Lifecycle

*Derive the processing status from the background job.*

| Stored state | Meaning | Settled? |
|---|---|---|
| waiting | known, not yet imported | no |
| imported | a draft bill exists | **yes** |
| duplicate | stopped by the duplicate rule | no |
| failed | data error or interrupted import | no |
| possibly lost | APIX shows it received, but no bill exists (migration) | no |
| closed | closed without bill by an Accountant, with a reason | **yes** |

*Processing* is never stored. It is shown while the record's `queue_job` is
pending, enqueued or started, so a crashed worker can never leave a record stuck
in a stored "processing" state. After 30 minutes the inbox flags it as
*processing too long*.

Settling a record (imported, or closed) calls `_on_settled()`. The core does
nothing there; the APIX add-on uses it to mark the package received.

### Invoice data

Every source produces the same dict (the former AI extraction output): `type`
(Invoice or CreditNote), `invoice_number`, dates, `currency`, `seller` and
`buyer` blocks, `payment_reference`, `lines[]`, `tax_subtotals[]`, `totals`,
plus an optional `source_meta` for structured formats. The same validators check
every source. Totals tolerance: the larger of 0.02 and 0.5 % of the larger
compared amount (0.02 for the amount to pay).

*Route Finvoice through the extraction data and the standard decoder*: there is
one bill-building path. Invoice data → Peppol BIS 3.0 XML
(`adapters/ubl_generator.py`) → Odoo's own `account_edi_ubl_cii` decoder on a fresh
draft move → post-processing (expense category accounts, bank checks, vendor
data suggestions, chatter).

### Source dispatch

The core owns the mail door (`message_new`) and the import job
(`_process_import`); a source never overrides them. A source registers a
`SourceReader(key, priority, policy, structured)` in `_import_readers()` and
answers these hooks for its own key only, passing every other key to `super()`:

| Hook | Used for |
|---|---|
| `_source_accepts_attachment(key, file)` | mail intake: the first reader (lowest priority number) that accepts a file takes it. Finvoice (10) sniffs the content; AI (100) takes `.pdf` |
| `_source_claim_companions(key, documents, pool)` | files that belong with a document (e.g. the PDF image of a Finvoice message) |
| `_source_receive_mail(key, files, companions, company, values, msg)` | creates and enqueues the records |
| `_source_read(key)` | the job's reading (AI call, or Finvoice parse) |
| `_source_bill_attachments(key)` | which files go on the bill when re-importing |

Upload into a purchase journal follows the same order. Nothing depends on module
load order. A mail nothing can read becomes one failed record
(`import.unsupported_format`) that keeps the files.

Entry points for sources that do not come through mail or upload (APIX):
`_create_for_source(source, company, files, values)` and
`_import_from_invoice_data(invoice_data, policy, …) → ImportResult`
(`outcome` imported / duplicate / failed, `move`, `duplicate_of`, `code`,
`message`). Neither commits.

### Matching policy

*Let sources set a vendor matching policy.* A `MatchingPolicy` says which seller
identifiers may **set** the vendor, which only **propose** candidates, and what
happens on a conflict.

| | assign by | propose by | conflict |
|---|---|---|---|
| Default (AI, PDF) | VAT, Business ID, phone, domain, name | — | first match (existing behaviour) |
| Strict (Finvoice, APIX) | VAT, Business ID | phone, domain, name | leave empty |

Vendors are counted as vendor companies (contacts collapse into their company).
When several companies share an assigning identifier, none is set and all are
offered. No policy ever creates a vendor. Under the strict policy the decoder
never creates a bank account and never attaches an IBAN that belongs to another
partner.

### Duplicate rule

*Enforce duplicate invoices in the core before any bill.* Before a bill is
built, the core looks for a non-cancelled bill or credit note of the same kind,
in the same company, from the same vendor company — or, with no vendor matched,
from a partner with the seller's VAT number or Business ID — with the same
invoice number, compared after removing case, spaces and separators. An earlier
import record with the same number counts too. The lookup uses an expression
index on the normalised bill reference created by the core's post-install hook.

On a hit the outcome is *duplicate*: no bill, a link to the existing one. An
Accountant may *Create bill anyway* (sets the override and re-runs) or *Close
without bill*. The rule runs for every source, including PDFs read by the AI.

## 4. Transactions and jobs

- One background job is one transaction. There is no `commit()` inside a job: a
  mid-job commit followed by an error makes `queue_job` retry work that already
  happened.
- Import jobs use the identity key `invoice-import:<record id>`, so one record
  never has two jobs.
- Data errors fail the record visibly and are not retried automatically.
  Technical errors are retried by the job and then recorded as *interrupted*.

## 5. APIX receiving (summary)

Details are in the `goodahead_invoice_import_apix` README.

- **Scheduler** (hourly cron) enqueues one run per company whose connection works
  and whose receiving state is shadow or live. Each run is its own job on the
  `root.apix` channel, so one company's failure never stops another's.
- **Listing** creates one import record per company and APIX storage id (a
  partial unique index enforces it). *Import each package in its own job keyed
  by company and storage id.*
- **Package import** downloads without marking received, translates the Finvoice
  message and imports it with the strict policy.
- **Mark received** runs in a separate job enqueued in the transaction that
  settled the record. *Mark packages received only after the bill commits*: if
  the import rolls back, the mark job never existed and APIX still lists the
  package.
- **Daily checks** warn about packages about to leave APIX's listing window and
  reconcile APIX's received flag with the inbox.

## 6. Migration from an earlier APIX connector (summary)

*Pause the community modules per company through a bridge add-on*, *Recognise
earlier imports by matching existing bills* and *Migration sources are adapters
in their own add-ons.* The migration tool (its own add-on) carries the transfer
credentials over from an earlier connector's migration source, runs a shadow
period that classifies every listed package without creating bills or marking
anything received, switches a company to live (every source pauses its
connector for that company; for the community modules their adapter does), and
can switch back. Packages APIX already shows as received are matched first
against the earlier connector's own record when its source keeps one, then
against existing bills by attachment checksum and by the duplicate rule;
unmatched ones become *possibly lost* records for an Accountant to import or
close. The source interface is described in the migration add-on's README
(*Writing an adapter for another connector*).

## 7. Upgrade through the split

*Rename the import record model in place during the upgrade.*

Upgrading `goodahead_invoice_ai_import` to 19.0.4.0.0 installs the core first.
The core's `pre_init_hook`:

1. does nothing on a fresh database (no `ai_invoice_import` table);
2. refuses when the old table exists but `goodahead_invoice_ai_import` is not
   being upgraded in the same run;
3. refuses while `queue_job` holds pending, enqueued, started or waiting jobs
   for the old model;
4. otherwise renames the table, sequence and constraints, every column that
   stores a model name (messages, attachments, activities, followers, jobs,
   rules, properties, aliases and more), and moves ownership of every external
   id the core now declares from the AI add-on to the core;
5. maps old states (draft and processing → waiting, done → imported).

What moves is computed from what the core declares, not from a hand-written
list. Fields the AI add-on still declares (model name and token counts) keep
their AI ownership, so uninstalling the AI part removes only those columns.

The `post_init_hook` creates the duplicate-rule index, backfills source,
normalised invoice number and seller identifiers, and seeds partner identifiers.

## 8. Security

- Accountant = billing rights; Administrator = accounting-administrator rights.
- Import records are separated by company through a record rule; Scheduler runs
  are per company.
- APIX transfer credentials live only on the connection record, readable by
  accounting administrators; they never appear in logs, chatter, exports or
  copies. *Store APIX credentials on a per-company connection record.*
- Everything arriving by mail, upload or APIX is untrusted: drafts only, the
  duplicate rule, no vendor creation or overwrite.

## 9. Accepted debt

- Generic fields on vendor bills keep their `ai_` prefix, and some system
  parameters keep their `goodahead_invoice_ai_import.*` keys. Renaming columns
  adds upgrade risk without user value; planned for a later minor release.
- Only the receiving half of the community APIX modules is replaced; their
  sending half stays until a sending add-on exists.
