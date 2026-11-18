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
     before decoding, so the vendor's PDF is the bill's preview;
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

Odoo 18.0 imports every UBL vendor bill through `account.edi.common`
(`_import_fill_invoice` → `_import_partner`, `_import_partner_bank`,
`_import_invoice_lines` → `_import_product` → `product.product._retrieve_product`). The
module calls this the **legacy** pipeline; newer 19.0 builds replaced it with a
**search-plan** pipeline (`account.edi.ubl._ubl_import_invoice`), which only the 19.0
branch of this module supports.

An override of a method a build never calls is a silent no-op, so
`models/core_profile.py` checks the pipeline at registry load
(`goodahead_invoice_ai_import.core_profile`) and imports are refused with a clear error
on an unrecognised core. `models/account_edi_ubl.py` holds the search-plan hooks; on
18.0 nothing calls them, and the tests gated on that profile are skipped.

| Concern | Hook on 18.0 |
|---|---|
| Vendor keys (business id, phone, domain) | `res.partner._retrieve_partner_with_name` |
| No automatic vendor creation | `account.edi.common._import_partner` guarded for AI imports |
| Vendor product codes | `account.edi.common._import_invoice_lines` puts the vendor on the context; `product.product._retrieve_product` then matches supplierinfo (that vendor, same row) and barcode only |
| Bank account on the payee side | `account.edi.common._import_partner_bank` |

The matching rules themselves live once, on `res.partner`, so the decoder hooks and the
post-decode fallback use the same code.

### Odoo 18.0 differences from the 19.0 branch

- The UBL decoder is returned as a callable by `_get_edi_decoder` and returns `True` when
  it imported the document (19.0 returns a dict and `None`).
- There is no `res.partner.bank.clearing_number`: a US routing number is stored in
  `aba_routing` when `l10n_us` is installed, and otherwise stated on the bill.
- The Peppol endpoint sanitizer is defined in `lib/ubl_generator.py`, since 18.0 core has
  none.
- JSON routes are `type="json"`; ormcache uses the default registry cache.
- With Enterprise OCR (`account_invoice_extract`), AI-imported bills keep
  `extract_state = no_extract_requested`; the module never sends them to OCR, but a
  customisation that later registers a new main attachment on such a bill could.
- The `migrations/19.0.*` folders come from the 19.0 branch. Odoo 18.0 never runs
  them, because their versions are above this module's; they run once when the
  database is upgraded to Odoo 19.0 with the 19.0 branch. They are written to be
  idempotent; test that upgrade on a copy first. `migrations/0.0.0` runs on every
  upgrade.

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
  database can still be a vendor. The check on core's own match
  (`_ai_impossible_vendor_reason`) refuses internal users only, so a vendor with a
  portal login that core matches is kept.

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
   reported. A US routing number is recorded on the vendor's account when `l10n_us`
   is installed, and otherwise stated on the bill.
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
| `models/account_edi_common.py`, `models/product_product.py` | hooks into 18.0's UBL import |
| `models/account_edi_ubl.py` | search-plan hooks; not called on 18.0 |
| `models/core_profile.py` | core pipeline detection; substitute-PDF switch, set only on a core that reads it |
| `models/prompt_profile.py` | extraction prompt profiles |
| `models/vendor_candidate.py`, `models/vendor_data_suggestion.py`, `wizard/vendor_data_review.py` | candidate cards, suggestions queue, review dialog |
| `controllers/main.py`, `static/src/components/failed_imports_menu/` | systray bubble for failed imports |
| `hooks.py`, `migrations/` | identifier seeding, substitute-PDF policy, data migrations |
