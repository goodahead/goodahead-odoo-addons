# Invoice Import: Finvoice (`goodahead_invoice_import_finvoice`)

Imports Finvoice e-invoices as draft vendor bills, **without any AI reading**.
A Finvoice file uploaded into a purchase journal, or attached to a mail that
reaches the vendor bill intake, goes through the same rules as every other
source of the invoice import core (`goodahead_invoice_import`): vendor matching
that never creates a vendor, vendor data suggestions, bank-account checks and
the duplicate rule.

- **Versions:** Finvoice 1.3, 2.0, 2.01 and 3.0. A missing or unknown version
  fails with a plain-language reason. Files in ISO-8859-15, files holding
  several messages and messages inside a bank SOAP frame are read.
- **VAT rate from the header:** a message whose rows carry no VAT rate or
  amount, with no VAT breakdown, gets the one Finnish rate its VAT and
  VAT-exclusive totals make to the cent; the bill notes it.
- **One import record per invoice:** a file holding several Finvoice invoices
  gives one record each.
- **Strict vendor matching:** only the seller's VAT number or Business ID sets
  the vendor (a Business ID is recognised also when the sender put it in the
  VAT field). Matches by phone, email or web domain, or name are only offered
  as candidates. When the identifiers point to different vendors, none is set.
- **Credit notes** become draft vendor credit notes with positive amounts,
  whether the sender wrote negative amounts or a credit-note type. A credit
  note naming its original invoice is linked to the vendor's posted bill with
  that reference, when there is exactly one.
- **Row totals are the sender's:** each bill line carries its row's stated
  total to the cent. A price per 100 pieces the sender does not declare is no
  problem; a total that is not quantity × a storable price becomes one unit
  at that total, with the sender's quantity and price in the line text.
- **Sublines (SubInvoiceRow)** are left out as headings and subtotals, unless
  the stated total includes them (a credit for an earlier payment): then each
  becomes a line.
- **A message that does not add up** (rows that don't make the stated total)
  becomes a draft bill with the rows as they are and the red total banner, for
  an Accountant to review before posting. No line is added to make it fit.
- **Attachments:** the Finvoice message is the bill's main document unless a
  PDF image came with it; other files are attached to the bill.
- **Bank accounts:** an account the vendor does not hold yet is never created;
  it becomes a suggestion. An IBAN that belongs to another partner is not
  attached, and the chatter names its owner.
- Messages that are not invoices (orders, quotations, pro forma) fail with
  their own reason.

The Finvoice parser and mapping are pure Python and written from the Finvoice
3.0 implementation guidelines (version 2.6) published by Finance Finland. They
live in `goodahead_einvoice_base` (`lib/finvoice/`, with the Finvoice 3.0
schema), shared with sending; this add-on's `lib/finvoice/` re-exports them.

## Which add-ons to install

| You need | Install | What comes with it |
|---|---|---|
| PDF invoices read with AI | `goodahead_invoice_ai_import` | `goodahead_invoice_import`, `goodahead_claude` |
| Finvoice files by upload or email | `goodahead_invoice_import_finvoice` | `goodahead_invoice_import`, `goodahead_einvoice_base` |
| APIX e-invoices | `goodahead_invoice_import_apix` | `goodahead_invoice_import`, this add-on, `goodahead_apix` |
| Moving a company over from an earlier APIX connector (e.g. the community APIX modules) | `goodahead_invoice_import_apix_migration` | its adapter installs itself next to the connector and brings the migration tool (community: `goodahead_invoice_import_apix_migration_community`) |

This add-on works with or without the AI add-on. With both installed, Finvoice
files come here and PDFs go to the AI, in whichever order they were installed.
Without the AI add-on, a file that is not Finvoice gives a failed import
record saying the format is not supported.

## Install

Put the add-on and its dependencies (`goodahead_invoice_import`,
`goodahead_einvoice_base`, `queue_job`) on the add-ons path and install
*Invoice Import: Finvoice* from Apps; the core comes with it.

## Configuration

Nothing specific. To receive Finvoice by mail, point a mail alias at the import
record model (see the AI add-on's README for the per-company alias). Finvoice
files can also be uploaded from the vendor bills list of a purchase journal.

Totals are checked within the platform's tolerance (the larger of 0.02 and
0.5 % of the larger compared amount; 0.02 for the amount to pay). An invoice
that fails validation keeps its file and data on the import record; an
Accountant can retry it from there without uploading again.

### Limits

Anybody can mail the import alias, and every invoice in a file becomes an
import record and a job. Over a limit the file (or the whole email) becomes
one *failed* import record that says why, and no job. System parameters
(*Settings ▸ Technical ▸ System Parameters*); `0` switches a limit off:

| Parameter | Default | Meaning |
|---|---|---|
| `goodahead_invoice_import_finvoice.max_messages_per_file` | 500 | Most invoices imported from one Finvoice file |
| `goodahead_invoice_import_finvoice.max_files_per_email` | 20 | Most Finvoice files imported from one email |
| `goodahead_invoice_import_finvoice.max_file_bytes` | 20971520 (20 MiB) | Largest Finvoice file imported |

The files that come with a multi-invoice file (images, delivery notes) are
stored once and shared by its invoices' records.

## Usage

1. Upload a Finvoice XML file from the vendor bills list of a purchase journal,
   or send it to the import mail alias.
2. The file gets an import record in *Accounting ▸ Vendors ▸ Incoming
   invoices* and, per invoice in it, a **draft** vendor bill (or credit note).
3. Review the bill (vendor, lines, any red total banner or vendor data
   suggestions) and post it yourself. A failed record shows its reason and can
   be retried from the record.

## Security

A Finvoice file is untrusted vendor input, like any document: it only creates
draft bills, passes the duplicate rule, and can never create or overwrite a
vendor. The parser does not load external entities, refuses a file above
its size limit before reading it, and reads in time proportional to the
file's size. For a file that arrived by email, the seller's email, phone and
website wait for a person as suggestions.

## Known limitations

- Only Finvoice 3.0 is checked against the official schema (in tests, and as a
  warning). Versions 2.0 and 2.01 are read by the same mapping but are not
  schema-checked.
- Scanned paper invoices delivered as Finvoice are not told apart from real
  e-invoices; they are treated the same, and bank account and identifier
  differences only ever become suggestions.

## Licence

LGPL-3.0 or later. Copyright Goodahead Oy — https://www.goodahead.com/
