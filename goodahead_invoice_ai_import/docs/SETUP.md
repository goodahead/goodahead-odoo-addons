# Setting this up on a real instance

Everything here is **configuration the module deliberately does not ship**. Each
item says why, so nobody later "fixes" it by adding a data record.

The module enforces its own limits regardless of how any of this is configured —
see [What the module enforces whatever you do](#what-the-module-enforces-whatever-you-do).

The worked examples below describe a two-company installation; the companies
are called `Alpha LLC` (id 1) and `Beta Inc.` (id 2) here, and every domain is
a placeholder.

---

## 1. The inbound email alias — by hand, on purpose

**The module ships no `mail.alias`.** It used to ship `vendor-invoices@`; that
record is gone because each installation creates its own (for example
`billing@`) by hand, and
a module-created alias would sit beside it as a second live inbound address that
nobody expects — each message billing a Claude call per PDF.

To create it: **Settings → Technical → Email → Aliases**, or the Alias field on
the purchase journal.

| Field | Value |
|---|---|
| Alias name | `billing` (the local part only) |
| Aliased model | **AI Invoice Import** (`ai.invoice.import`) |
| Alias domain | the instance's alias domain |
| **Default Values** | `{'company_id': <id>}` — **see below, this one matters** |
| Alias contact security | see below |

### 🔴 ONE ALIAS PER COMPANY. This is not optional here.

⚠️ **A separate incoming mail server per company does NOT do this.** That is
worth stating because it sounds like it should, and an installation may well
have one incoming mail server per company configured.

Checked, not assumed:

* `fetchmail.server` has **no company field** — not in
  `addons/mail/models/fetchmail.py` at the deployed revision, and no company
  column on the `fetchmail_server` table. It puts only
  `default_fetchmail_server_id` on the context.
* So an incoming server decides **which mailbox is collected**, which is real
  and useful, and decides **nothing** about which company the resulting record
  belongs to. Inside `message_new`, `env.company` is the scheduler user's
  company — the same one for every message, from either mailbox.

**Core's own journal aliases solve it the right way for the native importer**,
and the same shape applies here. Each journal alias carries the company, for
example:

| Journal alias | `alias_defaults` |
|---|---|
| `vendor-bills` | `{'move_type': 'in_invoice', 'company_id': 1, 'journal_id': <Alpha purchase journal>}` |
| `vendor-bills-beta` | `{'move_type': 'in_invoice', 'company_id': 2, 'journal_id': <Beta purchase journal>}` |

A second alias is needed precisely because one address cannot serve two
companies.

Alpha LLC and Beta Inc. have **different charts of accounts**. The
import record's company defaults to the company the alias's user resolves to —
the **same company for every vendor**, whichever one actually sent the bill.

A bill imported into the wrong company **does not fail**. That company's
accounts resolve, the shared expense categories resolve, the bill posts, and
nothing anywhere says it is in the wrong books. It is the quietest way to get
this wrong, and it is only found at reconciliation.

### Create BOTH of these. The second one is the easy one to forget.

**Settings → Technical → Email → Aliases → New**, twice:

| Field | Alpha LLC | Beta Inc. |
|---|---|---|
| Alias Name | `billing` | `billing-beta` |
| Aliased Model | AI Invoice Import | AI Invoice Import |
| Alias domain | `alpha.example.test` | the Beta domain |
| **Default Values** | `{'company_id': 1}` | `{'company_id': 2}` |
| Alias Contact Security | your call — see below | your call — see below |

Company ids in this example are **1 = Alpha LLC**, **2 = Beta Inc.**
(look yours up under **Settings → Users & Companies → Companies**).

🔴 **`Default Values` is the field that does the work.** It is not decoration and
it is not optional: it is the only thing that puts the bill in the right books.
Checked on a test database, with two aliases and routed test mail:

| alias | `env.company` when the record is created | `Default Values` | record lands in |
|---|---|---|---|
| `zzprobe-alpha` | Alpha LLC | `{'company_id': 1}` | Alpha LLC |
| `zzprobe-beta` | **Alpha LLC** — the same | `{'company_id': 2}` | **Beta Inc.** |

The middle column is the whole point: the default company is Alpha LLC for
**both** aliases, so for one of them it is simply wrong, and only `Default
Values` corrects it.

ℹ️ The pass-through itself is long-standing behaviour, not something this
release added — an alias that names its company has always worked. What this
release adds is the **warning when an alias does not name one**, because that is
the configuration that misfiles quietly.

An alias with no `company_id` still works — it falls back to the alias user's
company — but it is not silent about it. Every emailed import posts a note
naming **which company it was filed under**, and says whether that was the
alias's decision or a fallback. A bill in the wrong company does not error
(that company's accounts resolve perfectly well), so the company is stated as a
fact on the record rather than left to be noticed at reconciliation.

🔴 **`alias_contact` is not authentication.** "Authenticated Partners" checks the
`From` header, and a `From` header is forgeable. On a large database that is
over a hundred thousand partners who can cause a billed extraction, plus anyone
willing to spoof one of them. Decide this with that in mind; the module's own limits below
are what actually bound the damage.

⚠️ **Check the alias domain is right before pointing a vendor at it.** A database
converted from another installation can carry `localhost` as
`mail.catchall.domain`. Mail sent to an alias
on the wrong domain is not bounced to anybody who will notice.

Core's own `vendor-bills@` alias on `account.move`, where it exists, keeps
the **native** inline importer — mail sent there does **not** go through AI
extraction. Two addresses, two behaviours; worth knowing before telling vendors
which to use.

---

## 2. Claude: API key and model

The key lives on the company (`res.company.claude_api_key`, group **Settings**)
and is owned by `goodahead_claude`, not by this module. **Settings → Claude.**

🔴 **Run "Sync Models" once before the first import.** On a database
converted from an earlier version the model registry can hold rows with no
release date, and the resolver then falls through to an old model the API no
longer lists. A sync fills the dates and brings in the current models.

Then set **Settings → Accounting → AI Invoice Import → Extraction Model** to
**`claude-opus-5-5`**.

⚠️ **Setting it is required, not optional.** Left empty, the resolver picks the
*most recently released* model, which is currently **Haiku 5.5** — the small
tier. That is the right default for fraud detection and the wrong one for
reading an invoice PDF.

⚠️ Separately: check `claude_default_model_id` on each company. If it points at
a retired model, fraud detection is unaffected (it pins its own
`fraud_model_id`), but "Test Connection" fails because of it. Worth clearing.

---

## 3. Expense categories — per company, because the charts differ

The thirteen shipped categories are **shared** across companies
(`company_id` empty). The **account** is `company_dependent`: one category row
holds a different account in each company.

**For each company**, switch into it and set the accounts:
**Settings → Accounting → AI Invoice Import → Configure Expense Categories.**

A category with no account **for the company the bill belongs to** is not a
silent failure — the line falls back to the journal default and the bill's
chatter says so, naming the company. That is deliberate: the previous behaviour
could not distinguish "this code does not exist" from "nobody set an account for
this company", and the second is the one two companies make likely.

---

## 4. queue_job channel capacity — deploy config, not a record

The module declares the channel `root.ai_import`. Its **capacity** is an
`odoo.conf` setting and is not something a record can carry:

```
channels = root:4,root.ai_import:1
```

Without it the channel inherits root's capacity, and a folder of uploaded PDFs
can occupy every worker for ~60 s each. It is a separate channel from
`root.claude_api` on purpose, so a batch of invoices cannot starve fraud
detection.

---

## What the module enforces whatever you do

These are **code**, not configuration, so they hold for any alias anybody
creates, and they cannot be switched off from the UI:

| Guard | Default | System parameter |
|---|---|---|
| PDFs accepted from one email | 10 | `goodahead_invoice_ai_import.max_pdfs_per_message` |
| Largest PDF sent for extraction | 15 MB | `goodahead_invoice_ai_import.max_pdf_bytes` |

Anything refused by either limit is **named on the import record's chatter**.
Silently dropping a vendor's attachment is how a bill goes missing without
anybody knowing there was one.

Also always on, and not configurable:

* **Only purchase documents route to AI.** A sale-journal upload stays on the
  native importer — otherwise a supplier PDF becomes an `out_invoice` and the
  supplier's bank details land on a document where the company is the payee.
* **One Claude call per import, ever.** A failure is terminal and the job is
  marked done rather than retried; retry is a human action, from the `failed`
  state only, and is refused server-side once a bill exists.
* **A re-import costs nothing.** The extraction JSON is stored, editable while
  failed, and can be re-imported without a new API call.
* **A misread bank number never becomes an account.** IBANs are checked with
  mod-97, US routing numbers with the ABA checksum, and a rejected routing
  number takes its account number with it.
  🔴 **A US account number has no checksum and cannot be verified.** A clean
  import does not mean the account number was confirmed — only the routing
  number was.

---

## Trying it end to end

1. Upload a vendor PDF: **Accounting → Vendors → Bills → Upload**, into a
   purchase journal.
2. The upload returns an **AI Invoice Import** record, not a bill — the work is
   queued. The record is under **Accounting → Vendors → AI Invoice Imports**.
3. A queue_job worker must be running, or the record stays in `processing`.
4. On success the record reaches `done` and links the **Vendor Bill**. The bill's
   chatter carries the model used, the token counts and the extraction JSON as
   an attachment.
5. On failure the record goes to `failed` with the error, keeps its JSON, and
   appears in the systray bubble for anyone with Billing access. Fix the JSON on
   the record and use **Re-import from JSON** to retry with no new API call.
