# Goodahead Invoice Sending through APIX (`goodahead_invoice_send_apix`)

Sends customer invoices and credit notes through the company's APIX
connection, as Finvoice 3.0 e-invoices or as printed letters, next to the
ERP's own email sending. Every company sends with its own APIX connection and
sender details; every try is kept in the invoice's delivery history.

Nothing happens until someone sends: creating or posting invoices — one or
thousands in a data migration — stores only their transmit method. No
message, attachment, job, history entry or APIX call is made before a user
presses a send button.

## What it does

- **Transmit method** (`account.move.transmit_method`): how a customer
  invoice or credit note goes out — e-invoice, printed letter or email. Set
  when the invoice is created, from the invoice contact's sending channel
  (else its commercial customer's): *E-invoice via APIX*, *Printed letter via
  APIX* or *by Email*; with no channel set, e-invoice when the customer has an
  e-invoice address, else email. It can be changed by hand later, except while
  a send of the invoice is in progress.
- **Readiness**: the invoice form, the invoice list and the contact form say
  whether an invoice is ready for its channel and exactly what is missing and
  where it is fixed (see *Readiness rules*). Worked out from stored details
  only, never by asking APIX.
- **Send via APIX** (invoice header): sends the invoice now and shows the
  outcome — accepted by APIX (with the batch id and APIX's warnings), refused
  (APIX's reason) or failed (why, and whether it may already be at APIX). An
  invoice already sent (by APIX, by email or before this add-on) asks for
  confirmation first. Not ready → the **fix-it wizard** looks up or takes the
  customer's e-invoice address and operator.
- **Look up e-invoice address**: the same fix-it wizard without an invoice.
  On the contact form (*Sales & Purchase* tab, under the e-invoice address
  and operator) for billing users who may change the contact: looked up with
  the APIX connection of the user's current company, saved to that contact.
  On the company form, for accounting administrators: the company's own
  address and operator, looked up with that company's connection. Without a
  working connection the wizard says so and entry by hand still works;
  nothing is written until *Save*, nothing is sent, and the delivery-method
  check (about an invoice) is offered from an invoice only.
- **Business ID from the lookup**: when *Save* uses an address APIX found,
  the Business ID APIX gave with it also goes to the customer (the
  commercial partner; on the company form, the company) — only when it has
  none, never over an existing one. For a Finnish Business ID and a customer
  without a VAT number, the box *Also set VAT number FI…* (off by default)
  also saves FI + its digits. Entry by hand fills neither.
- **Send & Print**: *E-invoice (APIX)* and *Printed letter (APIX)* sit next to
  email and download, preselected by the transmit method, with the readiness,
  in-progress, already-sent and copy warnings as the dialog's alerts. With
  several invoices and APIX among the methods, *Send* opens the mass sending
  wizard for the APIX invoices; the others continue in the ERP's batch flow.
- **Mass sending wizard** (invoice list ▸ Action ▸ *Send e-invoices…*):
  classifies the selection per company and transmit method (ready, not ready
  with *Fix*, already sent — included only after a warning —, in progress,
  sending off, skipped, draft), then **Send now** (the wizard stays open and
  shows each invoice's status within about two seconds, with *Fix* and
  *Retry* on a refused or failed one) or **Send in background** (recommended
  above the company's background threshold; the wizard tells how many were
  queued and closes). Closing the wizard never stops queued sends.
- **Background sends**: one job per invoice on the `root.apix.send` channel,
  run in the invoice's company whoever queued it. A queued send can be
  cancelled from the Delivery tab. A send still running 10 minutes after it
  started and a queued send whose job ended without sending are failed every
  minute by the sweeper, so no invoice stays blocked.
- **Delivery tab** (invoice form): every try on every channel — e-invoice,
  printed letter, email (also emails sent with the ERP's own Send outside
  this add-on) and download — with when, who (or "background, queued by …"),
  outcome, whether it counts as sent, reasons and warnings, APIX batch id,
  document id and credits. Nobody changes or removes an entry.
- **Failed sends**: refused and failed sends — background ones too — count in
  the shared e-invoicing attention bubble (Accountants, their own companies
  only) and are listed in *Customers ▸ Failed sends*; an item opens the
  invoice on its Delivery tab. An invoice leaves the list when a later send
  succeeds or someone **marks it handled** with a reason, and comes back when
  a later send fails again. APIX connection trouble itself is shown to
  accounting administrators only.
- **Developer view** (developer mode): *Technical* on a delivery row or an
  attempt opens the exact Finvoice message and package sent (downloads), the
  APIX exchange with the transfer id and request digest masked, the schema
  result and problems, the units sent without a code and the job.
  *Regenerate and check* on the invoice builds and checks the message from
  the current data without sending or recording anything.

## Setup

1. Set up the company's APIX connection (*Accounting ▸ Configuration ▸
   E-invoicing ▸ APIX connection*; `goodahead_apix`, see its README) and
   check it: the status must be *Working*.
2. Fill in the company's sender details: name, Business ID, address, its own
   e-invoice address and operator (`l10n_fi_edicode`; *Look up e-invoice
   address* on the company form finds them at APIX), and a payment account
   with IBAN and BIC.
3. If the company sent through an **earlier APIX setup** (for example the
   community APIX modules), install `goodahead_invoice_send_apix_migration`
   and the earlier setup's adapter **before** switching sending on, and carry
   the company's sending over with the migration tool (*Accounting ▸
   Configuration ▸ E-invoicing ▸ APIX migration tool*). Without an adapter the
   earlier setup is invisible to this add-on and both could send.
4. On the APIX connection screen, section *Sending*, an accounting
   administrator switches **sending on** for the company. It is refused while
   the connection is not working or an earlier setup still sends for the
   company (the message names it). Also set there:
   - **Background threshold** (default 20): above it the mass sending wizard
     recommends the background.
   - The last APIX **credit balance** is shown after each send.
5. Customers: an e-invoice address and operator for e-invoices, a postal
   address for printed letters, an email address for email. *Look up
   e-invoice address* on the contact's *Sales & Purchase* tab finds the
   address and operator at APIX (or takes them by hand) and saves them as a
   pair. *E-invoice via APIX* / *Printed letter via APIX* as the contact's
   sending channel makes it the default transmit method of its new invoices.
6. Units: standard units carry their UN/ECE Rec 20 code from install; give
   your own units a code in *Accounting ▸ Configuration ▸ E-invoicing ▸ Unit
   codes* (a unit without a code never blocks a send; it goes by name).

**Job channel capacity.** Background sends run on `root.apix.send`, under
the APIX channel `root.apix` that receiving uses. Set the capacity in the
`queue_job` deploy settings, the sub-channel below the parent so receiving
always keeps a slot: `root.apix:3,root.apix.send:2` (or
`root.apix:4,root.apix.send:3` where the workers allow). At about 10 s per
invoice, 1,000 queued invoices finish within 3 hours at capacity 2.

## Readiness rules

`lib/readiness.py` — pure Python, no ERP import and no APIX call. From the
stored details of the invoice contact (falling back to the commercial
customer per field) and of the invoice's company:

| Item | E-invoice | Letter | Email |
|---|---|---|---|
| sending switched off for the company | blocks (only item shown) | blocks | — |
| customer's e-invoice address, operator (on the operator list) | blocks | — | — |
| customer's name | blocks | blocks | — |
| customer's postal address | warns | blocks | — |
| customer's Business ID or VAT code (APIX accepts the e-invoice but notes "HUOM: Ostajan Y-tunnus/ALV-tunniste puuttuu"; add it on the customer or find it with *Look up e-invoice address*) | warns | — | — |
| customer's email address | — | — | blocks |
| your company's name | blocks | blocks | — |
| your company's Business ID (unless it has its own e-invoice address) | blocks | — | — |
| IBAN and BIC of your payment account | blocks | — | — |
| the invoice's due date | blocks | — | — |
| units without a unit code (named) | warns | warns | — |
| a 0 % tax without a VAT category (named; an Administrator sets it) | warns | — | — |
| a line without a percentage tax, or with taxes of different VAT categories | warns | — | — |
| taxes of one VAT category with different VAT reasons in one breakdown entry | warns | — | — |
| reverse charge or intra-EU supply without the customer's VAT number (shown instead of the Business ID or VAT code warning when both apply) | warns | — | — |
| customer reference or source document over 70 characters (left out) | warns | warns | — |
| our numeric order number withheld (it would read as a payment reference) | warns | warns | — |
| a different delivery address that lacks a name, street, post code or town | warns | — | — |

A foreign customer with an e-invoice address and an operator but no Finnish
Business ID is ready. A download, a refused or a failed send never makes an
invoice "already sent". The content warnings never block: only what APIX
would refuse does.

## What the e-invoice states

Every message passes the Finvoice 3.0 schema check before it leaves; a text
too long for its place is cut or split, an identifier (order number,
reference, barcode) is never cut — it is left out with a readiness warning.

- **VAT category per rate and category.** The VAT breakdown has one entry
  per VAT rate and VAT category, and every row names its category. A tax's
  category is the one *set* on it (the ERP's own *Tax Category Code* of the
  tax, whoever set it), else standard rate above 0 %, else for a 0 % sales
  tax the one category its Finnish VAT report lines name: intra-EU sales of
  goods (`fi_311`) → intra-EU supply; EU services (`fi_312`) and
  construction services (`fi_319`) → reverse charge; every other line,
  zero-rated turnover (`fi_304`) included, names none, and a tax whose lines
  name two categories stays *unclassified*. The default is worked out when it
  is shown and when a message is built, never stored. An unclassified 0 %
  tax goes without a category and without a reason — never a guessed one.
  The tax list shows the category and *set / default / unclassified*, with
  the filter *Unclassified 0 % sales taxes*. Sales taxes refuse a no-VAT
  category on a taxed rate, standard rate on 0 %, an exemption reason code
  of another category and a VAT reason over 3 lines of 70 characters;
  purchase taxes are not checked. Only accounting administrators change a
  tax's category, code or reason; every change is kept in the tax's chatter.
- **VAT reason.** A no-VAT entry states its reason (one Finvoice text per
  line) and its exemption reason code — the one set on the tax, else the
  category's standard code (reverse charge `VATEX-EU-AE`, intra-EU supply
  `VATEX-EU-IC`, export `VATEX-EU-G`, outside scope `VATEX-EU-O`; none for
  exempt and zero rated). An empty reason sends the category's default
  wording (Finnish, then English), shown as the field's placeholder:

  | Category | Default VAT reason |
  |---|---|
  | Reverse charge | Käännetty verovelvollisuus / Reverse charge |
  | Intra-EU supply | Yhteisömyynti, AVL 72 a § / Intra-Community supply, VAT Directive art. 138 |
  | Export | Vienti EU:n ulkopuolelle, AVL 70 § / Export outside the EU |
  | Exempt | Arvonlisäverosta vapautettu myynti / VAT exempt supply |
  | Zero rated | Nollaverokannan myynti / Zero-rated supply |
  | Outside scope | Ei arvonlisäveron alaista myyntiä / Outside the scope of VAT |

  **Have the company's accountant confirm these wordings** before the first
  send, and set a tax's own reason where it differs.
