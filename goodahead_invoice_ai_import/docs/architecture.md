# Architecture

`goodahead_invoice_ai_import` turns vendor invoice PDFs into draft vendor bills. Claude
reads the PDF and returns structured JSON; the module converts that JSON into a Peppol
BIS Billing 3.0 UBL document and hands it to Odoo's own `account_edi_ubl_cii` decoder,
which builds the bill. The module then post-processes the bill.

```
PDF -> Claude (JSON) -> validation -> Peppol BIS 3.0 UBL -> Odoo UBL decoder -> draft bill -> post-processing
```

**Why a UBL intermediate instead of creating the move directly**

- Odoo's importer already handles partner, tax, bank account and line creation.
- The Peppol schema gives the extraction a precise target.
- The generated XML is kept on the import record as an audit trail.
- XML is generated in Python, not by the model, so namespaces and escaping are always
  valid.

---

## Entry points

Both entry points create an `ai.invoice.import` record and enqueue `_process_import` on
the `root.ai_import` queue_job channel.

1. **Upload.** `account.journal.create_document_from_attachment` is overridden. PDFs
   uploaded into a purchase journal of a company with a Claude API key are queued as AI
   imports. Anything else (non-PDF files such as XML e-invoices, sale journals, companies
   without a key) goes to Odoo's native importer unchanged. Mixed uploads are split.
2. **Email.** `ai.invoice.import` inherits `mail.thread` and implements `message_new`,
   so any mail alias pointed at the model works. One import is created per PDF, up to
   the per-message limit; refused attachments are named on the record. The module ships
   no alias (see [SETUP.md](SETUP.md)).

## Processing

`ai.invoice.import._process_import` (queue job):

1. Skips if the import is already done or linked to a bill.
2. Calls `account.move._ai_extract_and_generate_ubl`, which makes the single paid Claude
   call and generates the UBL XML. Any failure is terminal: the record goes to `failed`
   and the job is not retried, so a document is never billed twice.
3. Calls `_finalize_import`, shared with **Re-import from JSON**:
   - stores the extraction JSON, model name, token counts and the XML attachment;
   - creates a fresh draft move and attaches the vendor's PDF as its main attachment
     before decoding, so core does not render a substitute PDF;
   - runs `account.move._ai_decode_ubl_into_move`, which calls the native UBL decoder
     directly on the move;
   - links the move, then runs `_ai_post_process` under a savepoint.

A failed import keeps its JSON. It can be edited and re-imported with no new API call.
Retry is manual, only from `failed`, and refused once a bill exists.

## Extraction (`lib/claude_client.py`)

`ClaudeInvoiceExtractor` receives an HTTP client from `goodahead_claude`, which owns the
API key, model registry and transport. This module never opens a socket.

**Prompt.** The system prompt is composed of country-neutral procedures (document type,
currency, dates, numbers, contact details) plus one locale block of named slots (tax,
identifiers, bank and payment wording, schema field descriptions):

- A company's **Extraction Prompt** profile (`goodahead.invoice.prompt.profile`) is used
  when set.
- Otherwise the company's country selects the `us` or `eu` block, or the country-neutral
  block for any other country.
- The available expense categories are listed in the prompt so the model picks one per
  line.

**Validation.** `_validate_extraction` checks required fields and types, the document
type, and the arithmetic: lines against the subtotal, tax-exclusive plus tax against
tax-inclusive, payable against tax-inclusive, and per-line magnitude. Tolerance is the
greater of 0.02 and 0.5% (0.02 only for payable vs. tax-inclusive). A missing amount is
an error, never 0.00; a null per-line tax is accepted when tax is printed only as a
document total. Failures raise `ClaudeExtractionError` carrying the JSON, so it is stored
for correction.

**Currency.** The model follows an ordered procedure: a printed ISO code, then an
unambiguous symbol, then other evidence. Ambiguous symbols (`$`, `kr`, `¥`) are never
settled from the buyer's country. When the PDF prints no currency marking at all and
vendor and buyer are in the same country, that country's currency is used and a note is
posted on the bill. Whether anything was printed is decided by `lib/pdf_currency.py` from
the PDF's text layer, form fields and annotations; the model's own report is used only
when the PDF has no text layer.

## UBL generation (`lib/ubl_generator.py`)

`generate_peppol_invoice_xml(data, default_currency=None)` produces an `Invoice` or
`CreditNote` with the BIS 3.0 `CustomizationID` and `ProfileID`, supplier and customer
parties (including phone and email contact), payment means, tax totals and subtotals,
monetary totals and lines (vendor product code as `SellersItemIdentification`).

- Credit notes printed with negative amounts are normalised to positive UBL values.
- `EndpointID` is emitted only when Odoo core maps the country's EAS scheme to the VAT
  number and the value passes core's validation. Other countries get no endpoint, so no
  vendor country can fail an import.
- The default currency is the company's currency.

## Odoo core pipelines

Odoo 19.0 has two vendor-bill import pipelines, depending on the build:

