# `goodahead_invoice_import_finvoice` — changelog

Newest first. Test counts are the full gate of the add-ons installed at that
version.

**Release gate (19.0):** 2,020 tests, 0 failed.

---

19.0.1.6.3 — release clean-up: no functional change.

- README: Install and Usage sections.
  Upgrade: `-u goodahead_invoice_import_finvoice`.

---

19.0.1.6.2 — release clean-up: invented test data, no functional change.

- Comments, docstrings and tests no longer carry data from real documents
  or databases; test documents are invented, same shapes.
  Upgrade: `-u goodahead_invoice_import_finvoice`.

---

19.0.1.6.1 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_import_finvoice`.

---

19.0.1.6.0 — release check.

- Release check: a ticket id / vendor name removed from comments and docs. No code change.

---

19.0.1.5.0 — the Finvoice reader moves to the shared e-invoicing base.

- The Finvoice library (`lib/finvoice/`: parser, mapping, values, errors and
  the Finvoice 3.0 schema) moves unchanged to the new add-on
  `goodahead_einvoice_base`, so receiving and sending share one reader and one
  schema. `lib/finvoice/` here is a re-export: the old import path
  `odoo.addons.goodahead_invoice_import_finvoice.lib.finvoice` and its
  submodules give the very same objects, and `validate_schema` keeps its
  receiving shape (a list of `"line N: message"` strings).
- New dependency: `goodahead_einvoice_base`. No data change.
- Upgrade: `-u goodahead_invoice_import_finvoice` (installs the base).

19.0.1.4.0 — Finvoice 1.3, sublines, totals that do not add up, credit notes.

- **Finvoice 1.3 is accepted** and read like 2.0; only a missing
  or unknown version is refused. Real incoming bills from an operator were all
  1.3.
- **VAT rate from the header**: rows with no VAT rate or amount
  and no VAT breakdown get the one Finnish rate (25.5, 24, 14, 13.5, 10 or 0)
  that the header's VAT and VAT-exclusive totals make to the cent, with a
  warning on the bill. No such rate: the rows stay without one, as before.

- **Sublines (SubInvoiceRow)** are read as lines and booked only when the
  stated total includes them: the rows alone miss it and the rows with the
  sublines make it (a credit for an earlier payment, for example). Otherwise
  they are left out as headings and subtotals, as before.
- **A message whose rows do not make its stated total** is imported as a draft
  bill with the red banner, instead of failing (needs
  `goodahead_invoice_import` 19.0.1.9.0, `source_meta.amounts_stated`).
- **A price per 100 pieces the sender does not declare** is imported from the
  stated row total; **a row total that is not quantity × a storable price**
  keeps its total (one unit, the sender's quantity and price in the text).
- **Credit notes:** a credit row with a negative quantity and unsigned amounts
  is a credit; the credit note is linked to the vendor's one posted bill whose
  reference is the message's OriginalInvoiceNumber (no link when there is none
  or several).
- Upgrade: `-u goodahead_invoice_import,goodahead_invoice_import_finvoice`.

19.0.1.3.0 — a Finvoice that does not add up, or lacks a value, fails visibly.

- The invoice's line total is the document's VAT-exclusive total (adjusted by
  its rounding, per the implementation guidelines), no longer the sum of the
  mapped rows, so rows that do not make the stated total fail as
  `import.totals_mismatch` and no bill is created.
- A message that lacks a required value (invoice number, date, seller name,
  total, a row amount) keeps everything else it held as editable invoice data
  on the failed record: fill in the gap and *Re-import from JSON*.
- The context keys (`goodahead_finvoice_main_attachment_id`,
  `goodahead_finvoice_extra_attachment_ids`) are removed; pass
  `main_attachment` / `extra_attachments` to `_import_from_invoice_data`.
- Upgrade: `-u goodahead_invoice_import_finvoice`. Needs
  `goodahead_invoice_import` 19.0.1.8.0.

19.0.1.2.0 — strict bank-account rule (from the core).

- Finvoice imports use the core's strict bank-account rule: a bank account the
  vendor does not hold is never created, an IBAN registered to another partner
  is not attached and the chatter names its owner, a new IBAN stays a
  suggestion that is never pre-selected.
- Values the vendor already has never change; empty address, phone, email and
  website are filled from the invoice and logged.
- Upgrade: `-u goodahead_invoice_import_finvoice`. Needs
  `goodahead_invoice_import` 19.0.1.5.0. Tests: 925.

19.0.1.1.0 — dispatch through the core, independent of load order.

- Registers the `finvoice` reader (priority 10, strict matching policy) with
  the core instead of overriding the mail intake. With the AI add-on installed
  in either order, Finvoice files come here and PDFs go to the AI.
- A re-import uses the strict policy and records no AI usage.
- Upgrade: `-u goodahead_invoice_import_finvoice`. Migration `19.0.1.1.0` sets
  `reader = 'finvoice'` on existing Finvoice records. Tests: 823 with every
  add-on, 141 without the AI part.

19.0.1.0.0 — Finvoice by upload and email.

- Finvoice files uploaded into a purchase journal or mailed to the bill intake
  are split into one import record per invoice and imported through the core
  with the strict matching policy and no AI.
- The Finvoice message is the bill's main document unless a PDF image came
  with it; neutral *Imported from Finvoice (upload | email)* chatter.
- Unreadable files and Finvoice 1.3 fail at intake with a reason; non-invoice
  messages fail with their own code; without the AI add-on a mail with no
  readable file fails as *Format not supported*.
- The pure-Python library (parser and mapping for Finvoice 2.0, 2.01 and 3.0)
  maps each message to the shared invoice data per the Finvoice 3.0
  implementation guidelines v2.6: credit notes turned positive once, rounding,
  document-level charges, VAT breakdown, IBAN/BIC pairing, Business ID in the
  VAT field. Sub-rows are informational and not bill lines; the amount to pay
  subtracts amounts already paid.
- Install: `-i goodahead_invoice_import_finvoice`. Tests: 58 (48 offline
  library tests on invented fixtures).
