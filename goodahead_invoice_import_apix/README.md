# Invoice Import — APIX receiving (`goodahead_invoice_import_apix`)

Fetches Finvoice e-invoice packages from the APIX e-invoicing operator for every
connected company and imports each one as a draft vendor bill on the right
existing vendor, through the invoice import core (`goodahead_invoice_import`)
and the Finvoice source (`goodahead_invoice_import_finvoice`). No AI is used.

- **Every package is visible.** Each package APIX lists gets one entry in
  *Accounting ▸ Vendors ▸ Incoming invoices* with its status: waiting,
  imported, duplicate, failed, possibly lost, closed without bill. Packages
  still being created at APIX show as waiting and are not fetched yet.
- **No lost invoice.** A package is marked received at APIX only **after** its
  bill is saved, or after an Accountant closed it without a bill. If an import
  fails or stops part-way, APIX still lists the package and the next run tries
  again.
- **No double bill.** One import record per company and APIX storage ID, one
  background job per package; a package listed twice, or by two runs at once,
  gives exactly one bill.
- **No duplicate vendors.** Only the seller's VAT number or Business ID sets the
  vendor; weaker matches are offered as candidates; a vendor is never created
  or overwritten.
- **Each company on its own.** The Scheduler runs one job per company; one
  company's failure never stops another's invoices, and a bill only ever lands
  in the company whose connection fetched it.
- **Shadow run** that lists and classifies packages without billing or marking
  anything, used by the separate migration tool
  (`goodahead_invoice_import_apix_migration`) to move a company over from an
  earlier APIX connector, with a way back.

## Which add-ons to install

| You need | Install | What comes with it |
|---|---|---|
| PDF invoices read with AI | `goodahead_invoice_ai_import` | `goodahead_invoice_import`, `goodahead_claude` |
| Finvoice files by upload or email | `goodahead_invoice_import_finvoice` | `goodahead_invoice_import` |
| APIX e-invoices | `goodahead_invoice_import_apix` | `goodahead_invoice_import`, `goodahead_invoice_import_finvoice`, `goodahead_apix` |
| Moving a company over from an earlier APIX connector | `goodahead_invoice_import_apix_migration` | its adapter for that connector installs itself next to it (community APIX modules: `goodahead_invoice_import_apix_migration_community`) and brings the migration tool along |

This add-on knows **no other APIX connector** and has no install dependency on
any. If a database still has an earlier APIX connector installed, make sure
its migration adapter is installed too (it installs itself for the community
modules, or for any other earlier connector it has an adapter for) before turning receiving on: only then is receiving switched live
through the migration tool, which pauses the earlier connector.

## Configuration

### 1. Connect the company to APIX

See the `goodahead_apix` README. In short: on the company's APIX connection
(*Accounting ▸ Configuration ▸ E-invoicing ▸ APIX connection*),
choose the environment, enter the Business ID, then paste the transfer ID and
key or use **Fetch from APIX login**, and press **Check connection** until the
status is *Working*.

### 2. Choose the receiving state

On the same connection form, **Receiving**:

| State | What happens |
|---|---|
| **Off** (default) | Nothing is fetched. The connection stays usable for other APIX functions. |
| **Shadow** | Packages are listed and classified (would import, would duplicate, imported before, would be possibly lost, would fail). No bill is created and nothing is marked received. Used by the migration tool. |
| **Mirror** | For a test or staging copy next to production (buttons *Start mirror* / *Stop mirror*, from off or shadow only). Every listed package becomes a draft bill, also those APIX shows as received, and nothing is ever marked received, so production still gets every package. Bills settled in mirror mode are never marked, not even after going live. No daily checks. |
| **Live** | Packages are imported as draft bills and marked received after the bill is saved. |

When the migration tool says so (an earlier APIX connector is installed, or
the connection was migrated from one), the radio is read-only: going live and
back happen in the migration tool, which pauses and resumes the earlier
connector.

**Fetch now** starts the same run the Scheduler would, for this company only,
following its receiving state.

### 3. Scheduler and jobs

- **APIX: fetch e-invoices** — recurring every 60 minutes (adjust the interval
  in *Settings ▸ Technical ▸ Scheduled Actions*). It only enqueues one run per
  company whose connection is *Working* and whose receiving state is not off.
- **APIX: daily ageing and reconciliation checks** — once a day, per company in
  shadow or live.
- All APIX jobs run on the `root.apix` job channel (owned by
  `goodahead_apix`, shared with sending), separate from the AI channel, so an
  AI backlog never delays e-invoices. Its capacity is a `queue_job` deploy
  setting (for example `root.apix:1`, or `root.apix:3,root.apix.send:2` with
  sending).
- Retries: a package import retries technical errors three times (after 1 and
  5 minutes), then is recorded as *interrupted* and enqueued again by the next
  run. Invalid invoice data fails visibly and is not retried automatically.
  The mark-received job retries five times (after 1, 4, 16 and 64 minutes);
  after that the daily reconciliation reports it.

### 4. Administrator to-dos