- **search plan** (newer builds): `account.edi.ubl._ubl_import_invoice` with ordered
  partner and product search plans.
- **legacy** (older 19.0 builds): `account.edi.common._import_partner` →
  `res.partner._retrieve_partner`, and `_retrieve_product` for products.

An override of a method that a build never calls is a silent no-op, so
`models/core_profile.py` detects the pipeline at registry load
(`goodahead_invoice_ai_import.core_profile`) and imports are refused with a clear error
on an unrecognised core.

| Concern | search plan | legacy |
|---|---|---|
| Vendor keys (business id, phone, domain) | extra steps in `_import_ubl_retrieve_customer_search_plan` | `res.partner._retrieve_partner_with_name` |
| No automatic vendor creation | `_import_ubl_create_missing_customer` suppressed for AI imports | `account.edi.common._import_partner` guarded for AI imports |
| Vendor product codes | plan narrowed so an unknown vendor code does not fall through to the internal reference | `product.product._get_product_domain_search_order` |
| Bank account on the payee side | `_import_ubl_retrieve_partner_bank` | `_import_partner_bank` |

The matching rules themselves live once, on `res.partner`, so both pipelines and the
post-decode fallback use the same code.

## Vendor matching (`models/res_partner.py`)

`res.partner._ai_find_vendor(seller, company)` never creates a partner. Steps, strongest
first: VAT, business id + country, phone, email domain, website domain, exact name.

- A key that resolves to more than one commercial partner is not a match.
- Phone and web-domain keys come from `goodahead.partner.identifier`, which stores every
  phone and domain a partner has been seen under (add-only). It is seeded from the
  contact book on install/upgrade and kept current by a hook on partner create/write.
- Public mail-provider and placeholder domains never identify a vendor
  (`_PUBLIC_EMAIL_DOMAINS` in `models/partner_identifier.py`, extended by the system
  parameter `goodahead_invoice_ai_import.public_email_domains`). See
  [nordic-mx-evidence.md](nordic-mx-evidence.md) for how the Nordic entries were sourced.
- The bill's own company partner and its contacts, partners linked to a user, and
  contacts belonging solely to another company are refused. Another company of the same
  database can still be a vendor.

Identifier inserts tolerate concurrent imports: a row stored by another transaction
first is skipped instead of failing the import.

## Post-processing (`account.move._ai_post_process`)

1. **Vendor.** If no vendor matched, the bill keeps an empty vendor, a chatter note lists
   what was read, and up to three candidates (`goodahead.vendor.candidate`, ranked by
   posted bills) are shown under the Vendor field with **Use this**. **Create contact from
   this invoice** creates one only on request.
2. **Vendor data.** Empty vendor fields (address, phone, email, website) are filled from
   the invoice and logged. Contradicting values become
   `goodahead.vendor.data.suggestion` rows, reviewed in a dialog from the bill or in the
   suggestions queue. Bank account and tax id are always suggestions, never pre-ticked
   and skipped by bulk apply; the vendor name is never suggested. Applying a suggestion
   also teaches the matcher the vendor's phone and domain.
3. **Expense accounts.** Lines without a product account get the account mapped to their
   expense category (`goodahead.expense.category`, account per company). A missing
   mapping falls back to the journal default with a chatter warning.
4. **Bank details.** The bank account on the bill must belong to the payee. IBANs
   (mod-97) and US routing numbers (ABA checksum) that fail validation are dropped and
   reported; the US routing number is recorded on the vendor's account.
5. **Notes and totals.** Currency-deduction and remit-to notes are posted; the printed
   total and tax are stored (`ai_document_total`, `ai_document_tax`) for the red mismatch
   banner; the extraction JSON is attached with model and token usage.

No approval step is completed automatically; an imported bill is reviewed like any
other.

## Main files

| Path | Role |
|---|---|
| `lib/claude_client.py` | prompt composition, extraction call, validation |
| `lib/ubl_generator.py` | JSON → Peppol BIS 3.0 UBL |
| `lib/pdf_currency.py` | detects whether the PDF prints a currency marking |
| `lib/bank_numbers.py` | IBAN and ABA checksums |
| `models/ai_invoice_import.py` | import record, email intake, queue job, re-import |
| `models/account_journal.py` | upload routing |
| `models/account_move.py` | decode, post-processing, banners, vendor UI |
| `models/res_partner.py` | vendor matcher and refusal rules |
| `models/partner_identifier.py` | phone/domain identifiers, public domain list |
| `models/account_edi_ubl.py`, `models/account_edi_common.py`, `models/product_product.py` | hooks into the two core pipelines |
| `models/core_profile.py` | core pipeline detection, substitute-PDF policy |
| `models/prompt_profile.py` | extraction prompt profiles |
| `models/vendor_candidate.py`, `models/vendor_data_suggestion.py`, `wizard/vendor_data_review.py` | candidate cards, suggestions queue, review dialog |
| `controllers/main.py`, `static/src/components/failed_imports_menu/` | systray bubble for failed imports |
| `hooks.py`, `migrations/` | identifier seeding, substitute-PDF policy, data migrations |
