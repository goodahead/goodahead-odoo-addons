# `goodahead_invoice_ai_import` — changelog

Newest first. Upgrade with `-u goodahead_invoice_ai_import` unless an entry
says otherwise.

**Release gate (19.0):** 2,020 tests, 0 failed.

---

19.0.4.2.3 — release clean-up: no functional change.

- Test comments made generic; an approval add-on is no longer named in tests.
  Upgrade: `-u goodahead_invoice_ai_import`.

---

19.0.4.2.2 — release clean-up: invented test data, no functional change.

- Comments, docstrings and tests no longer carry data from real documents
  or databases; test documents are invented, same shapes.
- The prompt's two example US addresses are invented ones of the same
  length and shape.
  Upgrade: `-u goodahead_invoice_ai_import`.

---

19.0.4.2.1 — release clean-up: no functional change.

- Comments, tests and documentation reworded for the public release; this
  changelog shortened to one entry per version. Test fixtures use invented
  documents only.

---

## Upgrading to 19.0.4.x — the split into a core and an AI part

- Run `-u goodahead_invoice_ai_import`. It installs `goodahead_invoice_import`
  first and renames `ai.invoice.import` to `goodahead.invoice.import` in place.
  The upgrade refuses while import jobs are queued; let them finish first. Take
  a backup; there is no automatic downgrade.
- Installing the core on its own on a database that still has the old table is
  refused with a message telling you to upgrade this add-on instead. A refused
  upgrade changes nothing.
- Kept through the upgrade: settings, the mail alias and mail intake, the import
  history, failed imports (they can still be retried), messages, attachments,
  activities and followers. PDFs are still read by the AI.
- The one deliberate behaviour change: a PDF whose vendor and invoice number
  already exist as a bill is flagged *duplicate* with a link to the existing
  bill, instead of becoming a second draft bill. An Accountant chooses *Create
  bill anyway* or *Close without bill*.
- New statuses on import records: waiting, imported, duplicate, failed,
  possibly lost, closed without bill.
- Install a Finvoice or APIX source next to this add-on if you receive
  e-invoices: they are imported without AI and without AI cost.

---

19.0.4.2.0 — *Create bill anyway* on a duplicate PDF builds the bill from the
stored reading, without a second AI call. Needs `goodahead_invoice_import`
19.0.1.8.0.

19.0.4.1.0 — mail and jobs reach the AI through the core's source dispatch.
With the Finvoice add-on installed, Finvoice files go to it and PDFs come here.
A mail with no supported attachment now fails as "Format not supported"; other
attachments of a mail with PDFs are named in the chatter.

19.0.4.0.0 — the import machinery (import record, vendor matching, suggestions,
bank checks, bill decoding) moves to the new dependency
`goodahead_invoice_import`; this add-on keeps the AI reading, PDF mail intake,
token usage, prompt profiles and extraction settings. See the upgrade notes
above. Uninstalling this add-on now leaves the import history and vendor data in
place.

19.0.3.33.0 — several bills from one vendor imported at the same time no longer
make one of them fail. The failed-imports bubble and the import menus are shown
to Bookkeepers and Administrators only; access rights are unchanged.

19.0.3.32.0 — extraction prompts are a collection a company picks from. New
*Extraction Prompts* (Accounting ▸ Configuration ▸ AI Invoice Import) with two
built-in profiles, United States and European Union; duplicate one to make an
editable profile, and pick it in the new company setting *Extraction Prompt*.
Empty keeps the choice by company country. An inbound mail alias left behind by
an older version is kept as it is and only released from this module.

19.0.3.31.1 — the red banner keeps "(tax …)" on one line.

19.0.3.31.0 — the bill carries the PDF's lines only; any difference between the
bill and the invoice total is shown in a red banner for the reviewer.

19.0.3.30.0 — the imported bill's total equals the invoice's, tax included.

19.0.3.29.0 — an invoice that prints its tax only as one total is imported, with
a banner explaining how the tax was applied.

