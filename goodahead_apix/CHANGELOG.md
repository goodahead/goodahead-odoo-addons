# `goodahead_apix` — changelog

Newest first.

**Release gate (18.0):** 2,043 tests, 0 failed.

---

18.0.1.7.3 — release clean-up: no functional change.

- Manifest description shortened to one current sentence.
  Upgrade: `-u goodahead_apix`.

---

18.0.1.7.2 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_apix`.

---

18.0.1.7.1 — the APIX connection menu under Configuration.

- Menu *APIX connection* moved from *Accounting ▸ Vendors* to
  *Accounting ▸ Configuration ▸ E-invoicing* (first, before *APIX
  migration tool* and *Unit codes*), accounting administrators only. Same
  menu id: an upgrade moves it in place and drops the billing group.
  Access rights are unchanged: billing users still read the connection
  status. README updated.
- Tests: 4 new (menu parent and order, administrator sees it, a billing
  user does not; 18.0 reads the menu's `groups_id`).

---

18.0.1.7.0 — first 18.0 release of 19.0.1.3.0 to 19.0.1.7.0 (listed below), for Odoo 18.0.

- No behaviour differs on 18.0. The channel's pre-migrate is
  `migrations/18.0.1.4.0`; the attention section's domain is combined
  with `odoo.osv.expression` (19.0: `Domain`).
- Upgrade: `-u goodahead_apix,goodahead_invoice_import_apix` (installs
  `goodahead_einvoice_base`).

19.0.1.7.0 — release notes for sending.

- README: APIX sending (`goodahead_invoice_send_apix`) in the add-on table
  and the job channel note; neutralization also switches sending off and
  cancels queued sends where the sending add-on is installed. No code change.

---

19.0.1.6.0 — sending interface fields and connection trouble.

- `interface_name` / `interface_version` on the connection (required,
  defaults `Standard` / `1.0`, accounting administrators only; form group
  "Sending interface"): the `soft` / `ver` parameters of the sending calls.
- `connection_trouble` section of the shared attention bubble (accounting
  administrators): connections of the user's allowed companies matching
  `_attention_connection_domain()` (here `status = rejected`; the sending
  add-on adds an open sending pause). `unchecked` is not trouble.
- Depends on `goodahead_einvoice_base` (the bubble).
- Tests: 10 new (defaults, rights, form, section per role and company).

---

19.0.1.5.0 — copy safety.

- `data/neutralize.sql`: a neutralized database copy loses the transfer id
  and key; status *Not checked* with "Credentials cleared by database
  neutralization". Receiving's own neutralization (receiving off) still runs.
- `goodahead.apix.connection._apix_copy_warning()` (super-chained): the
  warning text on a neutralized copy whose production credentials were
  entered again, else False. Never blocks; sending surfaces show it.
- Tests: 11 (neutralization, the warning's cases, receiving's mirror answer).

---

19.0.1.4.0 — the `root.apix` job channel belongs to the connection.

- The `root.apix` job channel (record and external id
  `goodahead_apix.channel_apix`) moves here from
  `goodahead_invoice_import_apix`, so receiving and sending share it without
  sending depending on receiving. New dependency: `queue_job`.
- Upgrade: a pre-migrate script moves the external id; the channel record, its
  id and every job on it stay as they are. Nothing to move on a fresh install,
  without receiving, or when it already moved. Upgrade together with
  `goodahead_invoice_import_apix` 19.0.1.8.0 (`-u goodahead_apix,goodahead_invoice_import_apix`).
- Tests: the channel's owner, receiving's job functions on it, and the
  script's rules.

---

19.0.1.3.0 — the APIX client can send.

- `send_invoice_zip` (SendInvoiceZIP, `PUT /invoices`): sends one package and
  returns a `SendAnswer` (batch id, credits, counters, one entry per accepted
  or rejected document with its English, Finnish and Swedish texts, and the
  raw answer). An ERR answer is returned with its status code, not raised, so
  the sending service maps it.
- New error `apix.no_answer` (`ApixNoAnswer`), raised only by
  `send_invoice_zip`: the package may have reached APIX but no valid answer
  came (read timeout, connection broken after the upload, an answer that is
  not a Response). `apix.unreachable` there means the connection could not be
  opened at all.
- `address_query` (AddressQuery) lists every e-invoice address and operator
  APIX knows for a Business ID or name (`[]` when none); `delivery_method`
  (DeliveryMethod) returns the channel and price per document. Their errors
  raise the existing classes with APIX's text.
- No key, digest, transfer id, URL or package content is logged or kept in an
  error text. Receiving's calls are unchanged.
- Upgrade: `-u goodahead_apix`. No data change.

18.0.1.2.0 — the connection knows no earlier connector (as 19.0.1.2.0).

- The field *Migrated from community* (`migrated_from_community`) is removed.
  Where a connection came from is now the migration add-on's business:
  `goodahead_invoice_import_apix_migration` adds *Migrated from*
  (`migrated_from`, the code of the migration source) to the connection.
  Nothing was deployed with the old field, so there is no data migration.
- Upgrade: `-u goodahead_apix` together with `goodahead_invoice_import_apix`
  18.0.1.6.0.

18.0.1.1.0 — first release for Odoo 18.0, with the features of 19.0.1.1.0 (listed
below). The two unique rules (one connection per company, credentials used by one
company) are `_sql_constraints` on 18.0, under the same constraint names.

19.0.1.1.0 — credentials never exported; APIX errors classified.

- **Transfer ID and key are never exported**, not even by an administrator:
  they are left out of the export dialog and an export that names them is
  refused.
- **APIX errors by StatusCode** (APIX *Response message structure*): only
  error 04 *Permission denied* is a credential rejection (`apix.rejected`);
  error 05 *No data available* on the listing is "no packages"; every other
  error is `apix.service_error` (`ApixServiceError`), a failed call that
  leaves the credentials trusted. The connection check stays *Not checked* on
  a service error instead of *Rejected*.
- **Connection check** also compares the Business ID APIX reports with the
  company's own Business ID (*Company Registry*) when it is set; a mismatch
  is *Rejected* and fetching stays off.
- **Fetch from APIX login:** any unexpected error is reported on the dialog,
  so the clearing of the password hash is always committed.
- **Client hardening:** APIX answers are parsed with no DTD, no entity
  expansion and no network access; the `urllib3.connectionpool` logger is
  kept at INFO so a debug log level never writes request URLs (digests,
  keys) to the log.
- Upgrade: `-u goodahead_apix`.

19.0.1.0.0 — first version: the per-company APIX connection.

- **Connection record** per company (`goodahead.apix.connection`):
  environment, transfer ID and key, Business ID, status (*Not checked*,
  *Working*, *Rejected*) with its message and date. One connection per
  company; the same transfer credentials cannot serve two companies (the
  refusal names the company that uses them).
- **Connection check** asks APIX for the company behind the credentials and
  fails when its Business ID differs from the connection's. Changing the
  credentials, environment or Business ID resets the status to *Not checked*.
- **Fetch from APIX login** dialog: the web password is replaced by its
  SHA-256 hash before anything is written, the hash is cleared as the first
  step of the fetch, and only the returned transfer credentials are stored.
- **Access:** transfer credentials are readable and editable by accounting
  administrators only and are not copied on duplicate; billing users see the
  status through *Accounting ▸ Vendors ▸ APIX connection*. Settings block
  *APIX e-invoicing* in the Accounting settings.
- **Pure-Python client** (`lib/apix_client.py`), written from the APIX
  documentation only: RetrieveTransferID, company information, ListInvoiceZIPs,
  Download with explicit `markreceived`, GetMetadata. SHA-256 digests checked
  against the documented examples; typed errors (rejected, empty answer,
  unreachable, unexpected); 60-second timeouts; no secret in logs or error
  texts.
- Install: `-i goodahead_apix` (installed automatically by
  `goodahead_invoice_import_apix`). Tests: 48 (32 offline client tests, 16
  connection tests).
