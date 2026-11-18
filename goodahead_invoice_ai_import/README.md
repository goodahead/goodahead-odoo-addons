# AI Invoice Import

`goodahead_invoice_ai_import` turns vendor bill PDFs into draft vendor bills in Odoo
18.0. Claude (Anthropic) reads the PDF. The module checks the result and converts it to
a Peppol BIS Billing 3.0 UBL document, which Odoo's own UBL importer turns into a bill.

```
PDF (upload or email) -> Claude -> checks -> Peppol UBL -> Odoo UBL importer -> draft bill -> post-processing
```

The bill is always a **draft**. Nothing is posted, paid or approved automatically.

- [Features](#features)
- [Product lookup from vendor pricelists](#product-lookup-from-vendor-pricelists)
- [Setup](#setup)
- [Who sees what](#who-sees-what)
- [Further reading](#further-reading)

---

## Features

### Getting PDFs in

- **Upload.** PDFs uploaded on the Vendor Bills dashboard (**Upload**) into a purchase
  journal are queued as AI imports. XML e-invoices, uploads into sale journals and
  companies without a Claude API key keep using Odoo's native importer. A mixed upload
  is split between the two.
- **Email.** Point any mail alias at the **AI Invoice Import** model. Each PDF in a
  message becomes its own import, up to 10 PDFs per message.
- **Background processing.** Each import runs as a `queue_job` job on its own channel
  (`root.ai_import`), so a batch of PDFs does not block other work.
- **Import records.** Every PDF gets an **AI Invoice Import** record showing its state
  (processing, done, failed), the bill, the model used, token counts, the extraction
  JSON and the generated UBL XML.

### Reading the document

- **One Claude call per PDF.** A failed import is never retried automatically, so a
  document is never paid for twice.
- **What is read:**
  - document type: invoice or credit note;
  - number, dates and payment reference;
  - currency;
  - the vendor's name, VAT number, business ID, address, phone, email and website;
  - bank details: IBAN/BIC, or US routing and account number;
  - every line: description, vendor product code, quantity, unit, price, tax and
    expense category;
  - the printed totals.
- **Country-aware prompt.** The prompt has a United States block, a European Union
  block, or a country-neutral block, chosen from the company's country. Dates and
  numbers are read in the vendor's local format.
- **Your own prompt wording (optional).** Duplicate a built-in **Extraction Prompt**
  profile, edit the copy and select it for the company.
- **Currency.** The currency comes from a printed ISO code, then an unambiguous symbol,
  then other evidence. An ambiguous symbol (`$`, `kr`, `¥`) is never settled from the
  buyer's country. Only when the PDF prints no currency at all and vendor and buyer are
  in the same country is that country's currency used, with a note on the bill.

### Checks before a bill is created

- **Arithmetic.** The lines must add up to the subtotal, subtotal plus tax to the total,
  and the total must match the amount payable.
- **No silent zeros.** An amount that could not be read stops the import; it is never
  written as 0.00.
- **Bank numbers.** IBANs are checked with mod-97 and US routing numbers with the ABA
  checksum. A number that fails is dropped and the bill says so.
- **Fix and re-import, free.** A failed import keeps its JSON. Correct the one wrong
  value and click **Re-import from JSON**; no new Claude call is made.

### Vendor matching

- **Order of keys:** VAT number, then business ID + country, phone, email domain,
  website domain, and finally the exact name.
- **Ambiguous keys are ignored.** A key shared by more than one company is not used.
  Public mail providers (gmail.com, outlook.com and many Nordic providers) never
  identify a vendor. You can add your own domains to that list.
- **The matcher learns.** Every phone number and web domain a vendor has been seen
  under is remembered. **Settings > Technical > Partner Identifiers** lists them.
- **Never your own company.** Your own company, its contacts and internal users are
  never chosen as the vendor. A vendor with a portal login can still be matched by
  Odoo's importer (VAT, Peppol ID, phone or email); the module's own keys and the
  candidate list skip any partner that has a user login.
- **No vendor is created automatically.** An unmatched bill keeps an empty vendor and
  shows:
  - up to three candidates, ranked by posted bills, each with a **Use this** button;
  - **Create contact from this invoice**, which creates the contact only when clicked.
- **Posted-bill counter.** The bill shows how many posted and draft bills the chosen
  vendor already has, with a warning when it has none.

### Keeping vendor data current

- **Empty fields are filled.** Address, phone, email and website missing on the vendor
  are filled from the invoice and logged in the chatter.
- **Conflicts become suggestions.** A value that differs from what the vendor already
  has becomes a **Vendor Data Suggestion**. Review it from the bill or from the
  suggestions queue.
- **Never applied automatically:**
  - bank account and tax ID: always suggestions, applied one at a time;
  - the vendor name: never suggested.

### Lines and accounts

- **Product lookup** from the vendor's pricelist rows; see the
  [next section](#product-lookup-from-vendor-pricelists).
- **Expense categories.** Claude picks an expense category for each line (for example
  freight, software, office supplies). Each category maps to an account per company.
  A line without a product account gets its category's account. A missing mapping
  falls back to the journal's default account, and the chatter names the line and the
  company.
- **The document's own lines.** The bill carries exactly the lines on the PDF. No extra
  rounding or tax line is added.

### Bank details

- **Only the vendor's account.** The account on the bill must belong to the vendor.
- **Never on your own company.** A vendor's IBAN is never recorded on your own company
  or on a user. This also applies to genuine UBL, Factur-X and Peppol bills.
- **Credit notes.** On a received credit note, the IBAN is recorded on the vendor, not
  on your company.
- **US routing numbers** are stored on the vendor's bank account when `l10n_us` is
  installed; otherwise they are stated on the bill once.

### Review aids on the bill

- **Total mismatch banner.** The printed total and tax are stored on the bill. While
  the bill differs from them by more than 0.02, a red banner shows both figures. It
  clears itself once the bill matches and does not block posting.
- **Chatter notes** cover:
  - what was read off the document;
  - currency deductions and remit-to addresses;
  - fallbacks to default accounts;
  - the model and token usage, with the extraction JSON attached.
- **The vendor's own PDF** is the bill's main attachment and preview.

### Failures

- **Failed-imports bubble.** Failed imports are counted in a systray bubble.
- **Manual retry.** Retry is only possible from the failed state, and is refused once
  a bill exists.
- **Unrecognised Odoo build.** On an Odoo build whose import pipeline the module does
  not recognise, imports are refused with a clear error. They are not half-processed.

### Multi-company

- **One alias per company.** Each company that receives bills by email gets its own
  alias. The alias's default values put the import in the right company.
- **Shared categories.** Expense categories are shared; their accounts are set per
  company.
- **Sister companies.** Another company in the same database can still be a vendor.

---

## Product lookup from vendor pricelists

Vendors print their **own** article numbers, not yours. On AI-imported bills the module
therefore matches products through the vendor pricelist (`product.supplierinfo`, the
**Vendors** list on a product's **Purchase** tab, or **Purchase > Configuration > Vendor
Pricelists**).

### How a line is matched

Claude reads the vendor's article/SKU code from each line. The code is matched as
follows:

1. **Known vendor:** a product is found when one of its pricelist rows has that
   **vendor** *and* that **Vendor Product Code**, on the same row. A product with rows
   (Vendor A, `111`) and (Vendor B, `XXX`) does not match Vendor A quoting `XXX`.
2. **Barcode:** a barcode (GTIN) carried in the document matches a product's
   barcode, with or without a known vendor, since a GTIN is global. The AI extraction
   reads one code per line and does not emit it as a barcode, so in practice step 1 is
   what matches.
3. **Nothing else.** Your own **Internal Reference** and the product **name** are not
   used. Vendor codes and your codes are different namespaces: vendor code `1001` must
   not become your product `1001`.
4. **No match:** the line is created without a product, with the document's
   description, and gets its account from its expense category.

**Unknown vendor:** when the bill has no vendor (none matched), only a barcode can
match. Pick the vendor and the lines can be matched on the next import.

**Companies:** your company's own products are tried first, then products shared
between companies, as in core.

### Setting it up

For each product you buy from a vendor:

1. Open the product, **Purchase** tab, and add a line under **Vendors**:
   - **Vendor:** the vendor (the company, not a contact person);
   - **Vendor Product Code:** the code exactly as it is printed on that vendor's
     invoices.
2. Or import the rows in bulk under **Purchase > Configuration > Vendor Pricelists**
   (columns: Vendor, Product, Vendor Product Code).

One row fixes every later bill from that vendor. The import never changes pricelist
rows: price and quantity on the bill come from the document.

Editing pricelist rows in the UI needs the **Purchase** app. The lookup itself works
without it.

### Limitations

- **Variants.** Matching is done per product template. If several variants share a
  template, the first variant is taken. Variant-specific rows are not distinguished.
- **Non-AI bills.** Genuine e-invoices (UBL, Factur-X, Peppol) are not affected; they
  keep Odoo's own product matching.

---

## Setup

Each step is explained in more detail, with the reasons, in [docs/SETUP.md](docs/SETUP.md).

1. **Install the dependencies.**
   - Odoo 18.0. Community is enough.
   - [OCA/queue](https://github.com/OCA/queue) `queue_job`.
   - `goodahead_claude` from this repository.
   - Python `requests`.
   - For US vendors, `l10n_us`, to store routing numbers.
2. **Run `queue_job`.** Load it server-wide (`--load=base,web,queue_job`) with at least
   one worker. Limit the AI channel in `odoo.conf`:
   ```
   [queue_job]
   channels = root:4,root.ai_import:1
   ```
   Without a running worker, imports stay in `processing`.
3. **Install** `goodahead_invoice_ai_import`.
4. **Claude API key.** Under **Settings > General Settings > Claude Connection**:
   1. Enter the key from [Anthropic](https://www.anthropic.com/api).
   2. Click **Test Connection**.
   3. Click **Sync Models**.
5. **Extraction model.** Under **Settings > Accounting > AI Invoice Import**, choose a
   capable model in **Extraction Model**. Only PDF-capable models are listed. Left
   empty, the company's **Default Model** (Claude Connection) is used; if that is empty
   too, the most recently released PDF-capable model, which may be a small one.
6. **Expense category accounts.** For **each company**: switch into it, open **Settings
   > Accounting > AI Invoice Import > Configure Expense Categories**, and set an account
   on each category.
7. **Vendor pricelist codes** for the products you want matched on bill lines; see
   [Setting it up](#setting-it-up).
8. **Email alias (optional).** Create it under **Settings > Technical > Email >
   Aliases**:
   - **Aliased model:** AI Invoice Import;
   - **Default values:** `{'company_id': <company id>}`;
   - one alias per company.

   Check the alias domain before giving the address to vendors.
9. **Optional settings** under **Settings > Accounting > AI Invoice Import**:
   - **Extraction Prompt**: your own prompt profile;
   - **Domains That Never Identify a Vendor**: extra email domains to ignore.
10. **Try it.** Upload a vendor PDF on the Vendor Bills dashboard. The upload opens an
    **AI Invoice Import** record. After about a minute it is `done` and links the
    draft bill.

### Limits enforced in code

| Limit | Default | System parameter |
|---|---|---|
| PDFs accepted from one email | 10 | `goodahead_invoice_ai_import.max_pdfs_per_message` |
| Largest PDF sent to Claude | 15 MB | `goodahead_invoice_ai_import.max_pdf_bytes` |

---

## Who sees what

- **Menus.** **Accounting > Vendors > AI Invoice Imports**, **Accounting > Vendors >
  Vendor Data Suggestions**, **Accounting > Configuration > AI Invoice Import** and the
  failed-imports bubble are for:
  - Bookkeepers (`account.group_account_user`);
  - accounting Administrators (`account.group_account_manager`).
- **18.0 Community.** There, `account.group_account_user` is the hidden "Show Full
  Accounting Features" group, so in practice Administrators see these menus.
- **Settings.** The Claude API key and model settings, and **Settings > Technical >
  Partner Identifiers**, need Settings access.

---

## Further reading

- [docs/SETUP.md](docs/SETUP.md): setup details and why the module ships no alias.
- [docs/architecture.md](docs/architecture.md): how the import works inside, and the
  Odoo 18.0 differences from the 19.0 branch.
- [CHANGELOG.md](CHANGELOG.md): release notes.

The tests never call the Claude API:

```
odoo -d <test-db> -i goodahead_invoice_ai_import --test-tags /goodahead_invoice_ai_import --stop-after-init
```

License: LGPL-3.0 or later. Author: Goodahead Oy.