- **Order numbers.** The customer's order number is the invoice's customer
  reference less every comma-separated part that is one of our order
  numbers (the ERP echoes our sale order number there when the customer gave
  none, and lists several on a grouped invoice): `PO-1, S00012` with source
  document `S00012` sends `PO-1`; `S00012` alone sends none. A credit note
  made by reversing an invoice sends that invoice's. Our order number is the
  source document as written; when it consists of digits that would pass
  the Finnish payment reference check and the invoice has no valid payment
  reference, it is left out, so no receiver reads it as one.
- **Buyer reference.** An invoice's *Buyer Reference* (at most 70
  characters, editable while draft) starts with the customer's *Standing
  Buyer Reference* — the invoice contact's, else the commercial customer's —
  on every way an invoice is created; a change of customer replaces it only
  while it is empty or still the previous customer's value; a reversal
  copies the reversed invoice's. Nothing is filled in on existing invoices.
  The standing value is customer data: users who may edit contacts set it;
  billing users without that right set the buyer reference on the invoice.
- **Delivery party** when the invoice's delivery address is another contact
  at a different postal address (compared without regard to case and
  spacing) with a name, street, post code and town.
- **Text rows, discounts, trade item numbers.** Notes and section headings
  go as text rows in their place; a discounted line states its price before
  discount (VAT excluded), its percentage and a discount amount that makes
  the row add up to the line's amount to the cent (a surcharge goes at its
  net price); a product barcode that is a valid GTIN (8, 12, 13 or 14 digits,
  check digit, not a store-internal number) goes as the trade item number.
