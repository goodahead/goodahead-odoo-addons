# Goodahead Odoo addons

Odoo 19.0 modules by [Goodahead Oy](https://goodahead.com), mainly for
accounting: AI import of vendor bills and two-step bill approval.

## Modules

| Module | Version | What it does |
|--------|---------|--------------|
| [goodahead_invoice_ai_import](goodahead_invoice_ai_import/) | 19.0.3.33.0 | Turns vendor bill PDFs (by email alias or upload) into draft bills. Claude reads the PDF; the result is built as Peppol BIS 3.0 UBL and imported through Odoo's own UBL importer. Vendors are matched by VAT, business ID, phone, email or website domain, then name; unknown vendors are offered as candidates, not created. The bill's IBAN must belong to the vendor. |
| [goodahead_claude](goodahead_claude/) | 19.0.1.4.0 | Claude (Anthropic) API client for other addons: per-company API key and default model, a synced model catalog, prompt caching, and Test Connection / Sync Models in Settings. |
| [goodahead_bill_approval](goodahead_bill_approval/) | 19.0.1.5.0 | A vendor bill or customer credit note is reviewed, then approved, before it can be posted. Two approval levels granted per user, with an optional chosen approver and substitute. |

## Dependencies

- Odoo 19.0 (Community is enough).
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

For the AI invoice import, continue with
[goodahead_invoice_ai_import/docs/SETUP.md](goodahead_invoice_ai_import/docs/SETUP.md):
email alias, API key and model, prompt profile, expense categories and
queue capacity. How the import works inside is in
[docs/architecture.md](goodahead_invoice_ai_import/docs/architecture.md).

## Tests

Each module has tests in its `tests/` folder:

```
odoo -d <test-db> -i <module> --test-tags /<module> --stop-after-init
```

The tests never call the Claude API; responses are mocked.

## Changes

Release notes are in each module's `CHANGELOG.md` where it has one.

## License

LGPL-3.0 or later; see [LICENSE](LICENSE). Each module declares its licence in
its `__manifest__.py`.
