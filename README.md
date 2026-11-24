# Goodahead Odoo addons

[![Nightly against Odoo head](https://github.com/goodahead/goodahead-odoo-addons/actions/workflows/nightly.yml/badge.svg)](https://github.com/goodahead/goodahead-odoo-addons/actions/workflows/nightly.yml)

The nightly run tests the `19.0` and `18.0` branches against the newest commit of
Odoo's own 19.0 and 18.0 branches, so a change in Odoo's internals shows up the
next morning. The workflow lives on the default branch (`19.0`).

Odoo 18.0 modules by [Goodahead Oy](https://goodahead.com), mainly for
accounting: AI import of vendor bills and two-step bill approval.

## Modules

| Module | Version | What it does |
|--------|---------|--------------|
| [goodahead_invoice_ai_import](goodahead_invoice_ai_import/) | 18.0.3.34.0 | Turns vendor bill PDFs (by email alias or upload) into draft bills. Claude reads the PDF; the result is built as Peppol BIS 3.0 UBL and imported through Odoo's own UBL importer. Vendors are matched by VAT, business ID, phone, email or website domain, then name; unknown vendors are offered as candidates, not created. The bill's IBAN must belong to the vendor. |
| [goodahead_claude](goodahead_claude/) | 18.0.1.4.0 | Claude (Anthropic) API client for other addons: per-company API key and default model, a synced model catalog, prompt caching, and Test Connection / Sync Models in Settings. |
| [goodahead_bill_approval](goodahead_bill_approval/) | 18.0.1.5.0 | A vendor bill, vendor credit note or customer credit note is reviewed, then approved, before it can be posted. Two approval levels granted per user, with an optional chosen approver and substitute. |

## Dependencies

- Odoo 18.0 (Community is enough). The `19.0` branch has the Odoo 19.0
  versions.
- `goodahead_invoice_ai_import`, US vendors: install `l10n_us` to store the
  routing number on the vendor's bank account (otherwise it is stated on the
  bill).
- [OCA/queue](https://github.com/OCA/queue) `queue_job`, for
  `goodahead_claude` and `goodahead_invoice_ai_import`.
- Python `requests`.
- `goodahead_invoice_ai_import`: a Claude API key from
  [Anthropic](https://www.anthropic.com/api).

## Installation

1. Clone this repository and add its folder to Odoo's `addons_path`,
   together with OCA/queue.
2. Run Odoo with `queue_job` loaded as a server-wide module
   (`--load=base,web,queue_job`) and at least one worker.
3. Update the apps list and install the modules you need.

For the AI invoice import, the feature list and setup steps (including product
lookup from vendor pricelists) are in
[goodahead_invoice_ai_import/README.md](goodahead_invoice_ai_import/README.md);
the reasons behind each setup step are in
[docs/SETUP.md](goodahead_invoice_ai_import/docs/SETUP.md). How the import
works inside is in
[docs/architecture.md](goodahead_invoice_ai_import/docs/architecture.md).

## Tests

Each module has tests in its `tests/` folder:

```
odoo -d <test-db> -i <module> --test-tags /<module> --stop-after-init
```

The tests never call the Claude API; responses are mocked.

## Changes

Release notes are in each module's `CHANGELOG.md`.

## License

LGPL-3.0 or later; see [LICENSE](LICENSE). Each module declares its licence in
its `__manifest__.py`.
