# Reference documentation

The documents below are the **source of truth** for this module:

- The APIX client is written from the APIX documentation only. Every operation, parameter and
  status code it uses comes from the pages listed below.
- The Finvoice → import translation is written from the Finvoice documents. Where the documents
  and other implementations disagree, the documents win.

## APIX (Finago Apix) REST API

Source: the public APIX documentation, https://wiki.apix.fi/. No copies are included with this
add-on; read the pages on the APIX site.

| Page | Relevant for |
|---|---|
| [REST API for external usage](https://wiki.apix.fi/rest-api-for-external-usage) | Hosts (production `api.apix.fi` / `terminal.apix.fi`, test `test-api.apix.fi` / `test-terminal.apix.fi`); **RetrieveTransferID** (credentials); **Receiving functions**: **ListInvoiceZIPs** (`list2`, storage and transfer statuses, 500-item cap), **Download** (`markreceived=yes/no`), **GetMetadata**; 3-month retention of received invoices |
| [SHA-256 usage in APIX webservices API](https://wiki.apix.fi/sha-256-usage-in-apix-webservices-api) | How request digests are computed (parameter order, shared secret, `SHA-256:` prefix, timestamp format) |
| [Response message structure](https://wiki.apix.fi/response-message-structure) | Response XML (`Status`, `StatusCode`, `FreeText`, `Content/Group/Value[@type]`) |
| [Request message structure](https://wiki.apix.fi/request-message-structure) | Request XML (only needed for PUT operations; not used for receiving) |
| [List value types](https://wiki.apix.fi/list-value-types) | Value types and status codes |
| [Application interface for software vendors](https://wiki.apix.fi/application-interface-for-software-vendors) | Expected behaviour of integrating software (first and later invocations) |
| [Attachments / SecureStorage](https://wiki.apix.fi/attachments-securestorage) | Attachments and storage identifiers when receiving |

The APIX responses used in the tests (`goodahead_apix/tests/fixtures/`) are invented; they follow
the documented message structure but are not copied from the documentation.

## Finvoice 3.0 — `finvoice/`

Source: Finance Finland (Finanssiala), https://www.finanssiala.fi/en/topics/finvoice-standard/
(page updated 16 Nov 2026). The page states: "All materials on this page are freely usable."
The files are kept unmodified, as downloaded on 2026-11-23. They are not covered by
this add-on's licence; see `finvoice/NOTICE`.

| File | Source URL | Version / date | Relevant for |
|---|---|---|---|
| `Finvoice_3_0_implementation_guidelines.pdf` | https://file.finanssiala.fi/finvoice/Finvoice_3_0_implementation_guidelines.pdf | Version 2.6, 16 September 2025 | Mapping rules: invoice type codes (§10.5), credit notes and `OriginalInvoiceNumber` (§10.6), rows and subtotals (§10.1–10.2), VAT-exempt grounds (§10.4), numeric values (§9.4), character set (§9.3) |
| `Finvoice_3_0_soveltamisohje.pdf` | https://file.finanssiala.fi/finvoice/Finvoice_3_0_soveltamisohje.pdf | Finnish original of the guideline | Wording check when the English translation is ambiguous |
| `Finvoice3.0.xsd` | https://file.finanssiala.fi/finvoice/Finvoice3.0.xsd | Header: "Modified 17.12.2020 (back to version 11/2019)", for Finvoice 3.0 | Element names, cardinality, types; **test fixtures must validate against it** |
| `Finvoice_def_3_0.xls` | https://file.finanssiala.fi/finvoice/Finvoice_def_3_0.xls | Data list 3.0 (updated 21 Dec 2020) | Element-by-element descriptions and allowed values |
| `Finvoice_3_0_structure.pdf` | https://file.finanssiala.fi/finvoice/Finvoice_3_0_structure.pdf | Schema diagram 3.0 | Overview of the message tree |

The schema `Finvoice3.0.xsd` used at run time ships in `goodahead_einvoice_base`
(`lib/finvoice/schema/Finvoice3.0.xsd`), where the tests validate fixtures against it.
