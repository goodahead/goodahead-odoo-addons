# Architecture — the AI source

Since 19.0.4.0.0 this add-on is one **source** of the invoice import platform.
The platform — import records, the lifecycle, vendor matching, the duplicate
rule, vendor data suggestions, bank checks, expense categories and bill
decoding — is described in `goodahead_invoice_import/docs/architecture.md`.
This page covers only what the AI source adds.

## Flow

```
PDF uploaded into a purchase journal ─┐
                                      ├─> goodahead.invoice.import (reader "ai", state waiting)
PDF in a mail to the import alias ────┘            │  queue_job, channel root.ai_import
                                                   v
                         core job (_process_import) asks the "ai" reader to read
                                                   │
                     Claude reads the PDF -> invoice data (JSON) + token usage
                                                   │
                     core: validate -> duplicate rule -> Peppol BIS 3.0 XML
                           -> Odoo's account_edi_ubl_cii decoder on a fresh draft move
                           -> post-processing (expense accounts, bank checks,
                              vendor data suggestions, chatter)
```

## How it plugs into the core

- **Reader.** `_import_readers()` registers `ai` with priority 100 and the
  default matching policy (VAT number, Business ID, phone, web domain and name
  may all set the vendor; the first match wins). Finvoice registers priority
  10, so with both installed a Finvoice file never reaches the AI.
- **Mail.** `_source_accepts_attachment` takes `.pdf` files;
  `_source_receive_mail` creates one import record per PDF, within the
  per-message cap (`goodahead_invoice_ai_import.max_pdfs_per_message`, default
  10) and the size limit (`goodahead_invoice_ai_import.max_pdf_bytes`, default
  15 MiB).
- **Upload.** `account.journal.create_document_from_attachment` routes PDFs
  into a purchase journal to the queue when the company has a Claude key;
  anything else falls through to Odoo's own import.
- **Reading.** `_source_read` makes the single paid Claude call and finishes
  the import through the core. A failure after the call is terminal for the
  job (it is not retried, so a PDF is never billed twice by Claude); the
  extraction is kept and *Re-import from JSON* rebuilds the bill without a new
  call.

## The extraction

- The client (`lib/claude_client.py`) builds the prompt, frames the PDF and
  checks the stop reason. HTTP, the API key and the model registry belong to
  `goodahead_claude`.
- **Prompts** are a collection (`goodahead.invoice.prompt.profile`): built-in
  United States and European Union profiles composed from code, editable
  copies, and a country-neutral fallback. Each company may pick a profile;
  empty picks by the company's country.
- The output keys are the platform's invoice data; validation is the core's
  (`goodahead_invoice_import/lib/invoice_data.py`, re-exported from
  `lib/claude_client.py` for compatibility).
- Token usage and the model used are stored on the import record (fields owned
  by this add-on).

## What changed with the split

- The import record model was renamed from `ai.invoice.import` to
  `goodahead.invoice.import` in place (*Rename the import record model in place
  during the upgrade*).
- The core now owns `message_new` and the import job; this add-on no longer
  overrides them (*Split the import into a core and four source or connection
  add-ons*).
- A PDF whose vendor and invoice number already exist as a bill is stopped as a
  duplicate instead of becoming a second draft (*Enforce duplicate invoices in
  the core before any bill*).