- **Standard codes.** International invoice type (380 / 381), payment means
  (SEPA credit transfer in euros, else credit transfer) and the rows total.
  No conformance to the European norm is claimed; e-invoice addresses go as
  before.

## Troubleshooting

| You see | Meaning and what to do |
|---|---|
| "Not ready to send as e-invoice: …" | Details are missing; the line says which and where. *Fix e-invoice address* opens the fix-it wizard. |
| "A send of this invoice is already in progress, started by … on …" | One send at a time per invoice. Wait, or cancel the queued send on the Delivery tab. |
| Refused: *APIX's reason* | APIX did not take the invoice. Fix the data and send again (the invoice is not counted as sent). |
| Failed: credentials refused / no sending contract / no credit left / APIX unreachable | The company's sending pauses for 5 minutes and the connection screen says why; a passing connection check ends the pause. |
| Failed: APIX did not answer | The invoice may already be at APIX. Check APIX before sending again. |
| Failed: the send was still running 10 minutes after it started | Released by the sweeper; it may already be at APIX. |
| Failed: the background job ended without sending | Nothing was sent; send again. |
| The mass wizard says "Waiting for the background worker" | No queued send has started for 30 s: the job runner is not running or the channel has no capacity. |
| The schema check failed | The reason names the invoice field; the developer view lists the schema problems. Nothing was sent. |

