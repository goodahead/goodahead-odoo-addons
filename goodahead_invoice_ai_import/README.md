# AI Invoice Import (`goodahead_invoice_ai_import`)

Reads vendor invoice PDFs with Claude AI and hands the result to the invoice
import core (`goodahead_invoice_import`), which turns it into a draft vendor
bill on the right existing vendor.

Since 18.0.4.x (19.0.4.0.0 on Odoo 19.0) this add-on holds **only the AI reading**. Everything else —
import records, the inbox, vendor matching, vendor data suggestions, bank
checks, the duplicate rule, expense categories and bill decoding — lives in the
core and is shared with the other sources (Finvoice, APIX).

What stays here:

- the Claude extraction call and its key check (the API key and the model
  registry come from `goodahead_claude`);
- PDF intake: PDFs uploaded into a purchase journal and PDFs in mails that
  reach the import record's mail alias (one import record per PDF);
- extraction prompts (*Extraction Prompts*: built-in United States and
  European Union profiles, and editable copies) and the per-company choice of
  model and prompt;
- token usage on each import record, and the `root.ai_import` job channel.

## Which add-ons to install

| You need | Install | What comes with it |
|---|---|---|
| PDF invoices read with AI | `goodahead_invoice_ai_import` | `goodahead_invoice_import`, `goodahead_claude` |
| Finvoice files by upload or email | `goodahead_invoice_import_finvoice` | `goodahead_invoice_import` |
| APIX e-invoices | `goodahead_invoice_import_apix` | `goodahead_invoice_import`, `goodahead_invoice_import_finvoice`, `goodahead_apix` |
| Moving a company over from an earlier APIX connector (e.g. the community APIX modules) | `goodahead_invoice_import_apix_migration` | its adapter installs itself next to the connector and brings the migration tool (community: `goodahead_invoice_import_apix_migration_community`) |

With the Finvoice add-on installed as well, Finvoice files go to it (no AI
call, no AI cost) and PDFs come here, whichever order the add-ons were
installed in.

## Upgrading from 18.0.3.x

Run `-u goodahead_invoice_ai_import`. It installs `goodahead_invoice_import`
first and renames the import record model `ai.invoice.import` to
`goodahead.invoice.import` in place: settings, the mail alias, the import
history, failed imports, messages, attachments, activities and followers are
kept.

- **The upgrade refuses while import jobs are queued or running.** Let them
  finish (or cancel them) and run it again. Nothing is changed by a refused
  upgrade.
- Installing `goodahead_invoice_import` on its own on such a database is
  refused; upgrade this add-on instead.
- **Take a backup first.** There is no automatic downgrade; going back means
  restoring the backup.
- **One deliberate behaviour change:** a PDF whose vendor and invoice number
  already exist as a bill is now **stopped as a duplicate** (no bill, a link to
  the existing one) instead of becoming a second draft bill with a warning. An
  Accountant can still *Create bill anyway*.
- A mail alias that pointed at the old model keeps working; it points at the
  renamed model after the upgrade.

## Configuration

1. **Claude API key and models** — set up `goodahead_claude` (API key,
   fetched model list).
2. *Accounting ▸ Configuration ▸ Settings ▸ AI Invoice Import*:
   - **Extraction Model** — empty picks a model from the fetched registry.
   - **Extraction Prompt** — empty picks the profile from the company's
     country (United States, European Union, or a country-neutral fallback).
   - The core's settings (domains that never identify a vendor, expense
     categories) are in the same block.
3. **Mail intake** — the add-on ships no mail alias. Create one per company
   (*Settings ▸ Technical ▸ Email ▸ Aliases*) on the model *AI Invoice Import*
   (`goodahead.invoice.import`; the display name is kept for compatibility) with default values `{'company_id': <id>}`, so
   each mail lands in the right company.
4. **Job capacity** — the `root.ai_import` channel capacity is a `queue_job`
   deploy setting.

### Limits

Every reading is a paid Claude call, and anybody who knows the alias can send
mail to it. The limits below apply wherever a reading is queued (mail,
upload on a purchase journal, retry). A refused PDF becomes a *failed* import
record that says which limit refused it, and no job; *Retry* it once the limit
allows. All are system parameters (*Settings ▸ Technical ▸ System
Parameters*, prefix `goodahead_invoice_ai_import.`); `0` switches a limit off.

| Parameter | Default | Meaning |
|---|---|---|
| `max_pdfs_per_message` | 10 | PDFs one email may send for reading |
| `max_pdf_bytes` | 15728640 (15 MiB) | Largest PDF sent for reading |
| `max_pdf_pages` | 50 | Most pages of a PDF sent for reading |
| `daily_reads_per_company` | 1000 | Readings one company may queue in 24 hours |
| `daily_reads_per_sender` | 200 | Readings one sender may queue in 24 hours (the mail's sender address, or the user who uploads) |
| `recent_read_days` | 30 | A PDF already read in this many days is not read again; open the earlier import instead |

An email over the daily budget becomes one failed record for the whole mail.

## Security

- The Claude API key belongs to `goodahead_claude` and is readable by system
  administrators only.
- Every bill stays a draft. The AI reading never creates or overwrites a
  vendor; differences become vendor data suggestions.
- Each PDF costs one Claude call. A failed import keeps the extraction, and
  *Re-import from JSON* rebuilds the bill without a new call.

## Known limitations

- Fields on vendor bills keep their `ai_` prefix and some system parameters
  keep the `goodahead_invoice_ai_import.*` key, also where they now belong to
  the core. Kept for compatibility; renaming is planned for a later release.
- Uninstalling this add-on keeps the import history and vendor data (they
  belong to the core); only the token-usage columns are removed.

## Documentation

- `docs/architecture.md` — how the AI source plugs into the core. The core's
  `docs/architecture.md` describes the platform.
- `docs/SETUP.md` — setting up mail intake, models and job capacity on a real
  instance.
- `CHANGELOG.md` — every version.

## Licence

LGPL-3.0 or later. Copyright Goodahead Oy — https://www.goodahead.com/
