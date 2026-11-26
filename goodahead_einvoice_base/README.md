# E-invoicing Base (`goodahead_einvoice_base`)

The shared base of the Goodahead e-invoicing add-ons. Receiving (the invoice
import add-ons) and sending stand on it, so both directions use one Finvoice
library and one copy of the Finvoice 3.0 schema.

- **Finvoice library** (`lib/finvoice/`): pure Python, no ERP import, tested
  without a database. It reads Finvoice 1.3, 2.0, 2.01 and 3.0 messages into
  invoice data (`parse`, `to_invoice_data`) and checks 3.0 messages against
  the schema.
- **Finvoice writer** (`lib/finvoice/writer.py`): `write(values)` builds one
  Finvoice 3.0 invoice message (UTF-8) in schema order from a plain values
  dict (sender and receiver, seller, buyer, invoice, lines, VAT breakdown,
  payment details). Each line carries its real quantity, the unit's Rec 20
  code and readable name (a unit without a code goes out with its name
  only), and the base quantity when the price is per several units. Every
  message names `invoice.pdf` as its image. Credit notes (`INV02`) go out
  with negative amounts and name the original invoice only when there is
  one; `write(values, receiver="print")` addresses APIX's print service for
  a printed letter. An empty value the schema requires raises
  `finvoice.missing_field` naming it.
- **Finvoice content** (pure, shared by sending and receiving):
  `vat.py` — the VAT categories, the default VAT reason wordings (Finnish,
  then English, one 70-character text each), the standard exemption reason
  codes, the Finnish VAT report table that names a 0 % tax's default
  category (`fi_311` → intra-EU supply; `fi_312`, `fi_319` → reverse
  charge; anything else, `fi_304` included, none), the consistency check of
  a tax's rate, category, code and reason, and the per-line category and
  VAT breakdown by rate and category; `references.py` — the customer's
  order number (the customer reference less our echoed order numbers), our
  order number, and the rule that withholds a numeric order number that
  would read as a payment reference; `rows.py` — the row discount that
  makes a row add up to the cent and the import's choice of percentage;
  `gtin.py` — the trade item number check. The writer states the new
  content in schema order (delivery party, references, rows total, VAT
  reasons and exemption codes, text rows, discounts, trade item numbers,
  invoice type and payment means codes); the reader reads the order number,
  buyer reference and row discounts.
- **Buyer reference:** invoices and bills have a *Buyer Reference*
  (`einvoice_buyer_reference`, at most 70 characters, editable while
  draft). Sending fills it from the customer; the import from the message.
- **Plain-language problems:** `explain(problem)` names the invoice field
  behind a schema problem ("the customer's name", "the unit code of line 2
  (Unit codes)").
- **Schema check:** `validate_schema(xml_bytes)` returns one problem per
  schema error as `{path, line, message}`, where `path` is the element path
  inside the message (for example
  `/Finvoice/BuyerPartyDetails/BuyerOrganisationName`). Receiving treats
  problems as a warning; sending refuses to send a message that has any.
  `validate_schema_text(xml_bytes)` gives the same problems as
  `"line N: message"` strings.
- **Unit table** (`lib/finvoice/units.py`): the UN/ECE Rec 20 code, readable
  Finnish name and incoming aliases of the standard units (pieces, hours,
  kilograms, metres, litres, kWh and the like), so standard units need no
  configuration in either direction. `normalise_incoming(code_un, code_free)`
  reads an incoming row's unit (`kpl` and `H87` read as `C62`) and gives
  `None` for anything it does not know, never a guessed unit.
- **Unit codes in the ERP:** every unit has a *Unit code (UN/ECE Rec 20)*
  (`rec20_code`, 2–3 upper-case letters or digits), the one code used both
  when sending and when receiving. The standard units get theirs at install
  (by external id, then by name for units a company created itself such as
  *kk* or *Weeks*; a code already set is never overwritten). Each company has
  a *Default unit for unmapped incoming codes*: an incoming line whose code no
  unit carries gets that unit (or no unit when empty) and a note on the bill.
  Only accounting administrators change unit codes and the default unit;
  everyone else reads them. Both live in *Accounting ▸ Configuration ▸
  E-invoicing ▸ Unit codes*: an editable list (units without a code are
  highlighted and have a *Without code* filter) with the default unit behind
  the button above it. On Odoo 18.0 the list shows each unit's category; a
  unit relates to its category's reference unit (19.0: to any unit), and
  18.0 ships no minute or kWh unit (filled by name where a database has one;
  the square metre is `uom.uom_square_meter`).
- **Schema:** `lib/finvoice/schema/Finvoice3.0.xsd`, the reference schema
  published by Finance Finland.
- **Attention bubble** (systray): one bubble for every e-invoicing add-on,
  with the total and one block per section — failed imports from receiving,
  failed sends from sending (Accountants) and APIX connection trouble
  (accounting administrators only). A failed send opens the invoice on its
  Delivery tab. Each section
  is shown only to the people it is for, and counts only the companies the
  user is working in. Clicking an item opens it; *Show all* opens the
  section's list.

  Add-ons contribute a section by extending
  `goodahead.einvoice.attention._attention_sections()`:

      return super()._attention_sections() + [
          self._attention_section("my_key", "account.group_account_invoice",
                                  self._my_section),
      ]

  where the builder returns `{label, count, items, action, item_action}`
  (items: up to 10 `{id, title, subtitle, date}`). The systray reads
  `/goodahead_einvoice/attention`.

The add-on is installed automatically by the add-ons that need it.

## Install

Nothing to set up. Install an e-invoicing add-on (for example
`goodahead_invoice_import_finvoice`) and this base comes with it.

Upgrading to 18.0.1.4.0 fills the unit codes of existing databases the same
way the install does (`migrations/18.0.1.4.0/post-fill-unit-codes.py`).

## Configuration

- *Accounting ▸ Configuration ▸ E-invoicing ▸ Unit codes*: check that every
  unit you invoice in has a Rec 20 code, and choose the company's default
  unit for unmapped incoming codes (accounting administrators).
- Nothing else; the e-invoicing add-ons built on this base have their own
  settings.

### Limits

The Finvoice reader refuses a file larger than 20 MB before reading it
(`finvoice.parser.MAX_FILE_BYTES`; a caller may pass a smaller `max_bytes`).
The receiving add-ons set their own, smaller caps through system parameters;
see their READMEs.

## Usage

- The *Buyer Reference* field is on invoices and bills.
- The attention bubble in the systray lists failed imports, failed sends and
  APIX connection trouble for the companies you work in.

## Tests

The library tests run without the ERP:

    python -I -m unittest discover -s goodahead/goodahead_einvoice_base/tests -t goodahead

and in the ERP's test runner with `--test-tags /goodahead_einvoice_base`,
which also runs the bubble's browser tests (needs Chrome). All fixtures are
invented.

## Licence

LGPL-3.0 or later. Author: Goodahead Oy.

The Finvoice 3.0 schema (`lib/finvoice/schema/Finvoice3.0.xsd`) is published
by Finance Finland (Finanssiala), https://www.finanssiala.fi/en/topics/finvoice-standard/,
which states that all materials on its Finvoice page are freely usable. It is
included unmodified and is not covered by this add-on's LGPL-3.0 licence; see
`lib/finvoice/schema/NOTICE`.
