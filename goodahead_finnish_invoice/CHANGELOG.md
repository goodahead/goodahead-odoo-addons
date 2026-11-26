# `goodahead_finnish_invoice` — changelog

Newest first.

---

19.0.1.0.1 — release clean-up: no functional change.

- README: Install and Usage sections.
  Upgrade: `-u goodahead_finnish_invoice`.

---

19.0.1.0.0 — first public release.

- Manifest: website and a short description pointing to the README.
- No change to the layout or the code. Upgrade: `-u goodahead_finnish_invoice`.

---

19.0.0.0.4 — ready for the open-source release.

- README: what the add-on does, which documents it applies to, setup (company
  bank account, payment reference standard, late payment interest and
  complaint period) and the bank bar code rules.
- Licence header on every Python file; comments no longer name a ticket or
  another product. Author is Goodahead Oy.
- A release test: licence headers, the README and no ticket keys.
- No change to the layout or the code. Upgrade: `-u goodahead_finnish_invoice`.

---

19.0.0.0.3 — automated tests.

- Tests for the bank bar code, versions 4 (Finnish reference) and 5 (RF
  creditor reference), field by field and against the guideline's worked
  example; every reason `barcode_input_error` gives; the Finnish address
  helpers (`same_place`, `delivery_name`). These run without the ERP:
  `python -m unittest discover -s goodahead_finnish_invoice/tests -t goodahead_finnish_invoice`.
- A report test: a payable invoice renders its bar code; a credit note, a paid
  invoice and a zero invoice render none.
- No change to the layout or the code. Upgrade: `-u goodahead_finnish_invoice`.
