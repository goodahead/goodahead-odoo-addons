# Goodahead APIX Connection (`goodahead_apix`)

One APIX connection per company, shared by the add-ons that talk to the APIX
e-invoicing operator: APIX receiving (`goodahead_invoice_import_apix`) and APIX
sending (`goodahead_invoice_send_apix`). This add-on imports and sends nothing by
itself.

- **Connection record** per company: environment (test or production),
  transfer ID and transfer key, the company's Business ID, and the result of the
  last connection check (*Not checked*, *Working*, *Rejected*).
- **Two ways to get the transfer credentials:** paste them, or fetch them once
  with the company's APIX web login.
- **Connection check:** asks APIX which company the credentials belong to. If
  APIX reports a different Business ID than the connection's, the check fails.
- **One APIX account, one company:** the same transfer credentials cannot be
  used by a second company; the refusal names the company that already uses
  them.
- **Sending interface name and version** (`soft` / `ver` of APIX's sending
  calls, default *Standard* / *1.0*): settings on the connection, visible to
  accounting administrators only, so a value APIX asks for needs no release.
- **Connection trouble in the attention bubble:** accounting administrators
  see the APIX connections of their allowed companies that are not working
  (credentials or contract refused) in the shared e-invoicing bubble; each
  item opens the connection. Accountants are not shown it.
- **APIX client** (`lib/apix_client.py`): pure Python, no Odoo import.
  Transfer credentials, company information, listing, download (with an
  explicit mark-received choice) and metadata, with SHA-256 request digests,
  typed errors and a 60-second timeout on every call. For sending: send an
  invoice package (SendInvoiceZIP), look up a receiver's e-invoice addresses
  (AddressQuery) and ask the delivery channel and price (DeliveryMethod). A
  send that could not connect (`apix.unreachable`) is told apart from one whose
  answer was lost and may have reached APIX (`apix.no_answer`). Written only
  from the public APIX documentation.

## Which add-ons to install

| You need | Install | What comes with it |
|---|---|---|
| PDF invoices read with AI | `goodahead_invoice_ai_import` | `goodahead_invoice_import`, `goodahead_claude` |
| Finvoice files by upload or email | `goodahead_invoice_import_finvoice` | `goodahead_invoice_import` |
| APIX e-invoices | `goodahead_invoice_import_apix` | `goodahead_invoice_import`, `goodahead_invoice_import_finvoice`, this add-on |
| Sending invoices and credit notes as e-invoices or printed letters | `goodahead_invoice_send_apix` | this add-on, `goodahead_einvoice_base`, `queue_job`, `l10n_fi_edicode` |
| Moving a company over from an earlier APIX connector (e.g. the community APIX modules) | `goodahead_invoice_import_apix_migration` | its adapter installs itself next to the connector and brings the migration tool (community: `goodahead_invoice_import_apix_migration_community`) |

## Configuration

Open *Accounting ▸ Configuration ▸ Settings*, block *APIX e-invoicing* ▸
*Configure APIX connection* (or *Accounting ▸ Configuration ▸ E-invoicing ▸ APIX connection*,
accounting administrators). One company at a
time; switch company to set up the next one.

1. Choose the **environment**: *Test* for the APIX test service, *Production*
   for real invoices.
2. Enter the company's **Business ID**.
3. Get the **transfer credentials**, either way:
   - **Paste** the transfer ID and transfer key from APIX, or
   - **Fetch from APIX login**: enter the company's APIX web login (email and
     password) and Business ID. The add-on asks APIX for the transfer
     credentials and stores only those.
4. Press **Check connection**. The status becomes *Working* and shows the
   company APIX reports, or *Rejected* with a plain-language reason.

Changing the credentials, the environment or the Business ID resets the status to *Not
checked*; check again. Receiving starts only when the APIX receiving add-on's
receiving state is set (see its README).

**Job channel.** APIX background jobs (receiving's and sending's) run
on the `root.apix` job channel, which this add-on owns. Give it its capacity in
the `queue_job` deploy settings, for example `root.apix:3,root.apix.send:2`
(keep sending's sub-channel below the parent so receiving always has a slot).

## Security

- **Transfer credentials are visible and editable for accounting
  administrators only.** Accountants see the connection status, not the
  credentials, and cannot read or export them. Credentials are never copied
  when a record is duplicated, and never written to the chatter.
- **The web password is never stored.** The fetch dialog replaces the typed
  password with its SHA-256 hash (which is all APIX's request needs) before
  anything is written, and clears that hash as the first step of the fetch,
  whether it succeeds or not.
- **Nothing secret is logged.** Request parameters are logged without
  digests, keys or passwords; error texts carry no secrets.
- **Database copies.** Neutralizing a copy the ERP's standard way
  (`data/neutralize.sql`) clears the transfer id and key and sets the status
  to *Not checked* ("Credentials cleared by database neutralization"); the
  Business ID and environment stay. With the sending add-on, sending is also
  switched off for every company and queued sends are cancelled. A copy reaches APIX again only after an
  Administrator enters credentials. Sending surfaces ask
  `_apix_copy_warning()`: on a neutralized copy holding production
  credentials (entered again), and in receiving's mirror mode, it answers
  "This is a mirror environment holding production credentials — sending
  reaches real customers." It warns, it never blocks.

## Known limitations

- The client is tested against invented, recorded APIX answers only; there is
  no network call to APIX in the automated tests.

## Licence

LGPL-3.0 or later. Copyright Goodahead Oy — https://www.goodahead.com/
