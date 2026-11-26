# `goodahead_einvoice_base` — changelog

Newest first.

---

18.0.1.9.4 — security fix.

### Security

- The Finvoice reader refuses a file larger than 20 MB before reading it, and
  finds XML declarations and DOCTYPEs with linear scans instead of regular
  expressions whose running time grew with the square of the file size, so a
  crafted file can no longer hold a worker for a long time.
- Upgrade: `-u goodahead_einvoice_base`.

---

18.0.1.9.3 — release clean-up: invented test data, no functional change.

- Comments, docstrings and tests no longer carry data from real documents
  or databases; test documents are invented, same shapes.
  Upgrade: `-u goodahead_einvoice_base`.

---

18.0.1.9.2 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_einvoice_base`.

---

18.0.1.9.1 — first release for Odoo 18.0, with the features of 19.0.1.0.0 to
19.0.1.9.1 (listed below).

- The attention route is `type="json"` (19.0: `jsonrpc`).
- Unit codes: a unit relates to the reference unit of its unit category
  (19.0: to any unit), so the list shows the category. Odoo 18.0 ships no
  minute or kWh unit (filled by name where a database has one) and keeps
  the square metre as `uom.uom_square_meter` (filled by name).
- The unit codes' upgrade script is `migrations/18.0.1.4.0`.

---


19.0.1.9.1 — review fixes of the Finvoice content.

- `vat.check_tax` refuses an exemption reason code the e-invoice cannot
  hold (not 1–20 letters, digits or hyphens: the ERP lists longer national
  codes); `vat.sendable_vat` keeps a message valid for a tax the check
  skips (the code left out, at most three reason lines of 70, `left_out`
  naming what was dropped).
- The writer sends a delivery address's second street line as the second
  `DeliveryStreetName` (`delivery.street2`, cut to 35).

---

19.0.1.9.0 — the whole invoice in the Finvoice message.

- `lib/finvoice/vat.py`: VAT categories, default reason wordings (Finnish /
  English), standard exemption codes, the Finnish VAT report table for a
  0 % tax's default category (`fi_311` intra-EU supply, `fi_312` / `fi_319`
  reverse charge, otherwise none), the tax consistency check, per-line
  category resolution and the VAT breakdown by rate and category. Export
  taxes may carry the directive's article 146 code.
- `lib/finvoice/references.py` (order numbers, the payment-reference rule),
  `rows.py` (row discounts that add up to the cent, the import's percentage
  choice, text-row splitting), `gtin.py` (trade item numbers).
- Writer: delivery party, `InvoiceTypeCodeUN`, seller / order / buyer
  references (never cut), rows total, `VatFreeText` and
  `VatExemptionReasonCode` per categorised entry, text rows, `EanCode`,
  row discounts (negated with the rows on credit notes), payment means.
  Every new key optional; existing values write as before. `explain` names
  the new elements.
- Reader: `order_reference`, `buyer_reference`, and on discounted rows
  `gross_price`, `discount_percent`, `discount_amount`; every other key as
  before.
- Invoices and bills: *Buyer Reference* (`einvoice_buyer_reference`, at
  most 70 characters, editable while draft). The column is added empty.

---

19.0.1.8.1 — the default-unit button works from the list.

- *Default unit for unmapped incoming codes* above the Unit codes list
  raised a server error when pressed: the web client calls a list header
  button with the selected units, and the method accepted none. It now
  ignores the selection.

---

19.0.1.8.0 — release notes for sending.

- README: the attention bubble's sections as shipped (failed imports,
  failed sends for Accountants, connection trouble for accounting
  administrators; a failed send opens the invoice on its Delivery tab). No
  code change.

---

19.0.1.7.0 — the reader reads units through the shared table.

- `values.unit_code` delegates to `units.normalise_incoming` (one table for
  both directions; `H87` now reads as `C62`).
- `values.unmapped_unit_code`: the code as written when no unit can be read
  from it; the reader puts it on the line as `unit_code_unmapped` so the
  import can note it.

---

19.0.1.6.0 — the unit codes screen.

- Accounting ▸ Configuration ▸ E-invoicing ▸ Unit codes: every unit with its
  reference unit, Rec 20 code and the readable name e-invoices carry; units
  without a code highlighted, filter "Without code". Accounting
  administrators edit the code in the list and set the default unit for
  unmapped incoming codes from the button above it; anyone else gets the
  list read-only (`uom.uom._get_view`).
- `uom.uom.einvoice_readable_name` (computed) and
  `uom.uom.action_einvoice_default_unit()`.
- Tests: 10 new (menu, window, both roles, highlight and filter, validation).

---

19.0.1.5.0 — credit notes, printed letters and plain-language schema problems.

- `write(values)` writes credit notes (`invoice.type` `credit_note`: `INV02`
  `HYVITYSLASKU`, totals, VAT breakdown, row quantities and amounts and the
  instructed amount negative, unit prices positive) and names
  `OriginalInvoiceNumber` only when `invoice.original_number` is set.
- `write(values, receiver="print")` sends to APIX's print service
  (`PRINT_RECEIVER` = `TULOSTUS` / `APIX`, observed); the customer's postal
  address is required then.
- An empty value the schema requires raises
  `FinvoiceError("finvoice.missing_field", field="<section>.<key>")`.
- `lib/finvoice/explain.py`: `explain(problem)` names the invoice field behind a
  schema problem ("the customer's name", "the unit code of line 2 (Unit
  codes)"); an unknown element falls back to its name.
- Tests: 22 new (credit notes with and without an original, letters, missing
  values, every row of the explain table).

---

19.0.1.4.0 — unit codes and the default incoming unit.

- `uom.uom.rec20_code`: the unit's UN/ECE Rec 20 code for both directions;
  `units.invalid_code` unless 2–3 upper-case letters or digits;
  `units.not_allowed` when anyone but an accounting administrator changes it
  (administrators without the settings right write it through sudo).
- `res.company.einvoice_default_uom_id`: the unit for incoming codes nobody
  mapped; same write rule.
- `uom.uom._einvoice_outgoing()` → `(code, readable name ≤ 14)`;
  `uom.uom._einvoice_unit_for_code(code, company)` → `(unit, note)`, a
  shipped unit preferred when two share a code, else the default unit (or
  none) with "Unit code X is not mapped — … used".
- Install (`post_init_hook`) and upgrade (`migrations/19.0.1.4.0`) fill the
  codes by external id, then by name; never over a code already set.
- Tests: 25 new (fill, upgrade script, rights, validation, both lookups).

---

19.0.1.3.0 — the Finvoice 3.0 writer for invoices.

- `lib/finvoice/writer.py`: `write(values)` builds one Finvoice 3.0 invoice
  message in schema order from the sending values dict: message frame,
  seller with bank account, buyer, totals, VAT breakdown, rows with real
  quantity, Rec 20 unit code, readable unit name (cut to 14 characters) and
  base quantity when not 1, payment details, and `invoice.pdf` as the image.
  A unit without a code goes out with its name only. Pure Python; nothing in
  the ERP calls it yet.
- Tests: five invented values fixtures, each schema-valid and read back by
  the reader with the same totals, lines and units; output is deterministic.

---

19.0.1.2.0 — the shared e-invoicing attention bubble.

- The systray bubble moves here from `goodahead_invoice_import` as the one
  e-invoicing attention bubble: the sum of every section the user may see,
  one block per section with its newest items and *Show all*. Hidden when
  nothing needs attention; a failed refresh keeps the last value.
- `goodahead.einvoice.attention` (abstract): add-ons extend
  `_attention_sections()` and append their section through
  `_attention_section(key, groups, builder)`. A section is built only for
  users in one of its groups; a section that raises is dropped and logged,
  the others still show.
- Route `/goodahead_einvoice/attention` → `{total, sections}`.
- Tests: 26 → 34 (frame, route and the component's states in the browser).

---

19.0.1.1.0 — the shipped unit table.

- `lib/finvoice/units.py`: the UN/ECE Rec 20 table of standard units (code,
  readable Finnish name, incoming aliases) and `readable_name`,
  `is_valid_rec20`, `normalise_incoming`. Pure Python; nothing in the ERP uses
  it yet. Tests: 9 → 26, including agreement with the 19.0 standard table.

---

19.0.1.0.0 — the shared e-invoicing base, with the Finvoice library.

- New add-on. The pure-Python Finvoice library and the Finvoice 3.0 schema move
  here unchanged from `goodahead_invoice_import_finvoice`, which re-exports
  them from its old import path.
- `validate_schema` returns `{path, line, message}` per schema problem, with
  the element path; `validate_schema_text` keeps the reader's earlier
  `"line N: message"` strings.
- Install: comes with `goodahead_invoice_import_finvoice` 19.0.1.5.0. Tests: 9.
