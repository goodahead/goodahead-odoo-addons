# Invoice Import (`goodahead_invoice_import`)

The shared core of the Goodahead invoice import platform for Odoo 19.0. It turns
incoming vendor invoices into **draft** vendor bills on the right **existing**
vendor, whatever channel brought them. It needs no AI.

The core does not read documents itself. Source add-ons read a document (a PDF
with AI, a Finvoice file) into one shared set of invoice data and hand it to
the core. The core then does the same for every source:

- **Import records.** One record per incoming invoice, shown in
  *Accounting ▸ Vendors ▸ Incoming invoices*, with its status: waiting,
  imported, duplicate, failed, possibly lost, closed without bill. While its
  background job is queued or running, a record shows as *processing*.
- **Vendor matching that never creates a vendor.** A bill from an unknown
  seller arrives without a vendor. Up to three likely vendors are offered, and
  *Create contact from this invoice* makes one only when an Accountant chooses
  it.
- **No overwrites.** A value the vendor already has is never changed. Each
  difference (address, bank account, VAT number) becomes a vendor data
  suggestion to review. Bank account and VAT number suggestions are never
  pre-selected.
- **Bank-account checks.** An IBAN that belongs to another partner is not
  attached; the chatter names its owner.
- **Duplicate rule.** A bill or credit note of the same kind, not cancelled,
  in the same company, from the same vendor company (or, with no vendor
  matched, the same seller VAT number or Business ID) with the same invoice
  number (ignoring case, spaces and separators) stops the import. The record
  is flagged *duplicate* with a link to the existing bill. An Accountant can
  *Create bill anyway* or *Close without bill*.
- **Recovery.** Retry a failed record from its kept invoice data, close a
  record without a bill (with a reason), reopen it.
- **Bill building.** Invoice data is turned into a Peppol BIS 3.0 document and
  decoded by Odoo's own `account_edi_ubl_cii` importer; expense categories set
  accounts on lines with no product.

## Which add-ons to install

| You need | Install | What comes with it |
|---|---|---|
| PDF invoices read with AI | `goodahead_invoice_ai_import` | this core, `goodahead_claude` |
| Finvoice files by upload or email | `goodahead_invoice_import_finvoice` | this core |
| APIX e-invoices | `goodahead_invoice_import_apix` | this core, `goodahead_invoice_import_finvoice`, `goodahead_apix` |
| Moving a company over from an earlier APIX connector (e.g. the community APIX modules) | `goodahead_invoice_import_apix_migration` | its adapter installs itself next to the connector and brings the migration tool (community: `goodahead_invoice_import_apix_migration_community`) |

You never install this core on its own for a reason of your own: a source
add-on installs it. Without any source add-on it receives nothing.

## Install and upgrade

- **New database:** install the source add-on you need (table above).
- **Database already running `goodahead_invoice_ai_import` (19.0.3.x):** run
  `-u goodahead_invoice_ai_import`. That upgrade installs this core first, and
  the core's pre-install hook renames the import record model
  `ai.invoice.import` to `goodahead.invoice.import` **in place**: same rows and
  ids, with messages, attachments, activities, followers and job records
  re-pointed. Old statuses are mapped (draft and processing become waiting,
  done becomes imported).
  - The upgrade **refuses** while import jobs are still queued or running.
    Let them finish (or cancel them) and run it again.
  - Installing this core on its own (`-i goodahead_invoice_import`) on such a
    database is **refused** with an instruction to run
    `-u goodahead_invoice_ai_import` instead.
  - **Take a backup first.** There is no automatic downgrade; going back means
    restoring the backup.

## Configuration

*Accounting ▸ Configuration ▸ Settings*, block *AI Invoice Import* (the title
is kept for compatibility; the block also holds settings that have nothing to
do with AI):

- **Domains That Never Identify a Vendor** — mail domains (free mail providers,
  placeholder domains) that must never match a vendor, on top of the shipped
  list.
- **Expense Categories** — map each category to an expense account per
  company. Lines with no matched product get the category's account.

Other screens:

- *Accounting ▸ Vendors ▸ Incoming invoices* — the inbox. Filters for
  unsettled, failed, duplicate, possibly lost and *processing too long*
  records.
- *Accounting ▸ Configuration ▸ AI Invoice Import ▸ Expense Categories*.
- *Accounting ▸ Vendors ▸ Vendor Data Suggestions* — differences between
  invoices and stored vendors, also reachable from the bill (*Review*).
- *Settings ▸ Technical ▸ Partner Identifiers* (developer mode) — the phone
  and web-domain keys vendor matching uses.
- The e-invoicing attention bubble in the systray (from
  `goodahead_einvoice_base`, shared with sending) shows failed, duplicate and
  possibly lost records to Bookkeepers and Administrators, in its *Failed
  imports* block.

Background jobs run with `queue_job`. Give the job runner enough capacity for
your volume (see the `queue_job` documentation).

## Usage

1. Invoices arrive through a source add-on (AI PDF reading, Finvoice upload or
   email, APIX). Each one becomes a record in *Accounting ▸ Vendors ▸ Incoming
   invoices* and, once processed, a **draft** vendor bill.
2. Work the inbox: open *failed* records and *Retry* them, decide *duplicate*
   ones (*Create bill anyway* or *Close without bill*), and pick a vendor for
   bills that arrived without one (or *Create contact from this invoice*).
3. On the bill, review any vendor data suggestions (*Review*), check the
   lines against the document, then post the bill yourself.

## Security

- Accountants (billing rights) see and work import records of the companies
  they are allowed in. A multi-company record rule keeps each company's
  records apart.
- Overriding a duplicate, closing, reopening and creating a vendor from an
  invoice are Accountant actions; nothing is posted automatically. Every bill
  stays a draft.
- Invoice data is untrusted vendor input: it can never create or overwrite a
  vendor.

## Known limitations

- Some technical names keep the old AI wording for compatibility: fields on
  vendor bills and the settings field use an `ai_` prefix, and some system
  parameters keep the `goodahead_invoice_ai_import.*` key (for example
  `goodahead_invoice_ai_import.core_profile` and
  `goodahead_invoice_ai_import.public_email_domains`). Renaming them is planned
  for a later release. For the same reason the import record model, the
  settings block and the configuration menu are still labelled
  *AI Invoice Import*.
- The core recognises two Odoo 19.0 vendor-bill import pipelines and refuses
  to import on a build it does not recognise.

## Documentation

- `docs/architecture.md` — how the core, the sources and the add-ons fit
  together.
- `CHANGELOG.md` — what changed in each version.

## Credits

Part of the shipped list of free mail providers comes from
[willwhite/freemail](https://github.com/willwhite/freemail) (`data/free.txt`),
ISC licence, copyright (c) 2015 Will White.

## Licence

LGPL-3.0 or later. Copyright Goodahead Oy — https://www.goodahead.com/