Problems reach the company's accounting administrators as to-dos on the APIX
connection — **one open to-do per condition**, not one per run, closed when the
condition clears:

| To-do | When |
|---|---|
| Credentials rejected | APIX rejects the transfer credentials. Fetching stops for the company until a connection check succeeds. |
| Run failed | APIX is unreachable or answers with something that is not a valid listing. An empty or unreadable answer is never taken as "no new invoices". The next run retries automatically. |
| Listing cap | The listing returned 500 packages, the most APIX returns; some packages may not be visible. Every listed package is still processed. |
| Ageing | Unsettled packages are within 7 days of leaving APIX's listing window (current and previous month), with seller, invoice number and age. |
| Reconciliation | APIX's received flag and the inbox disagree (a settled record not yet marked received, a received package unknown or unsettled in the inbox, an unsettled record gone from the listing). |

### 5. Working the inbox (Accountants)

- **Failed** — read the reason; **Retry** re-imports from the kept data
  without downloading again.
- **Duplicate** — the record links to the existing bill. **Create bill
  anyway** or **Close without bill**.
- **Possibly lost** — APIX shows the package as received but no bill exists
  (found at migration). **Import** it or **Close without bill**.
- **Close without bill** (reason required) marks the package received at APIX,
  so no run and no other receiving module imports it again. **Reopen** if it
  was a mistake.
- APIX filters: shadow results, ageing out of the listing, unconfirmed marks.

### 6. Packages received by someone else

A package APIX shows as received but that has no bill here (an earlier
connector received it, or another system) is never imported automatically.
The run makes it *possibly lost* and a recognition job looks for its bill:

1. the bill the earlier connector itself recorded for the package, when the
   migration tool's source for it keeps such a record (no download);
2. a vendor bill carrying the package's Finvoice message or invoice image;
3. the duplicate rule (vendor and invoice number, or the seller's VAT number
   or Business ID).

A match links the bill (imported, never imported again); no match leaves the
record for an Accountant. The shadow run classifies the same way.

The migration tool itself — detect, carry over, shadow counts, switch to live
and back — is the add-on `goodahead_invoice_import_apix_migration`; see its
README.

## Hooks for other add-ons

This add-on calls three methods that do nothing here, for the migration tool
and its source adapters to answer:

| Hook | Model | Here | Answered by |
|---|---|---|---|
| `_apix_receiving_via_migration()` | `goodahead.apix.connection` | False | the migration tool: True while a migration source is installed or the connection was migrated; live and back then go through `_apix_set_receiving_state` only |
| `_apix_bill_from_earlier_receiver()` | `goodahead.invoice.import` | no bill | the migration tool: the bill the earlier connector recorded for the package (its source's `find_bill`); only a vendor bill or credit note of the company that is not cancelled is taken |
| `_apix_after_package_settled()` | `goodahead.invoice.import` | nothing | e.g. a migration adapter, to tell the earlier connector a package was handled here |

`_apix_after_package_settled()` is called on the APIX records that were just
settled — a bill saved (by the package import, a retry, an override of a
duplicate) or recognised, or the record closed without a bill — in the
settling transaction, after the mark-received job was enqueued. An override
must not raise (that would undo the bill or the close): it works in a
savepoint, logs what it could not do, and never calls APIX. *Added in
18.0.1.6.0; to be carried to 19.0.*

## Security

- The receiving state, **Fetch now** and the to-dos are for accounting
  administrators. Accountants see the inbox and the connection
  status of the companies they are allowed in.
- Storage keys (the per-package secret APIX returns in the listing) are
  readable by accounting administrators only, are kept out of the listing data
  and exports, and never appear in logs, job results or the chatter. The same
  holds for transfer credentials (see `goodahead_apix`).
- Everything downloaded from APIX is untrusted vendor data: drafts only, the
  duplicate rule, no vendor creation or overwrite.
- A neutralized database copy turns receiving off here and loses its
  transfer credentials in `goodahead_apix`. In mirror mode the connection's
  `_apix_copy_warning()` answers, so every sending surface warns that this
  copy holds production credentials.

## Known limitations

- **More than 500 unreceived packages:** the APIX documentation says received
  packages are dropped first above 500 listed items. What APIX does when more
  than 500 packages are still unreceived has not been verified; the add-on
  warns at the cap and processes everything listed.
- **Scanned paper invoices** that reach APIX as Finvoice are not told apart
  from real e-invoices; they are imported the same way, and bank account and
  identifier differences only ever become suggestions.
- **Marking received** is done by downloading the package again with
  `markreceived=yes` (APIX has no separate acknowledge call). The digest of
  that call follows the APIX documentation but has not yet been confirmed
  against the APIX test environment.
- Only receiving is covered. An earlier connector's sending stays in use until
  a sending add-on exists.
- An earlier APIX connector without a migration adapter is invisible to this
  add-on: nothing stops the radio from going live next to it.

## Reference documentation

`docs/reference/README.md` lists the APIX and Finvoice documents this add-on is
written from, with their source URLs and what may ship publicly.

## Licence

LGPL-3.0 or later. Copyright Goodahead Oy — https://www.goodahead.com/