## Database copies

Neutralizing a copy the ERP's standard way switches sending off for every
company, cancels queued sends and clears the APIX credentials (which also
stops receiving on the copy). The copy sends again only after an
Administrator enters credentials and switches sending on — after every
rebuild of the copy. While a copy holds production credentials (a mirror),
every sending screen warns that sending reaches real customers.

## Install

Depends on `account`, `queue_job`, `l10n_fi_edicode` (e-invoice address and
operator on contacts and companies), `goodahead_apix` (the connection) and
`goodahead_einvoice_base` (the Finvoice library, unit codes and the attention
bubble). It never depends on `connector_apix`, `account_edi_finvoice`,
`account_invoice_transmit_method` or `uom_unece`. Where
`goodahead_finnish_invoice` is installed, the PDF sent with the e-invoice is
the Finnish invoice layout.

Installation (and the upgrade to 19.0.1.5.0) does two things, in plain SQL,
and nothing else — no history entry, job, attachment or APIX call:

- customer invoices and credit notes the ERP had already marked sent are
  marked **sent before sending was installed** (channel unknown), so sending
  one again asks for confirmation first. The ERP's own sent mark is read only
  this once; afterwards only the delivery history says what was sent;
- customer invoices and credit notes without a transmit method get one by
  the creation rule above. Vendor bills are not touched.

## Tests

The library tests (readiness, package, masking, lib purity) run without the
ERP:

    python -I -m unittest discover -s goodahead/goodahead_invoice_send_apix/tests -t goodahead

and in the ERP's test runner with `--test-tags /goodahead_invoice_send_apix`
(the mass wizard's widget tests run in Chrome). The 1,000-invoice background
load test is pre-release only: `--test-tags
pre_release/goodahead_invoice_send_apix` (set `GOODAHEAD_LOAD_TIME_FACTOR`
to scale the recorded 10 s APIX answer; default 0.001). All names and codes
in the tests are invented; no test reaches APIX.

## Licence

LGPL-3.0 or later. Author: Goodahead Oy.
