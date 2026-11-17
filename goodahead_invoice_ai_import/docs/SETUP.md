# Setup

The items below are configuration the module deliberately does not ship. Each one
says why, so it is not "fixed" later by adding a data record.

The module enforces its own limits however this is configured; see
[What the module always enforces](#what-the-module-always-enforces).

---

## 1. Inbound email alias

The module ships no `mail.alias`. A module-created alias would sit beside the one
you choose as a second live inbound address, and every PDF that reaches it costs a
Claude call.

Create it under **Settings > Technical > Email > Aliases** (or from the Alias field
on the purchase journal):

| Field | Value |
|---|---|
| Alias name | e.g. `billing` (local part only) |
| Aliased model | **AI Invoice Import** (`ai.invoice.import`) |
| Alias domain | the instance's alias domain |
| **Default Values** | `{'company_id': <id>}` (see below) |
| Alias contact security | see below |

### One alias per company

In a multi-company database, create one alias per company that receives bills, each
with its own `company_id` in **Default Values**. That value is the only thing that
puts the import in the right company.

A separate incoming mail server per company does not achieve this.
`fetchmail.server` has no company field: it decides which mailbox is collected, not
which company the resulting record belongs to. Inside `message_new`, `env.company` is
the scheduler user's company for every message.

A bill imported into the wrong company does not fail: that company's accounts and
the shared expense categories resolve and the bill can be posted. An alias without
`company_id` still works and falls back to the alias user's company, but every
emailed import posts a note naming the company it was filed under and whether that
came from the alias or from the fallback.

Core's own `vendor-bills@` journal alias, if present, keeps using Odoo's native
importer; mail sent there does not go through AI extraction.

### Contact security

"Authenticated Partners" checks the `From` header, which can be forged, so it is not
authentication. Every accepted message can trigger a paid extraction. The module's
own limits (below) are what bound the cost.

Check that the alias domain (`mail.catchall.domain`) is correct before giving the
address to vendors; mail sent to a wrong domain is not bounced to anyone who will
notice.

---

## 2. Claude API key and model

The API key lives on the company (`res.company.claude_api_key`, Settings group) and
is managed by `goodahead_claude` under **Settings > Claude**.

1. Run **Sync Models** once before the first import, so the model registry has
   current models and release dates.
2. Set **Settings > Accounting > AI Invoice Import > Extraction Model** to a capable,
   PDF-enabled model. The dropdown lists only PDF-capable models. If left empty, the
   resolver picks the most recently released model, which may be a small tier that
   is less suited to reading invoices.

---

## 3. Extraction prompt (optional)

**Settings > Accounting > AI Invoice Import > Extraction Prompt** selects an
extraction prompt profile for the company. Left empty, the prompt is chosen from the
company's country: the United States block, the European Union block, or a
country-neutral fallback.

To customise the wording, open **Accounting > Configuration > AI Invoice Import >
Extraction Prompts**, duplicate a built-in profile and edit the copy. Built-in
profiles are read-only and refreshed on every upgrade.

---

## 4. Expense categories

The shipped categories are shared across companies (`company_id` empty); the
**account** is company-dependent, so each company sets its own.

For each company, switch into it and set the accounts under **Settings > Accounting
> AI Invoice Import > Configure Expense Categories**.

A category with no account for the bill's company does not fail silently: the line
falls back to the journal's default account and the bill's chatter says so, naming
the company.

---

## 5. queue_job channel capacity

The module declares the channel `root.ai_import`. Its capacity is set in `odoo.conf`,
not by a record:

```
channels = root:4,root.ai_import:1
```

Without it the channel inherits root's capacity, and a batch of uploaded PDFs can
occupy every worker for about a minute each. It is separate from `root.claude_api`
so a batch of invoices cannot starve other Claude features.

---

## What the module always enforces

These limits are in code, so they apply to any alias and cannot be switched off from
the UI:

| Guard | Default | System parameter |
|---|---|---|
| PDFs accepted from one email | 10 | `goodahead_invoice_ai_import.max_pdfs_per_message` |
| Largest PDF sent for extraction | 15 MB | `goodahead_invoice_ai_import.max_pdf_bytes` |

Anything refused by either limit is named on the import record's chatter.

Always on, not configurable:

* **Only purchase documents go to AI.** An upload into a sale journal stays on the
  native importer, so a supplier PDF never becomes a customer invoice carrying the
  supplier's bank details.
* **One Claude call per import.** A failure is terminal and the job is not retried
  automatically. Retry is a manual action, only from the `failed` state, and is
  refused once a bill exists.
* **Re-import is free.** The extraction JSON is stored, editable while the import is
  failed, and can be re-imported without a new API call.
* **A misread bank number never becomes an account.** IBANs are checked with mod-97
  and US routing numbers with the ABA checksum; a rejected routing number discards
  its account number too. A US account number has no checksum, so only the routing
  number is verified.
* **No vendor is created automatically.** An unmatched bill keeps an empty vendor
  and shows candidates; a contact is created only from the bill's button.
* **Bank account and tax id are never written to a vendor automatically.** They are
  offered as suggestions that a person applies one at a time.

Optional: **Settings > Accounting > AI Invoice Import > Domains That Never Identify
a Vendor** (system parameter `goodahead_invoice_ai_import.public_email_domains`)
adds email domains that must never identify a vendor, on top of the shipped list of
public mail providers. Leaving it empty keeps the shipped list. An entry written as
`-example.com` removes a shipped domain; if exactly one bill-eligible contact uses
that domain, every invoice printing an address there will match that contact.

---

## Trying it end to end

1. Upload a vendor PDF under **Accounting > Vendors > Bills > Upload**, into a
   purchase journal.
2. The upload returns an **AI Invoice Import** record rather than a bill; the work is
   queued. Imports are listed under **AI Invoice Imports**.
3. A queue_job worker must be running, or the record stays in `processing`.
4. On success the record reaches `done` and links the vendor bill. The bill's
   chatter shows the model used, token counts, and the extraction JSON as an
   attachment.
5. On failure the record goes to `failed` with the error, keeps its JSON, and appears
   in the systray bubble for Bookkeepers and Administrators. Correct the JSON and use
   **Re-import from JSON** to retry without a new API call.
