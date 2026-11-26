# Invoice Import — APIX migration from the community modules (`goodahead_invoice_import_apix_migration_community`)

The community APIX modules (`connector_apix`) as a **migration source** of the
APIX migration tool (`goodahead_invoice_import_apix_migration`), source code
`community`. It lets the tool read the community connection's credentials and
**pause the community modules for one company at a time**, so a company that
receives through the APIX receiving add-on (`goodahead_invoice_import_apix`)
is never also received by the community modules.

You do not install it by hand: it installs **automatically** wherever both
`goodahead_invoice_import_apix` and `connector_apix` are installed, brings the
migration tool with it, and goes away with the community modules. From then
on the receiving radio is read-only and companies go live and back through
the migration tool.

What the source does for the tool:

| Tool step | What happens to the community modules |
|---|---|
| Detect / carry over | Reads environment, transfer ID, transfer key and Business ID of the company's community backend — never its web password or user name. |
| Status | Checks the community connector still has the methods and fields this adapter relies on; if they changed, the switch is **refused** with a plain-language reason instead of failing silently. |
| Pause (switch to live) | Cancels the company's queued community listings and downloads. |
| Resume (switch back) | Queues one community fetch for the company; the community modules use the credentials they kept. |

For a company whose receiving state is **live**:

- the community modules do not list or download that company's packages (the
  recurring fetch, the fetch button, the listing and the download skip its
  backend);
- the community **re-fetch** action is refused for it, so packages already
  received cannot be imported a second time;
- the community backend form shows a banner and hides its fetch buttons.

Companies in **off** or **shadow** keep receiving through the community
modules exactly as before. The community modules keep no record of which
package became which bill, so this source has no `find_bill`: their bills
are recognised by attachment and by the duplicate rule.

## Which add-ons to install

| You need | Install | What comes with it |
|---|---|---|
| APIX e-invoices | `goodahead_invoice_import_apix` | `goodahead_invoice_import`, `goodahead_invoice_import_finvoice`, `goodahead_apix` |
| Moving a company over from the community APIX modules | nothing by hand | this adapter and `goodahead_invoice_import_apix_migration` install themselves where the community connector is installed |

## Install

Nothing to install by hand. Put this add-on on the add-ons path next to the
community APIX modules; installing `goodahead_invoice_import_apix` on a
database that has `connector_apix` installs it automatically, together with
`goodahead_invoice_import_apix_migration`.

## Configuration

None. Use the migration tool (see the `goodahead_invoice_import_apix_migration`
README): *Accounting ▸ Configuration ▸ E-invoicing ▸ APIX migration tool*.

## Usage

1. Open the migration tool and pick the company. The tool detects the
   community backend and carries its credentials over.
2. Switch the company to **live**: the community modules stop listing and
   downloading its packages, and the APIX receiving add-on takes over.
3. To go back, switch the company back in the tool: the community modules
   resume with one fetch for that company.

## Security

The adapter reads only the four credential fields of the community backend.
It never reads, returns or copies the community web password.

## Known limitations

- Written for the community connector version present at release. A changed
  connector is detected and refused at the switch; the adapter then needs an
  update.
- A community download that was already running when the company was paused
  cannot be cancelled. The duplicate rule and the recognition of existing
  bills still keep it from producing a second bill.
- Only receiving is paused. The community modules' sending is untouched.

## Licence

**LGPL-3.0 or later.** The add-on runs together with the community APIX
modules (AGPL-3, https://github.com/tawasta/connector-apix), whose licence
applies to them. The community modules in turn need `l10n_fi_edicode` from
OCA/l10n-finland (https://github.com/OCA/l10n-finland). It is kept as a separate, optional add-on on purpose: the
platform add-ons (`goodahead_invoice_import`, `goodahead_invoice_ai_import`,
`goodahead_invoice_import_finvoice`, `goodahead_apix`,
`goodahead_invoice_import_apix`, `goodahead_invoice_import_apix_migration`)
never depend on it or on the community modules.

Copyright Goodahead Oy — https://www.goodahead.com/