19.0.3.28.0 — the import no longer grants bill approval step 1; a person reviews
the bill against the PDF first.

19.0.3.27.0 — the import raises bill approval step 1 when the optional approval
module is installed (a soft dependency).

19.0.3.26.0 — every vendor-matching step, including the business id, refuses our
own companies.

19.0.3.25.0 — the unmatched-vendor explanation no longer shows on bills typed
by hand.

19.0.3.24.0 — the vendor line and the candidate card show draft bills as well as
posted ones; a bill with no candidate explains why; a new button creates the
vendor contact from the extracted data.

19.0.3.23.0 — a document's vendor is never one of our own companies, and a
received document's bank account is never added to our own partner. Another
company's partner is still allowed (the intercompany case).

19.0.3.22.0 — the country-aware phone key and its migration are withdrawn; phone
matching works as in 19.0.3.10.0.

19.0.3.21.0 — phone-key fixes (withdrawn in 19.0.3.22.0); a refusal by the
core-profile check is recorded on the import record.

19.0.3.20.0 — currency detection from the PDF: hidden annotations and form-field
streams handled correctly, text encodings decided by the byte-order mark, and
older imports re-marked in both directions on re-import.

19.0.3.19.0 — currency detection also reads annotations and form fields, and a
short page that prints a currency counts as printed.

19.0.3.18.0 — whether an invoice prints a currency is now read from the PDF
itself, not from the AI's answer, so a domestic invoice without a currency is
marked correctly.

19.0.3.17.0 — fixes across vendor matching, the substitute-PDF setting and the
prompt; no new features.

19.0.3.16.0 — the email-domain exclusion list covers domains that identify
nobody (free providers, ISPs, placeholders), including their subdomains, and can
be extended in a system parameter.

19.0.3.15.0 — 72 free email providers added to the exclusion list, 47 of them
Nordic.

19.0.3.14.0 — prompt fixes: Finnish-only rules are no longer applied to other
European countries.

19.0.3.13.0 — the prompt is chosen from the importing company's country (US, EU
or neutral); the US prompt is unchanged.

19.0.3.12.0 — fixes to 19.0.3.11.0's phone-key migration.

19.0.3.11.0 — phone keys stored in international format (withdrawn in
19.0.3.22.0).

19.0.3.10.0 — the substitute "Generated by Odoo" PDF is turned off only on cores
that support the setting, and a stray setting from an earlier upgrade is
cleaned up.

19.0.3.9.0 — fixes across the dual-core support; no new features.

19.0.3.8.0 — product matching by vendor product code also works on the older
19.0 core.

19.0.3.7.0 — no vendor is created by the import on the older 19.0 core either.

19.0.3.6.0 — vendor matching by name also works on the older 19.0 core.

19.0.3.5.0 — one vendor matcher shared by every import path; no behaviour change.

19.0.3.4.0 — the module detects which 19.0 import pipeline it runs on and warns
when it recognises neither.

19.0.3.3.0 — an invoice that prints no currency gets the currency of the country
both parties are in, and the chatter says so.

19.0.3.2.0 — the bill shows how many bills the vendor already has.

19.0.3.1.0 — the bill's preview shows the vendor's PDF, not a generated
placeholder.

19.0.3.0.0 — no vendor is created automatically any more. The import matches
vendors also on phone number and email domain, proposes candidates and vendor
data suggestions, and a bill without a match is left without a vendor for a
person to choose. Upgrade: the migration seeds the partner identifiers.

19.0.2.4.0 — the extracted amounts are checked against each other; a missing
amount is no longer read as 0.00.

19.0.2.3.0 — the currency and the document type (invoice or credit note) are
recognised in any language.

19.0.2.2.0 — dates and numbers are read according to the document's country.

19.0.2.1.0 — Peppol endpoint schemes come from Odoo's own map.

19.0.2.0.0 — US invoices: routing and account numbers, state and remit-to
address are read. Older stored extractions simply have no US bank details.

19.0.1.3.0 — review fixes after the first version.

19.0.1.2.0 — first version in this repository.
