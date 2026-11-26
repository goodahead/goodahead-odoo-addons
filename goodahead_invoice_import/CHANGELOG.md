# `goodahead_invoice_import` — changelog

The shared import core, split out of `goodahead_invoice_ai_import` 19.0.3.33.0.
Newest first. Test counts are the full gate of every add-on of the platform
that was installed at that version.

**Release gate (18.0):** 2,043 tests, 0 failed.

---

18.0.1.12.4 — release clean-up: no functional change.

- Credit for the willwhite/freemail list (ISC) in the code and the README; comments no longer name an unpublished add-on; README gains a Usage section.
  Upgrade: `-u goodahead_invoice_import`.

---

18.0.1.12.3 — release clean-up: invented test data, no functional change.

- Comments, docstrings and tests no longer carry data from real documents
  or databases; test documents are invented, same shapes.
  Upgrade: `-u goodahead_invoice_import`.

---

18.0.1.12.2 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_import`.

---

18.0.1.12.1 — first 18.0 release of 19.0.1.9.1 to 19.0.1.12.1 (listed below), for Odoo 18.0.

- 18.0: incoming units through the shared unit codes work on both 18.0
  builds: before April 2026 through `account.edi.common._retrieve_line_vals`
  (the notes join the import's logs), later through the same hook as 19.0.
  A unit fits a product when it is of the product's unit category (19.0: a
  common reference unit).
- The failed-imports bubble is the base's shared bubble, as on 19.0.
- Upgrade: `-u goodahead_invoice_import,goodahead_invoice_import_finvoice`
  (installs `goodahead_einvoice_base`).

19.0.1.12.1 — discounted rows always make their stated amount.

- A kept discount is checked at the price the bill stores (Product Price
  digits): a gross price stated in five decimals (0.79839) is stored as
  0.80, which on 1000 units missed the stated amount by 1.53. A row that
  cannot be made that way goes like any other stated row — at its net
  price, or as one unit at its amount with the sender's quantity × price
  in the line text — also in the after-decode check, which no longer
  writes amount ÷ quantity.
- That after-decode check keeps the sign of a negative row.
- Tests: every Finvoice fixture without a discount gives the same bill from
  the reader output captured before the content feature as from today's.

---

19.0.1.12.0 — references and discounted rows from Finvoice bills.

- The import record shows the buyer's order number and buyer reference a
  Finvoice bill states; the draft bill gets them as its source document and
  buyer reference (its own reference stays the vendor's invoice number; no
  purchase order is looked up). Records imported earlier keep empty values.
- A discounted row keeps its price before discount and the stated (or
  worked-out) percentage when that makes the stated row amount at the
  ERP's discount precision; otherwise, or when the decoded line still misses
  the amount, the line goes at its net price as before. Rows without a
  discount, and AI- or UBL-read bills, import exactly as before.

---

19.0.1.11.0 — incoming units through the shared unit codes.

- A structured reader's bill lines (Finvoice) read their unit through
  `uom.uom.rec20_code`, the code an Administrator edits under Unit codes and
  sending writes: a changed code is read as that unit, `H87` and `kpl` as
  the standard piece. A code no unit carries gives the company's default
  unit for unmapped incoming codes, or no unit, and the bill's import note
  says "Unit code X is not mapped — … used". Never a guessed unit.
- `goodahead.invoice.import._import_decode_context()` puts
  `einvoice_unit_codes` on the decode for a structured reader; the override
  of `account.edi.ubl._import_ubl_invoice_retrieve_product_uoms` acts only
  then. AI-read bills keep the ERP's own unit lookup.
- The generated document carries a line's unmapped code as written
  (`unit_code_unmapped`) instead of a silent `C62`.
- Tests: 11 new (reader normalisation, changed / standard / unmapped codes,
  default unit or none, AI path unchanged).

---

19.0.1.10.0 — failed imports in the shared e-invoicing attention bubble.

- The systray bubble moved to `goodahead_einvoice_base` (a new dependency,
  installed automatically on upgrade). This add-on contributes its
  `failed_imports` section: the same records (failed, duplicate, possibly
  lost, of the user's active companies) for the same people (Bookkeeper or
  accounting Administrator) as before. Its block is headed *Failed imports*;
  *Show all* still opens *Incoming invoices needing attention*.
- `/goodahead_invoice_import/failed_imports` answers as before for one more
  release; the bubble no longer calls it.
- Upgrade: `-u goodahead_invoice_import`.

---

19.0.1.9.2 — an import job runs in its record's company.

- The import job is queued in the company of the import record, whatever
  company the user who started it (a retry, a duplicate override) had
  active. Before, with several companies active, the job could read the
  invoice with the other company's settings.

19.0.1.9.1 — the bill's references in the import job's result.

- The result of an import job names the bill it made with its vendor, invoice
  number and total ("OK: Created … · vendor · No. … · amount"), so the job
  list can be searched by them.

18.0.1.9.0 — documents with stated amounts: a draft with the banner, no Rounding line (same as 19.0.1.9.0).

- New `source_meta.amounts_stated`: a source whose every amount is stated by
  the document (a Finvoice message) and none read off a page. For such data:
  - lines that do not make the stated totals, or tax that does not, no longer
    fail the import: the draft bill carries the lines as they are and the red
    banner compares its total with the stated one;
  - the 100× check on quantity × price against the line total (made for a
    misread decimal point) is not applied: a price per 100 pieces that the
    sender does not declare is imported from its stated row total;
  - the bill's document is generated so the ERP's e-invoice reader adds no
    untaxed *Rounding* line: a line whose total is not its quantity times a
    price the ERP can store (7 h for 120.00) is booked as one unit at its
    total, with the sender's quantity and price in the line text; a document
    whose lines do not make its totals gets the lines' own totals and VAT
    breakdown. The stored invoice data is unchanged.
- AI readings are unchanged.
- Upgrade: `-u goodahead_invoice_import`.

18.0.1.8.0 — first release for Odoo 18.0, with the features of 19.0.1.8.0
(listed below).

- Carries every 18.0 adaptation of `goodahead_invoice_ai_import` 18.0.3.34.0 that
  belongs to the core: the `legacy` and `search_plan_18` core profiles; the
  generated UBL decoded through 18.0's decoder contract; no vendor created and no
  impossible vendor matched in `account.edi.common._import_partner`; a vendor's
  article code matched only against that vendor's supplierinfo rows (then barcode),
  in `product.product._retrieve_product` and, on `search_plan_18`, through this
  add-on's own supplierinfo search-plan step; a stray copy of a vendor's IBAN on
  our own company partner no longer strips the account off a bill; US routing
  numbers stored in `aba_routing` (with `l10n_us`) or stated on the bill; a tax id
  that `base_vat` refuses no longer blocks *Create contact*; a vendor with a portal
  login is accepted; Peppol endpoints follow the per-scheme format rules with a
  sanitizer of this add-on's own (18.0 ships none); 18.0's substitute-PDF switch is
  read where 18.0 reads it.
- The source matching policy is applied in `_import_partner` and, under a strict
  policy (`bank_account_lookup_only`, every structured source), the bill's bank
  account is only looked up on the vendor in `_import_partner_bank`, never created
  or re-activated. 18.0 builds before April 2026 have no
  `res.partner.bank._find_or_create_bank_account`, which is where 19.0 enforces it.
- Searching on the shown status (*Processing*) and on "processing for more than
  30 minutes" accepts `=`, `!=`, `in` and `not in`; 18.0 passes the operator
  through where 19.0 normalises it.
- 18.0 APIs: unique constraints as `_sql_constraints` (same constraint names),
  plain list domains, the default ormcache, a `json` route for the systray, and
  `self.env._` where `_()` cannot find the language (generator expressions). Chatter
  notes with the extracted data are posted with `no_new_invoice`, so 18.0 does not
  try to decode the attachment into the bill.
- Install: arrives with `-u goodahead_invoice_ai_import` from 18.0.3.34.0 (see that
  add-on's changelog), or install it with a source add-on.

19.0.1.8.0 — review fixes: company isolation, duplicate rule, re-imports.

- **Company isolation:** vendor-data suggestions and vendor candidates carry
  their bill's company (stored, filled for existing rows on upgrade) and a
  user sees only those of the companies they are allowed in. A partner
  identifier (phone, web domain) is readable only with its partner: shared
  contacts for everybody, a company's own contacts for that company.
- **Duplicate rule:** the seller's VAT number is stored and compared without
  spaces and case on both sides, so `FI 2345 6789` on a contact matches
  `FI23456789` on the invoice. Migration `19.0.1.8.0` normalises the stored
  VAT of existing import records.
- **Create bill anyway** on a duplicate PDF read by the AI builds the bill
  from the invoice data already stored — no second paid reading. A
  structured source's record is still read again.
- **Strict matching policy:** a vendor is offered by name only when its name
  is the printed name (case aside), as intended; the AI's suggestions
  are unchanged.
- **Retry** is offered on an interrupted import too, and its banner no
  longer promises an automatic retry.
- The systray list title is translatable.
- `lib/` imports nothing from Odoo: the UBL generator moved to
  `adapters/ubl_generator.py` (import it from there). The bill builder's
  create-and-decode step is its own method, `_import_create_and_decode`.
- The bill tests no longer skip on a database without a purchase journal:
  they load the generic chart of accounts (`tests/common.py`).
- Upgrade: `-u goodahead_invoice_import`.

19.0.1.7.0 — vendor auto-fill respects contact rights.

- An invoicing-only user (no *Contact Creation*) importing a bill whose vendor
  has empty fields no longer gets the import marked failed although the bill
  was created. Without write access to the contact, the empty-field fills
  become vendor-data suggestions for someone who may apply them; nothing is
  written with elevated rights.
- Upgrade: `-u goodahead_invoice_import`. Tests: 997 with every add-on
  installed; 103 with the community modules and the bridge.

19.0.1.6.0 — the inbox and the import record screens.

- **Inbox *Incoming invoices*** under Accounting ▸ Vendors (the menu and action
  keep their external ids): status badge, source, reader, seller, invoice
  number, amount and document date. Filters: unsettled, failed, duplicate,
  possibly lost, processing too long.
- **Import record form:** one banner per status, and only the buttons that
  status allows (Retry, Create bill anyway, Import, Close without bill,
  Reopen); buttons to the bill and to the existing bill of a duplicate; who
  closed a record, when and why.
- The systray counter counts failed, duplicate and possibly lost records.
- *Create contact from this invoice* and a candidate's *Use this* follow the
  import's matching policy, so an e-invoice bill never gets a vendor that only
  shares a phone, a web domain or a name.
- Seller, invoice number, amount and date are stored columns filled from the
  invoice data (APIX records fall back to the listing).
- Upgrade: `-u goodahead_invoice_import`. Tests: 973.

19.0.1.5.0 — strict bank-account rule in the core.

- Under the strict matching policy (every e-invoice source: APIX, Finvoice by
  upload or email) the decoder only uses a bank account the vendor already
  holds and never creates one. An IBAN registered to another partner is not
  attached, never suggested, and the chatter names its owner. A new IBAN stays
  a suggestion that is never pre-selected.
- Values the vendor already has never change; an empty address, phone, email
  or website is filled from the invoice and logged.
- PDF imports read by the AI are unchanged.
- Upgrade: `-u goodahead_invoice_import`. Tests: 925.

19.0.1.4.0 — source dispatch that does not depend on load order.

- The core owns the mail intake (`message_new`) and the import job; source
  add-ons register a **reader** with a priority (Finvoice before AI) and answer
  hooks for their own key only.
- Mail attachments go to the first reader that accepts them. Files no reader
  takes are named on the record; a mail nothing can read gives one failed
  record (*Format not supported*) that keeps the files.
- Each import record stores its `reader`; a re-import uses that reader's
  matching policy. Files that go on the bill are passed explicitly.
- Upgrade: `-u goodahead_invoice_import`; the source add-ons' migrations fill
  `reader` on existing records. Tests: 823 with every add-on, 141 without the
  AI part.

19.0.1.3.0 — duplicate invoices are stopped on every channel.

- **Deliberate behaviour change:** before any bill is created, the core looks
  for a non-cancelled bill of the same kind, in the same company, from the
  same vendor company (or, with no vendor matched, from a partner with the
  seller's VAT number or Business ID) with the same invoice number, compared
  ignoring case, spaces and separators. An earlier import record with the same
  number counts too. On a hit **no bill is created**: the record is flagged
  *duplicate* with a link to the existing bill until an Accountant overrides
  the flag or closes the record.
- This applies to PDFs read by the AI as well. Until now a duplicate PDF
  became a second draft bill with a warning; it is now stopped.
- The lookup uses an index on the normalised bill reference, created at
  install.
- The multi-company record rule on import records moved here from the APIX
  add-on.
- Upgrade: `-u goodahead_invoice_import`. Tests: 751.

19.0.1.2.0 — source interface and matching policy.

- Sources hand invoice data to the core through `_create_for_source` and
  `_import_from_invoice_data`, which returns an `ImportResult`. No PDF and no
  AI are needed.
- **Matching policy per source.** The default keeps the AI import's matching.
  The strict policy (e-invoices) lets only the VAT number or Business ID set the
  vendor, offers weaker matches as candidates, leaves the vendor empty when
  identifiers point to different vendors, and offers every vendor company that
  shares an identifier (contacts count as their company).
- Non-AI sources write a neutral *Imported from …* chatter note.
- Upgrade: `-u goodahead_invoice_import`. Tests: 735; the AI suite unchanged.

19.0.1.1.0 — import record lifecycle.

- Stored statuses: waiting, imported, duplicate, failed, possibly lost, closed
  without bill. *Processing* is no longer stored: it is shown while the
  record's background job is queued or running, and flagged as too long after
  30 minutes. This fixes imports that never showed as processing.
- New fields: source, failure kind, duplicate link and override, close reason,
  closed by and date.
- *Close without bill* dialog (reason required), *Reopen*, *Retry* from failed
  (or from a waiting record without a job), *Create bill anyway* on a
  duplicate.
- A PDF is no longer required on an import record.
- Upgrade: `-u goodahead_invoice_import`. Migration `19.0.1.1.0` maps
  draft/processing to waiting and done to imported, and fills source,
  normalised invoice number and seller identifiers on older records. Tests:
  715.

19.0.1.0.0 — first version, split out of the AI import.

- Holds the generic import machinery that was in `goodahead_invoice_ai_import`:
  the import record, vendor matching, partner identifiers, candidates,
  suggestions and the review dialog, expense categories, the core profile,
  bank checks, invoice-data validation, Peppol BIS 3.0 generation and
  decoding, the failed-imports systray and the bill-form additions. Their
  external ids keep their names and change owner.
- **The import record model is renamed** from `ai.invoice.import` to
  `goodahead.invoice.import`, in place, by this add-on's pre-install hook.
- **Upgrade (existing AI-import databases):** run
  `-u goodahead_invoice_ai_import`. It installs this core first and renames
  the model in place. The upgrade refuses while import jobs are queued or
  running — let them finish first. Installing this core on its own on such a
  database is refused. Take a backup; there is no automatic downgrade.
- Uninstalling the AI add-on afterwards keeps the import history, failed
  imports and vendor data, which now belong to this core.
- Tests: 692 on a fresh install (the AI suite's 681 unchanged); the upgrade
  rehearsal keeps ids, messages, attachments, activities and followers.
