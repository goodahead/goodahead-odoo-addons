# `goodahead_invoice_import_apix` — changelog

Newest first. Test counts are the gate at that version.

**Release gate (18.0):** 2,043 tests, 0 failed.

---

18.0.1.10.4 — security fixes.

### Security

- Neutralizing a database copy also clears every package's storage key.
- A package's storage key is cleared once APIX confirms the package as
  received; a later listing that shows it unreceived brings it back.
- Nothing is marked received while receiving is off.
- Upgrade: `-u goodahead_invoice_import_apix`.

---

18.0.1.10.3 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_import_apix`.

---

18.0.1.10.2 — README: where the APIX connection menu is.

- README names *Accounting ▸ Configuration ▸ E-invoicing ▸ APIX
  connection*. No code change.

---

18.0.1.10.1 — first 18.0 release of 19.0.1.7.0 to 19.0.1.10.1 (listed below), for Odoo 18.0.

- No behaviour differs on 18.0. The channel's pre-migrate is
  `migrations/18.0.1.8.0`.

19.0.1.10.1 — the APIX to-dos button works from the inbox.

- *APIX to-dos* above the incoming invoices list raised a server error when
  pressed: the web client calls a list header button with the selected
  records, and the method accepted none. It now ignores the selection.

---

19.0.1.10.0 — release check.

- Release check: a ticket id / vendor name removed from comments and docs. No code change.

---

19.0.1.9.0 — the copy warning in mirror mode.

- `_apix_copy_warning()` answers the connection's copy warning while the
  company receives in mirror mode (a copy holding production credentials):
  sending surfaces warn that sending reaches real customers. Nothing is
  blocked; the receiving neutralization is unchanged.
- Needs `goodahead_apix` 19.0.1.5.0 (neutralization also clears the
  credentials there).

---

19.0.1.8.0 — the `root.apix` channel moves to the connection.

- The `root.apix` job channel now belongs to `goodahead_apix`
  (`goodahead_apix.channel_apix`, its 19.0.1.4.0), shared with sending. The
  receiving jobs run on it as before; the channel record and its jobs are not
  touched.
- Upgrade: `-u goodahead_apix,goodahead_invoice_import_apix`. A pre-migrate
  script makes the same move when receiving is upgraded alone.

---

19.0.1.7.2 — APIX jobs run in their record's company.

- The package import, recognition, shadow classification and mark-received
  jobs are queued in the company of the import record, whoever started them.

19.0.1.7.1 — APIX job titles name the bill.

- The titles of the package import, recognition, shadow classification and
  mark-received jobs carry what the APIX listing tells before the import:
  seller, invoice number, amount, document date and the storage id. The job
  list can be searched by any of them. Jobs queued before the upgrade keep
  their old titles.

19.0.1.7.0 — mirror mode, for a test or staging copy next to production.

- **New receiving state *Mirror*** (buttons *Start mirror* / *Stop mirror* on
  the APIX connection, from off or shadow only, never from live). Every
  listed package becomes a draft bill, also those APIX shows as received
  (no recognition), and nothing is ever marked received at APIX, so the
  production receiver still gets every package. A warning banner shows on
  the connection while it is on. No daily checks run in mirror mode.
- **New field `apix_mirrored`** on the import record: set when a record is
  settled in mirror mode. Such a record is never marked received, not even
  after receiving goes live: it is left out of the unconfirmed marks, the
  reconciliation and the mark-received job.
- An Accountant's Retry / import of an APIX record also works in mirror mode.

19.0.1.6.1 — hook after a package is settled (same as 18.0.1.6.0).

---

18.0.1.6.0 — the migration tool moves to its own add-on (as 19.0.1.6.0).

- **The migration tool is now `goodahead_invoice_import_apix_migration`**: the
  wizard, its menu and action, the *Migration tool* button, the access rule
  and the tool's tests moved there. This add-on no longer knows the community
  APIX modules or any other earlier connector (`COMMUNITY_MODEL`,
  `community_installed`, the `_community_*` hooks are gone).
- **Two hooks** instead, answered by the migration add-on:
  `goodahead.apix.connection._apix_receiving_via_migration()` (False here;
  when True, going live and leaving live happen through
  `_apix_set_receiving_state` only, as before with the community modules) and
  `goodahead.invoice.import._apix_bill_from_earlier_receiver()` (the bill an
  earlier connector recorded for the package; nothing here). The guards on
  the receiving state are unchanged.
- **Recognition asks the earlier connector first:** the shadow classification
  and the live recognition of packages APIX shows as received take the
  earlier connector's own record of the package (when it has one) before any
  download, then the attachment checksum and the duplicate rule as before.
  Only a vendor bill or credit note of the company that is not cancelled is
  accepted from it.
- **New on 18.0 (not yet on 19.0): a third hook,**
  `goodahead.invoice.import._apix_after_package_settled()`, called on the APIX
  records just settled (a bill saved or recognised, or closed without a bill)
  in the settling transaction, after the mark-received job was enqueued.
  Nothing here; a migration adapter can use it to keep its earlier connector
  from reporting packages handled here as failed. See README.md,
  *Hooks for other add-ons*.
- Upgrade: install `goodahead_invoice_import_apix_migration` on databases that
  use the migration tool (it comes by itself with an adapter, e.g. where the
  community APIX modules are installed), then
  `-u goodahead_apix,goodahead_invoice_import_apix`. Nothing was deployed with
  the old layout, so there is no data migration.

18.0.1.5.1 — first release for Odoo 18.0, with the features of 19.0.1.5.1 (listed
below), including `data/neutralize.sql`.

- "One package, one record per company" is a partial unique index created in
  `init()` under the name 19.0 gives it (18.0 has no declarative partial index).
- The migration tool's form carries the company for every user, so its shadow-run
  counts are computed on 18.0 also for users without multi-company rights.
- The Administrator a to-do goes to is found through `groups_id`; the tool's
  action is restricted with `groups_id` (both `group_ids` on 19.0).
- The daily-check to-do texts are translated through `env._`.

19.0.1.5.1 — neutralized copies never receive.

- `data/neutralize.sql`: neutralizing a database (a test or staging copy of
  production) turns APIX receiving off for every company, so the copy cannot
  mark packages received on the live account and take them away from
  production. Turn it back on in the copy only against an APIX test account.

19.0.1.5.0 — review fixes: guarded receiving state, no import on stale statuses.

- **Receiving state is guarded wherever it is changed** (form, import, RPC):
  leaving live is refused while a mark-received is unconfirmed or a package or
  recognition job of the company is running. With the community APIX modules
  installed (or a connection migrated from them) going live and leaving live
  happen in the migration tool only; the radio is read-only there, with a hint.
- **Switch to live only enqueues a fresh run**, which lists the packages again,
  recognises the ones APIX now shows as received and only then imports the
  rest. A package job whose package the last listing shows as received (and
  that no Accountant asked to import) hands it to the recognition instead of
  billing it.
- **Switch back cannot race a running package job:** the job holds a share
  lock on the company's connection while it bills and re-reads the receiving
  state under it; the switch back takes the update lock first and is refused
  while it is held.
- **Migration tool, company isolation:** a record rule on the wizard and an
  access check in every migration method, before anything runs as superuser.
- **Shadow results show the vendor and the failure reason** (matched vendor,
  ambiguous or none; why a package would fail) in the inbox under *Shadow
  results* and on the record; cleared on going live.
- Records closed and reopened, or that an Accountant asked to import, are never
  turned *possibly lost* or imported by a run: they wait for an explicit Retry.
- Retry, re-import, *Import anyway* and *Import* of a possibly lost APIX record
  are refused unless the company is live.
- A package with several Finvoice messages fails as data with a plain reason
  (one package, one bill); the files are kept and closing it marks it received.
- Overlapping runs no longer fail on a package the other run created first.
- The last try of a package job records any error as an interrupted failure
  with a reason.
- The daily ageing check runs for every company in shadow or live, also with a
  rejected connection; only the reconciliation needs a working one.
- The load test is opt-in (`--test-tags apix_load`). `lib/apix_package.py` no
  longer imports from the ERP (the Finvoice sniff is passed in); a plain
  `unittest` test covers it without Odoo.
- Upgrade: `-u goodahead_invoice_import_apix`.

19.0.1.4.0 — migration tool from the community APIX modules.

- **Migration tool** (accounting administrators, one company at a time):
  detect the community connection and carry over environment, transfer
  credentials and Business ID (never the web password) into this add-on's
  connection, in shadow.
- **Shadow run** classifies every listed package without bills or marks:
  imported before (attachment checksum, then the duplicate rule with the
  seller-identifier fallback), would duplicate, would import, would be possibly
  lost, would fail. The tool shows the counts and warns before a week of
  shadow or while packages are unaccounted for.
- **Switch to live** pauses the community modules for the company (through the
  bridge add-on), clears the shadow results, recognises received packages that
  have a matching bill as imported and turns the rest into *possibly lost*
  records for review.
- **Switch back** is refused while any mark-received is unconfirmed; otherwise
  it lists what the community modules will pick up, sets receiving to off and
  resumes them.
- Package imports do nothing unless the company is live.
- The community modules are looked up by model name at runtime only: no
  install dependency and no import of their code.
- Upgrade: `-u goodahead_invoice_import_apix`. Tests: 982 without the
  community modules.

19.0.1.3.0 — inbox filters and APIX details on the import record.

- Inbox filters: shadow results, ageing out of the listing (same rule as the
  daily check), unconfirmed marks. An *APIX to-dos* button for
  administrators.
- Seller, invoice number and amount fall back to the APIX listing until the
  package is read. The storage key never appears on a screen.
- Upgrade: `-u goodahead_invoice_import_apix` (needs
  `goodahead_invoice_import` 19.0.1.6.0). Tests: 973.

19.0.1.2.0 — daily ageing and reconciliation checks.

- A daily job per company in shadow or live. **Ageing:** unsettled packages
  within 7 days of leaving APIX's current-and-previous-month listing raise one
  to-do with seller, invoice number, age and days left. **Reconciliation:**
  compares APIX's received flag with the inbox and reports settled records not
  yet received (re-enqueuing their mark in live), received packages unsettled
  or unknown to the inbox, and unsettled records gone from the listing — one
  to-do, refreshed daily and closed when clean. A failed listing skips the
  reconciliation without a second failure to-do.
- Also in this version (shipped without their own version bump):
  - **Mark received only after the bill commits.** The mark-received job is
    enqueued in the transaction that saved the bill or closed the record; it
    downloads with `markreceived=yes` and records APIX's confirmation. It
    skips confirmed records, records reopened before it ran and companies in
    shadow. Technical errors retry five times (1, 4, 16, 64 minutes); after
    that the record stays unconfirmed for the reconciliation.
  - The APIX-only bank-account overrides were removed; the core's strict
    bank-account rule (`goodahead_invoice_import` 19.0.1.5.0) applies instead.
- Upgrade: `-u goodahead_invoice_import_apix`. Tests: 67 for the add-on.

19.0.1.1.0 — package import job.

- Each waiting package is downloaded **without** marking it received; its
  files are kept on the import record; the Finvoice message is translated and
  imported with the strict matching policy: a draft bill or credit note
  (positive amounts) on the vendor matched by VAT number or Business ID. The
  Finvoice message is the main document unless a PDF image came with it; other
  files are attached; chatter *Imported from APIX*.
- Technical failures retry three times, then fail as *interrupted* and are
  picked up by the next run. Data errors fail visibly without retry. A row lock
  and the settled check keep one bill per package.
- New dependency: `goodahead_invoice_import_finvoice`.
- Upgrade: `-u goodahead_invoice_import_apix`. Tests: 906 with every add-on.

19.0.1.0.1 — company rule moved to the core.

- The multi-company record rule on import records now lives in
  `goodahead_invoice_import` (19.0.1.3.0); upgrading removes this add-on's
  copy.
- Upgrade: `-u goodahead_invoice_import_apix`.

19.0.1.0.0 — first version: receiving state and the Scheduler run.

- Per-company **receiving state** off / shadow / live on the APIX connection;
  an hourly Scheduler that enqueues one run per working company; **Fetch
  now**.
- A run lists the packages and keeps one import record per company and storage
  ID (partial unique index). Packages still being created at APIX are not
  fetched.
- Rejected credentials stop fetching until a successful check. Empty,
  unreadable or unreachable answers are failed runs, never "no invoices".
  Listing cap warning at 500 packages. One open administrator to-do per
  condition, closed when it clears.
- Storage keys are readable by accounting administrators only and kept out of
  the listing data and exports.
- Install: `-i goodahead_invoice_import_apix`. Tests: 16.
