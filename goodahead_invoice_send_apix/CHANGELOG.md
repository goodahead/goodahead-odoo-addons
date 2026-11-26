# `goodahead_invoice_send_apix` — changelog

Newest first.

---

19.0.1.24.4 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_send_apix`.

---

19.0.1.24.3 — no install warnings; the APIX menus in the Setup steps.

- Brings 19.0.1.23.5 into this line: distinct labels for the four field
  pairs that shared one, the developer page of an attempt carrying the
  developer group of its fields (no access rights inconsistency), and the
  README Setup naming *Accounting ▸ Configuration ▸ E-invoicing ▸ APIX
  connection* and *APIX migration tool*.
- Tests: 2 new (no shared field labels on the add-on's models, the
  developer view validates without a warning).

---

19.0.1.24.2 — the sending fixes in the Finvoice content line.

- Brings 19.0.1.23.2–19.0.1.23.4 into this line: *Look up e-invoice
  address* on the contact and the company, the ledger handing back plain
  values (the "Record does not exist" fix of the first real send), the
  Business ID APIX found kept by the fix-it wizard (VAT number on request),
  and the warning for a buyer without a Business ID or VAT number.
- Readiness shows one warning for a buyer without any identifier: on a
  reverse-charge or intra-EU invoice the VAT number warning replaces the
  Business ID / VAT one (the VAT number clears both).

---

19.0.1.24.1 — review fixes of the Finvoice content.

- The price before discount of a line with a price-included tax is stated
  in five decimals (it was the ERP's base rounded to cents).
- Sub-section lines go as text rows like sections and notes.
- The delivery party carries the second street line.
- A tax whose exemption reason code or reason lines the e-invoice cannot
  hold (a tax the consistency check skips) sends without them; readiness
  warns *VAT settings left out* naming the tax. A sales tax refuses such a
  code.
- Readiness asks for the customer's VAT number also when a reverse-charge
  or intra-EU tax sits on a line next to a higher-rate tax, and names what
  a delivery address lacks as name, street, post code and town.
- The standing buyer reference on a customer is written under the ERP's
  contact rights only (the write path for billing users is removed);
  billing users set the buyer reference on the invoice.
- Tests: a round trip from sending to an imported bill over credit notes,
  price-included and 100 % discounts, prices per several units and the
  intra-EU and export categories.

---

19.0.1.24.0 — the e-invoice says everything the invoice knows.

- VAT breakdown by rate and VAT category; each no-VAT entry with its VAT
  reason and exemption reason code; every row with its category. A tax's
  category: the ERP's *Tax Category Code* when set, else standard rate
  above 0 %, else the one category its Finnish VAT report lines name, else
  unclassified (sent without a category, never guessed). Depends on
  `account_edi_ubl_cii` (LGPL-3).
- Taxes: *VAT Reason* (empty = the category's default wording), the shown
  category with *set / default / unclassified*, the filter *Unclassified
  0 % sales taxes*, the consistency check on sales taxes, and every change
  of category, code or reason in the chatter. Only accounting
  administrators change them.
- References: the customer's order number (customer reference less our
  echoed order numbers; a reversing credit note takes the reversed
  invoice's), our order number (withheld when it would read as a payment
  reference), the buyer reference; an over-long one is left out, never cut.
- Customers: *Standing Buyer Reference*; new customer invoices and credit
  notes start with it, a change of customer replaces it only while it is
  empty or the previous customer's.
- Delivery party, text rows for notes and headings, row discounts (price
  before discount, percentage and an amount that adds up to the cent),
  trade item numbers, invoice type 380 / 381, payment means, rows total.
- Readiness warns (never blocks) about unclassified 0 % taxes, lines
  without one clear category, shared VAT reasons, a missing buyer VAT
  number on reverse-charge and intra-EU invoices, references left out and
  incomplete delivery addresses.
- Nothing is written to taxes, customers or invoices on install or upgrade.

---

19.0.1.23.5 — no install warnings; the APIX menus in the Setup steps.

- Distinct labels for four field pairs that shared one (install
  warnings): send attempt *Finvoice message file name* / *Package file
  name* (both were "Name") and *Background job id and state* (was
  "Background job", like the job link); fix-it wizard *Company to fix*
  (was "Company", like the computed company); lookup result *Listed
  operator* (was "Operator", like the operator name).
- Developer view of an attempt: the *Technical* page carries the
  developer group of its fields, so the elements that test them are
  consistent with the field rights (no access rights inconsistency).
  Still shown in developer mode only.
- README Setup names *Accounting ▸ Configuration ▸ E-invoicing ▸ APIX
  connection* and *APIX migration tool*.
- Tests: 2 new (no shared field labels on the add-on's models, the
  developer view validates without a warning).

---

19.0.1.23.4 — the Business ID from the lookup; a clearer warning without one.

- Fix-it wizard (invoice, contact and company): when Save uses an address
  APIX found, the Business ID APIX gave with it goes to the commercial
  partner's Business ID (company mode: the company's) only when that is
  empty — never over an existing value. For a Finnish Business ID and a
  target without a VAT number, the box "Also set VAT number FI…" (off by
  default) also saves FI + its digits, again only when empty. Entry by hand
  fills neither; no other path writes them.
- Readiness: an e-invoice to a customer (contact, else commercial partner)
  with neither a Business ID nor a VAT number still warns only (never
  blocks), and now says that APIX accepts it but notes "HUOM: Ostajan
  Y-tunnus/ALV-tunniste puuttuu", and where to fix it: the customer's
  Business ID or VAT number, or *Look up e-invoice address*.

---

19.0.1.23.3 — sending works outside tests: the first real "Send via APIX"
failed with "Record does not exist or has been deleted" on the send attempt.

- The ledger commits each step on its own cursor, while the request (or
  job) runs in a transaction whose snapshot predates that commit: the
  attempt it had just inserted did not exist for the caller, and a changed
  one still read as before. The ledger's claim, finish and record now hand
  back a plain `AttemptView` (state, batch id, document id, credits, reason,
  warnings) read on the ledger's cursor before it commits; the send's
  outcome, the notification, Send & Print and the background job's result
  come from it, never from reading the attempt in the caller's transaction.
- A background job reported its own send as "queued" (the job's snapshot):
  it now reports the outcome the ledger recorded.
- "Cannot cancel" names the state as committed now, read on the ledger's
  cursor.
- A connection check that finds a paused connection working ends the pause
  in the check's own transaction: the ledger's cursor would have waited on
  the row the check had just written until its lock timeout.
- Tests: `test_ledger_own_cursor` runs the button (accepted, refused,
  failed), Send & Print, a background job, the mass wizard's send now and a
  cancel with the ledger on real cursors over committed invented data.

---

19.0.1.23.2 — e-invoice address lookup from the contact and the company.

- *Look up e-invoice address* on the contact form (Sales & Purchase tab,
  under the e-invoice address and operator), for billing users who may
  change the contact: opens the fix-it wizard for that contact, looked up
  with the APIX connection of the user's current company; Save writes the
  address and operator as a pair to that contact only.
- The same button on the company form, for accounting administrators: the
  company's own e-invoice address and operator, looked up with that
  company's connection.
- The fix-it wizard takes one target: an invoice (as before), a contact or a
  company. Every action first checks that the user may change the target
  (the contact; for the company an accounting administrator who may change
  it), and that the lookup company is one of the user's companies, before
  any APIX call. The delivery-method check stays with invoices.
- No working connection: the wizard says so and entry by hand still works.
  Nothing is written before Save and nothing is sent.

---

19.0.1.23.1 — review follow-ups: only a press of the Send & Print dialog's
Send button sends through APIX (code calling the wizard gets "Not sent
through APIX"); a queueing conflict without a send in progress is retried,
not shown as in progress; a delivery history line that cannot be written
after the transaction is retried once, then noted on the invoice; clearer
refusal for a connector without a sending adapter.

---

19.0.1.23.0 — review fixes.

- APIX is called from Send & Print only when a person sends from the
  dialog: the ERP's batch cron and code calling the ERP's sending get the
  invoice error "Not sent through APIX" instead.
- An OK answer with no accepted document is a failed send
  (`send.unexpected`, "may already have reached APIX"), never "sent".
- The e-invoice address and its operator come as one pair from the invoice
  contact, else from the commercial customer.
- A download never becomes the invoice's latest send: a failed send stays
  in the failed-sends list and the bubble; one latest per invoice.
- Emails and downloads are recorded once the caller's transaction commits
  (a rolled-back send leaves no "sent by email"); the background job's
  email ends sent on commit, failed on rollback. An email locks the invoice
  row; a send claim that waits past the lock timeout answers "in progress".
- The mass wizard's include toggle is read-only on already-sent invoices
  (included through "Include" after its warning).
- The fix-it wizard checks the user may change the invoice on create and
  on every action, before any APIX call.
- The sending filters use SQL subqueries; readiness is worked out in
  batches (1,000 unsent invoices: Ready 0.80 s → 0.12 s).
- "Send via APIX" is hidden on invoices that go by email.
- Readiness warns only about units the send leaves without a code.
- Switching sending on is refused while an earlier connector without a
  sending adapter is found for the company (message from the migration
  bridge).

---

19.0.1.22.0 — release notes.

- README rewritten for the release: what it does (transmit method,
  readiness, the button, Send & Print, the mass sending wizard, background
  sends, the Delivery tab, failed sends, the developer view), setup (the
  connection, sender details, earlier setups first, the sending switch and
  background threshold, customers, units), job channel capacity
  (`root.apix:3,root.apix.send:2`), readiness rules, troubleshooting,
  database copies, install and tests. The "under construction" note
  removed. No code change.

---

19.0.1.21.0 — volume, concurrency, isolation and secret-leak suites.

- `test_nothing_until_send`: posted documents (one in ten a credit note,
  created and posted in chunks of 100; 200 in every run, 1,000 in the
  pre-release run) → 0 attempts, attachments, jobs and APIX calls; the list
  filters "Ready to send" / "Not ready" over them are timed (1,000: ≈ 0.7 s /
  0.02 s here; budget 30 s).
- `test_concurrency`: every door (button, Send & Print, the ERP's email, the
  mass wizard, the transmit method) refused with who and when while a send
  is queued, free again once it ended; a user's claim raced against a
  background queueing on two real connections (one wins, never two in
  progress).
- `test_company_isolation`: one company's refused credentials fail its sends
  alone, then fast as paused.
- `test_secrets`: no transfer id, key or digest in the run's log, the
  stored exchange, attempt files, chatter or job records.
- `test_lib_purity`: no ERP import under any platform `lib/`.
- `test_load` (`pre_release`, not in the standard run): 1,000 background
  sends against a 10 s recorded stub scaled by `GOODAHEAD_LOAD_TIME_FACTOR`
  — terminal states only, no duplicate, projected ≈ 5,000 s at capacity 2
  (budget 3 h); channel topology keeps receiving's slot. No code change.

---

19.0.1.20.0 — the developer view and "Regenerate and check".

- developer view: its own attempt form
  (`goodahead_invoice_send_attempt_view_form_technical`, opened by
  `action_open_technical()` — developer mode only, from the attempt form
  header and the delivery tab row): Finvoice message and package downloads
  (`message_file` / `package_file`, related, developer-only), masked APIX
  exchange, schema result and problems, units sent without a code, the job
  (`queue_job_label`; the link for job managers). The ordinary attempt form
  names no developer field, so it opens for everyone (19.0 keeps developer
  nodes in the arch and only hides them).
- `account.move._apix_regenerate_check()` → `{message, schema_result,
  problems, units_without_code}` and `action_apix_regenerate_check()` (header
  button, developer mode): the dialog `goodahead.invoice.send.regenerate`
  keeps the message in its own transient row — no attempt, attachment, job
  or APIX call.
- Tests: 7 new.

---

19.0.1.19.0 — failed sends in the attention bubble, the failed-sends list, Mark handled.

- `models/einvoice_attention.py`: section `failed_sends`
  (`account.group_account_invoice`) of the shared bubble — invoices whose
  latest send was refused or failed and is not handled, in the user's
  active companies; the 10 newest items open the invoice on its Delivery tab
  (`goodahead_delivery_tab` context: `account.move._get_view` makes that
  page the first one shown; the view cache key carries the flag).
- `action_failed_sends` (menu Customers › Failed sends): list with
  invoice, customer, company, channel, outcome, reason, when, started by;
  filters Refused / Failed / Ran too long, group by company or reason;
  "Open invoice" and "Mark handled" per row. Attempt `partner_id` (related).
- `goodahead.invoice.send.mark.handled`; attempt
  `action_open_mark_handled()` and `_mark_handled(reason)` (through the
  ledger, row-locked): `send.handled_reason_required`, `send.not_latest`.
  The delivery tab gets "Mark handled" on a row that needs attention.
  A later refused / failed send brings the invoice back; a later success
  keeps it off.
- `split_reason()` moved to the attempt model (the mass wizard uses it).
- Tests: 11 new (two companies; Accountants see no connection trouble).

---

19.0.1.18.0 — several invoices from Send & Print; email and download recording.

- `adapters/account_move_send_batch.py`: the ERP's batch Send & Print with
  more than one invoice and APIX among the methods opens the mass sending
  wizard for the APIX invoices (send now or background — nothing goes
  through APIX later without that choice); the others continue in the ERP's
  batch flow, remembered as Send & Print sends (`sending_data`
  `goodahead_origin`).
- `adapters/account_move_send.py`: each email of the ERP's sending (dialog,
  batch cron, or outside this add-on = `erp_send`) is an `email` attempt
  claimed through the ledger: `sent` (counts), `failed` with
  `send.email_failed` when the mail step raises, refused with
  `send.in_progress` while an APIX send of the invoice runs (one invoice:
  raised; several: skipped with a chatter note). A send that only produced
  the PDF is a `download` attempt (`downloaded`, never counts). Background
  email jobs record their own attempt (`goodahead_send_recorded`).
  `is_move_sent` is still never read.
- Tests: 12 new.

---

19.0.1.17.0 — live status of the mass wizard's send now, retry.

- `goodahead.invoice.send.wizard.get_run_status()` (`@api.readonly`):
  `{run_uuid, total, done, waiting_for_worker, lines}` — one row per invoice
  (its latest attempt of the run, read fresh by `_ledger_run_status`), plus
  the invoices another send held when the run was queued ("In progress —
  …"); only invoices the user may read. `waiting_for_worker`: some rows
  queued and no send of the run started for 30 s.
- `goodahead.invoice.send.wizard.line.action_retry()`: a refused or failed
  row of the sending wizard is checked again and queued again under the same
  run with send-now priority.
- Field widget `apix_send_run_status` (sending): polls every second
  through the ORM service, stops when every row ended or the dialog closes;
  three failed polls show "Status unavailable — the invoices keep sending in
  the background"; Fix and Retry per refused / failed row; the done summary.
- Tests: 11 Python (incl. 20 invoices, each poll < 1 s) + 6 widget unit
  tests (hoot, run by a browser test).

---

19.0.1.16.0 — the delivery tab, the invoice list and contact readiness.

- Invoice form "Delivery" page: `account.move.send_attempt_ids`
  (read-only, newest first) with when, started by (or "background, queued
  by …"), channel, outcome badge, counts as sent, reason / warnings, batch
  id, document id, credits; "Cancel" on a queued row; the pre-install mark
  as a banner line (not a row); "Not sent yet" (`delivery_empty`).
- `goodahead.invoice.send.attempt`: `display_date`, `started_by_label`;
  read-only list and form views (create / edit / delete off).
- Invoice list: optional "Transmit method" and "Sending" badge
  columns; filters "Ready to send", "Not ready", "Failed sends", "Not sent
  yet" — `send_state` and `already_sent` are searchable (in progress,
  refused / failed and sent from the history; ready / not ready by the
  readiness rules over the remaining posted customer documents).
- Contact form: readiness banner for the contact's own channel in
  the current company ("Ready to receive e-invoices", or what is missing and
  where it is fixed); `res.partner.send_readiness_level` /
  `send_readiness_html`. The readiness line is one helper
  (`readiness_display`) for invoices and contacts.
- Tests: 11 new (two-company list).

---

19.0.1.15.0 — the mass sending wizard.

- `goodahead.invoice.send.wizard` (+ `.line`, transient; `review`,
  `sending`, `queued`): opened from the invoice list's "Send e-invoices…"
  on the selected invoices the user may read (other companies never reach
  it). One line per invoice with its company and channel (transmit
  method), classified from stored data: ready (included), not ready (the
  missing items, "Fix"), already sent (when and how, or before the add-on;
  included only after the warning, per line or all), in progress (holder),
  not sendable — sending off (e-invoice / letter of a company switched
  off; its emails stay ready), skipped, draft or cancelled.
- The copy warning of every company in the selection; the background is
  recommended above the lowest background threshold of those companies
  (read with sudo); both choices always offered, none while nothing is
  sendable.
- `action_send_now()` / `action_send_background()`: re-check, then
  `_apix_queue` per company under one new run (`mass_now` priority 10, the
  wizard stays open; `mass_background` priority 20, "N invoices queued"
  and close). An invoice started elsewhere meanwhile is shown in progress.
  Closing the wizard changes nothing.
- Fix-it wizard: returning to the mass wizard re-classifies the fixed
  invoice's line (`_apix_after_fix`).
- Tests: 14 new.

---

19.0.1.14.0 — the sweeper and the sending neutralization.

- `goodahead.invoice.send.attempt._ledger_sweep(now=None) -> int` (own
  ledger transaction, rows locked `SKIP LOCKED`): fails every send still
  `running` 10 minutes after it started (`send.ran_too_long`) and every
  `queued` send whose job is failed, cancelled, done or gone
  (`send.job_lost`, "nothing was sent"); a job still pending, enqueued or
  started is left alone. Overlapping runs fail each attempt once.
- `data/ir_cron.xml`: "APIX sending: release hung and lost sends", every
  minute (`apix.send.sweep.v1`).
- `data/neutralize.sql` (run by the ERP's neutralization, not at install):
  sending switched off for every connection; queued sends cancelled
  (`send.cancelled_by_neutralization`, end date, no attention) with their
  jobs; running sends left to the sweeper.
- Tests: 10 new.

---

19.0.1.13.0 — APIX in the ERP's Send & Print (one invoice).

- `adapters/account_move_send.py` (19.0): methods `apix` / `apix_letter`
  offered for a company whose sending is on (`_is_applicable_to_company`),
  usable for a posted customer document ready for that channel
  (`_is_applicable_to_move`); preselected by the transmit method in place of
  the customer's ERP setting (`einvoice` → `apix`, `letter` → `apix_letter`,
  `email` → email); the choice never changes the transmit method.
- Alerts (stable keys): `apix_not_ready` (warning while not ticked, danger
  once ticked; the missing items and a "Fix" action), `apix_in_progress`
  (danger), `apix_already_sent` (danger until "Send again anyway" is ticked,
  then warning), `apix_copy_warning` (warning), `apix_sending_off` (info).
- `_call_web_service_after_invoice_pdf_render`: `_apix_send_now(origin=
  "send_print")` with the PDF the dialog rendered; a refusal or failure is
  the dialog's error (the attempt stays recorded).
- `account.move.send.wizard`: `apix_resend_confirmed` tick box; an accepted
  APIX send answers with the button's success notification.
- Tests: 13 new.

---

19.0.1.12.0 — background sends.

- `account.move._apix_queue(run_uuid, origin, priority)`
  (`models/account_move_queue.py`): per invoice, under a savepoint of the
  caller's transaction, a `queued` attempt (channel from the transmit
  method, `run_uuid`, `queued_date`, requester, origin `mass_now` /
  `mass_background`) and one job in the invoice's company
  (`with_company(move.company_id)`, identity key `apix-send:<attempt id>`,
  priority 10 now / 20 background, `queue_job_id` kept on the attempt).
  Returns `{queued, in_progress: {move_id: Holder}}`.
- `account.move._apix_send_queued(attempt)` (the job): acts only when the
  ledger moves the attempt `queued → running`; re-checks posted, the
  switch (`send.sending_off_since_queued`), the pause (`send.company_paused`,
  no APIX call) and readiness (`send.not_ready_since_queued`, items named);
  sends e-invoices and letters as the button does, emails through the ERP's
  email sending (`send.email_failed` when it raises; attempt `sent`);
  another channel fails `send.channel_unsupported`; any exception is
  recorded as `send.unexpected` and the job ends normally.
- `data/queue_job_data.xml`: channel `root.apix.send` under
  `goodahead_apix.channel_apix`; job function without a retry pattern.
- `goodahead.invoice.send.attempt.action_cancel()`: `queued → cancelled`
  with who and when, its job cancelled, the invoice free at once;
  `send.cannot_cancel` (`CannotCancel.code`) naming the current state.
  The ledger's insert now takes the state (`_ledger_insert`).
- Tests: 16 new (two companies: a failing company fails alone, a company
  switched off sends its emails only).

---

19.0.1.11.0 — "Send via APIX" on the invoice form.

- `account.move.action_apix_send()`: sends now through
  `_apix_send_now("button")` and answers with a sticky notification
  (accepted with batch and credits, letter via the print service, APIX
  warnings, refusals and failures with their reason) and a reload; a
  connection-level failure reads "the company's APIX connection is not
  working" for Accountants and the specific reason for Administrators; a
  not-ready refusal opens the fix-it wizard next when the e-invoice address
  can fix it; an invoice already sent opens the already-sent warning first
  (context `apix_resend_confirmed` after it).
- `goodahead.invoice.send.already.sent` (transient): when and how it
  was sent (or before the add-on, channel unknown); "Send again" / Cancel.
- `account.move`: `send_readiness_level`, `send_readiness_html` (the
  readiness line with each missing item and where it is fixed) and
  `send_state` (the sending badge: ready, not ready, in progress, sent,
  refused, failed). Not stored, no APIX call.
- Invoice form: "Send via APIX" (primary when ready; only for posted
  customer documents of a company whose sending is on), readiness banner
  with "Fix e-invoice address", copy-warning and in-progress banners, the
  transmit method next to the customer, the sending badge in the header.
- Tests: 20 new.

---

19.0.1.10.0 — sending one invoice now.

- `account.move._apix_send_now(origin, channel=None, resend_confirmed=False,
  pdf=None) -> SendResult` (`models/account_move_send.py`): checks (nothing
  written; `send.channel_unsupported` for an email invoice without an APIX
  channel) → claim → PDF → values → `finvoice.write` → schema gate
  (`send.schema_invalid`, nothing sent) → package → SendInvoiceZIP with the
  invoice company's connection → outcome through the ledger. Contract §10.4
  mapping: accepted (ValidateText kept as warnings), `send.refused`,
  `send.credentials_refused` / `send.no_contract` (connection rejected and
  paused), `send.no_credit` / `send.unreachable` (paused only),
  `send.no_answer` (may already be at APIX, resend free), `send.unexpected`
  (also any exception after the claim). An accepted send records the credit
  balance and ends a pause. Reasons start with `[code]`.
- Ledger: `_ledger_finish(..., files=None)` attaches the message and the
  package to the attempt in its own transaction; the masked exchange is kept.
- `tests/common.py`: `FakeApixSendClient` (accepted, warning, refused, ERR
  endings, unreachable, no answer, HTTP).
- Tests: 16 new.

---

19.0.1.9.0 — the fix-it wizard.

- `goodahead.invoice.send.fix` (+ `.result`, transient): look the
  invoice contact up at APIX (AddressQuery with the invoice company's
  connection), every address listed and none picked; entry by hand (address +
  an operator from the list); "Check delivery method" (DeliveryMethod, on
  request only, saves nothing); save writes `edicode` and
  `einvoice_operator_id` on the invoice contact (tracked, under the
  Accountant's name) and switches the invoice to e-invoice unless a send is in
  progress. Codes `fix.nothing_picked`, `fix.address_required`,
  `fix.operator_not_listed`, `fix.lookup_input_required` (raised as
  `FixError.code`); `fix.none_found`, `fix.lookup_unavailable`,
  `fix.delivery_method_unavailable` (wizard states); `fix.transmit_not_switched`
  (`outcome_code`). Lookup errors never touch the connection.
- `account.move.action_apix_open_fix()`; returns to the invoice or, with
  `return_to = mass_wizard`, to `return_res_model` / `return_res_id`.
- Access: Accountants (billing) use the wizard.
- Tests: 15 new.

---

19.0.1.8.0 — Finvoice values and the PDF image.

- `account.move._apix_finvoice_values(attempt_id=None, timestamp=None)`:
  the writer's values dict (+ `units_without_code`) from the invoice's
  own company and invoice contact; lines with real quantity, Rec 20 code,
  Finnish name and base quantity (a code-less multiple of a coded reference
  unit goes in that unit); VAT per rate; EPI with the payment reference only
  when it is a valid Finnish or RF reference; credit notes with
  `original_number` from `reversed_entry_id` only.
- `adapters/pdf.py`: `_apix_render_pdf()` renders `account.account_invoices`
  (the Finnish layout where installed).
- `tests/common.py` (`SendApixCommon`): two companies with sender details,
  bank accounts and working connections, invented customers and units.
- Tests: 11 new.

---

19.0.1.7.0 — readiness, already-sent and the sendable checks.

- `account.move` (not stored, stored data only, no APIX call):
  `send_readiness` / `send_ready` (for the transmit method),
  `send_in_progress` / `send_in_progress_info`, `already_sent`,
  `send_copy_warning`, `apix_send_available` (posted customer document of a
  company whose sending is on).
- `_apix_check_sendable(channel, resend_confirmed=False)`: the one check of
  every send door — `send.not_customer_document`, `send.not_posted`,
  `send.sending_off` (e-invoice and letter), `send.not_ready` (blocking items,
  never another channel), `send.already_sent` (unless confirmed),
  `send.in_progress` (holder). `_apix_already_sent_info()`: newest attempt
  counting as sent, else the pre-install mark (channel unknown).
- Recipient / sender / line values for the rules: invoice contact first,
  commercial customer per field; the invoice's payment account (else the
  company's first) for IBAN and BIC.
- `res.partner.send_readiness` / `send_ready` for the contact's default
  method in the current company, without invoice-level items.
- `goodahead.apix.connection._apix_for_company()` /
  `_apix_sending_enabled()`; switching on with `status` in the same write
  reads that status.
- Tests: 23 new.

---

19.0.1.6.0 — the sending switch, the background threshold and the sending
pause on the APIX connection.

- `goodahead.apix.connection`: `sending_enabled` (tracked, not copied;
  readable by Accountants), `background_threshold` (default 20, at least 1 —
  `send.threshold_invalid`; Administrators only), `send_paused_until`,
  `credit_balance` / `credit_balance_date` (Administrators only).
- `action_apix_sending_on()` / `action_apix_sending_off()`: Administrators
  only (`send.not_allowed`); switching on needs a working connection
  (`send.connection_not_working`) and no earlier setup still sending
  (`send.earlier_setup_sending`, naming it and pointing to the migration
  tool). The same guards run on any write of `sending_enabled`.
  `_apix_earlier_senders(company)` is empty here (the migration bridge
  answers).
- The sending pause: `_apix_send_circuit_open()`,
  `_apix_open_send_circuit(code, message)` (5 minutes; credentials refused
  and no contract also reject the connection), `_apix_close_send_circuit()`,
  `_apix_record_credit_balance(saldo)` — ledger writes on the connection
  row. A passing connection check closes the pause. An open pause is
  connection trouble in the attention bubble.
- Connection form: "Sending" section and switch buttons for Administrators;
  Accountants see "Sending" on / off only; a red banner while paused.
- Tests: 21 new.

---

19.0.1.5.0 — the install fill (promoted staged migrations 02 and 03).

- `hooks.py` `post_init_hook` (and `migrations/19.0.1.5.0` for databases on
  19.0.1.4.0): `mark_sent_before_install` sets `sent_before_install` on
  customer documents the ERP had marked sent (`is_move_sent`, read only
  here); `backfill_transmit_method` gives customer documents without a
  method the creation default (company-dependent channel read from the
  jsonb column for the move's company; commercial-customer fallback;
  e-invoice address; email). Set-based SQL; a method already set is kept;
  vendor bills untouched; nothing else is produced.
- Tests: 8 new.

---

19.0.1.4.0 — the transmit method and the customer's APIX channels.

- `account.move.transmit_method` (e-invoice / printed letter / email; not
  copied, tracked) on customer invoices and credit notes only, set once at
  creation from the customer (`_apix_default_transmit_method()`): the
  invoice contact's channel, else its commercial customer's (APIX channels
  `apix` → e-invoice, `apix_letter` → letter, `email` → email; any other
  value counts as not set), else e-invoice with an e-invoice address and
  email without. A change is refused with `send.in_progress`, naming who
  started the send and when, while a send is queued or running.
- `account.move.sent_before_install` (filled at install by a later step).
- `res.partner.invoice_sending_method` gets `apix` and `apix_letter`;
  `edicode` and `einvoice_operator_id` are tracked.
- Creating and posting invoices produces nothing else (no attempt, job or
  attachment).
- Tests: 21 new.

---

19.0.1.3.0 — the delivery ledger.

- `models/ledger.py`: `ledger_env(env)` — one ledger step on a cursor of its
  own (READ COMMITTED, `lock_timeout` 10 s), committed when it ends; under
  the test runner a savepoint of the test cursor (`FORCE_OWN_CURSOR` for
  tests that race real connections). `Holder` (code `send.in_progress`).
- `goodahead.invoice.send.attempt`: `_ledger_claim` (fails sends running
  over 10 minutes with `send.ran_too_long`, then inserts `running`; the
  partial unique index decides, the loser gets the holder), `_ledger_start`,
  `_ledger_finish` (outcome, `counts_as_sent`, `is_latest`,
  `needs_attention`; a late verdict is a new `late_answer` attempt),
  `_ledger_record` (one-step attempts; carried-over sends once per source),
  `_ledger_cancel` (`queued → cancelled`, then cancels the job),
  `_ledger_run_status`. Writes attempt rows only, never `account.move`.
- Tests: 27 new, four of them on real connections (claim race, start vs
  cancel race, a caller holding the invoice row, a caller's rollback).

---

19.0.1.2.0 — the delivery history model.

- `goodahead.invoice.send.attempt`: the immutable attempt per invoice and
  channel (fields of data-model.md "Aggregate: delivery history"), with the
  partial unique indexes `one_in_progress` and `migration_once` and the
  partial indexes for running, queued, attention, sent and run lookups.
- Read-only access for billing users, a record rule by allowed company, the
  developer fields behind developer mode; `create`/`write`/`unlink` refused
  with `send.history_readonly` unless written through `_ledger()`.
- Tests: 43 → 62 (indexes, the guards, developer fields, two companies).

---

19.0.1.1.0 — the package and the masked exchange record.

- `lib/package.py`: `build_package(message_bytes, pdf_bytes, invoice_number)`
  returns the per-invoice zip (`<safe invoice number>.xml` + `invoice.pdf`),
  byte-identical when regenerated from the same data.
- `lib/masking.py`: `mask_exchange(method, url, status, body)` masks
  `TraID`, `uid` and `d`, replaces a binary body by its size and SHA-256 and
  keeps the APIX answer. Pure Python; nothing calls either yet.
- Tests: 32 → 43.

---

19.0.1.0.0 — the add-on frame and the readiness rules.

- New add-on, installs empty (no models, no screens yet).
- `lib/readiness.py`: `evaluate(channel, recipient, sender, *, sending_enabled,
  lines, due_date, invoice_level)` returns `{ready, items}` per the readiness
  item table for e-invoice, printed letter and email. Pure Python, no APIX call.
- Tests: 32.
