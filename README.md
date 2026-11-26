# Goodahead Odoo addons

[![Nightly against Odoo head](https://github.com/goodahead/goodahead-odoo-addons/actions/workflows/nightly.yml/badge.svg)](https://github.com/goodahead/goodahead-odoo-addons/actions/workflows/nightly.yml)

The nightly run tests the `19.0` and `18.0` branches against the newest commit of
Odoo's own 19.0 and 18.0 branches, so a change in Odoo's internals shows up the
next morning.

Odoo 19.0 modules by [Goodahead Oy](https://goodahead.com), mainly for
accounting: Finnish e-invoicing (Finvoice through APIX, sending and
receiving), import of vendor bills from PDF with AI or from Finvoice files,
and two-step bill approval.

## Modules

### E-invoicing

| Module | Version | What it does |
|--------|---------|--------------|
| [goodahead_einvoice_base](goodahead_einvoice_base/) | 19.0.1.9.3 | Shared base of the Goodahead e-invoicing add-ons: the pure-Python Finvoice library, its schema, unit codes and the shared attention bubble. |
| [goodahead_apix](goodahead_apix/) | 19.0.1.7.3 | Per-company APIX connection: transfer credentials, environment and connection check. |
| [goodahead_invoice_send_apix](goodahead_invoice_send_apix/) | 19.0.1.24.4 | Send customer invoices and credit notes as Finvoice e-invoices and printed letters through APIX. |
| [goodahead_invoice_send_apix_migration](goodahead_invoice_send_apix_migration/) | 19.0.1.3.3 | Carry customers' e-invoice details, transmit methods and sent invoices over from an earlier APIX sending setup, and keep two senders from running at once. |
| [goodahead_invoice_send_apix_migration_community](goodahead_invoice_send_apix_migration_community/) | 19.0.1.1.4 | Sending source for the community APIX modules: report whether they still send, hand their customers' e-invoice details, transmit methods and sent invoices to the migration tool, and stop them per company. |
| [goodahead_finnish_invoice](goodahead_finnish_invoice/) | 19.0.1.0.1 | Customer invoice PDF laid out as a Finnish invoice, with the bank bar code. |

### Vendor bill import

| Module | Version | What it does |
|--------|---------|--------------|
| [goodahead_invoice_import](goodahead_invoice_import/) | 19.0.1.12.4 | Shared import machinery for vendor bills: import records, vendor matching, vendor-data suggestions, bank checks and bill decoding. |
| [goodahead_invoice_ai_import](goodahead_invoice_ai_import/) | 19.0.4.2.3 | Import vendor bills from PDF using Claude AI and Peppol BIS 3.0. |
| [goodahead_invoice_import_finvoice](goodahead_invoice_import_finvoice/) | 19.0.1.6.3 | Import Finvoice e-invoices uploaded or received by email as draft vendor bills, without any AI reading. |
| [goodahead_invoice_import_apix](goodahead_invoice_import_apix/) | 19.0.1.10.5 | Fetch e-invoice packages from APIX for every company, each company in its own background job. |
| [goodahead_invoice_import_apix_migration](goodahead_invoice_import_apix_migration/) | 19.0.1.1.2 | Move a company from an earlier APIX connector to APIX receiving: carry over, shadow run, switch to live and back. |
| [goodahead_invoice_import_apix_migration_community](goodahead_invoice_import_apix_migration_community/) | 19.0.1.0.4 | Migration source for the community APIX modules: detect their credentials, pause and resume them per company. |

### Other

| Module | Version | What it does |
|--------|---------|--------------|
| [goodahead_claude](goodahead_claude/) | 19.0.1.4.2 | Generic Claude (Anthropic) API client for Odoo addons. |
| [goodahead_bill_approval](goodahead_bill_approval/) | 19.0.1.5.0 | A vendor bill or customer credit note is reviewed, then approved, before it can be posted. Two approval levels granted per user, with an optional chosen approver and substitute. |

## Dependencies

- Odoo 19.0 (Community is enough).
- [OCA/queue](https://github.com/OCA/queue) `queue_job`, for
  `goodahead_claude`, the invoice import modules and APIX sending.
- `goodahead_invoice_send_apix`: `l10n_fi_edicode` from
  [OCA/l10n-finland](https://github.com/OCA/l10n-finland).
- The `*_migration_community` modules install themselves only where the
  community APIX modules (`connector_apix`) are present.
- Python `requests` and `lxml`.
- `goodahead_invoice_ai_import`: a Claude API key from
  [Anthropic](https://www.anthropic.com/api).
- APIX modules: an APIX account (transfer ID and key) per company.

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
