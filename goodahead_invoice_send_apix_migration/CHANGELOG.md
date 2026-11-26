# `goodahead_invoice_send_apix_migration` — changelog

Newest first.

---

19.0.1.3.3 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_send_apix_migration`.

---

19.0.1.3.2 — README: where the migration tool menu is.

- README names *Accounting ▸ Configuration ▸ E-invoicing ▸ APIX migration
  tool*. No code change.

---

19.0.1.3.1 — README: the ways out when a connector without a sending
adapter blocks switching sending on (remove its credentials, uninstall it,
or install its sending adapter).

---

19.0.1.3.0 — review fixes.

- Earlier-senders guard: a connector the receiving migration tool detects
  for the company (transfer credentials found) that has no sending adapter
  counts as still sending; switching sending on is refused and the message
  names it. README updated.

---

19.0.1.2.0 — release notes.

- README: the install order (earlier setup, sending add-on with this bridge
  and its adapter, connection carry-over, sending carry-over, switch on) and
  switching back; the "under construction" note removed. No code change.

---

19.0.1.1.0 — the sending step of the migration tool.

- `goodahead.apix.migration` gets the step `sending` (button "Sending"):
  `sending_source_code` (sending sources; the receiving source first),
  `sending_view` (`needs_connection`, `no_adapter`, `nothing`, `error`,
  `found`) with its banner, the source's status and the four counts.
- Buttons: `action_sending_carry_over` (result `done` / `partial` with the
  customers not carried), `action_sending_switch_back` (`switched_back` /
  `refused` while sending is on here), `action_open_sending_settings`.
- Tests: 11 new (every state of the sending step through the form).

---

19.0.1.0.0 — first version: the sending carry-over bridge.

- Sending sources in the receiving migration-source registry: a source that
  implements any `_migration_<code>_sending_*` method; one missing a required
  method counts as sending.
- `goodahead.apix.connection._apix_earlier_senders(company)` answers every
  sending source whose status is `sending`, so the sending switch is
  refused with `send.earlier_setup_sending`.
- `_migration_sending_read / _counts / _carry_over / _switch_back`: carry-over
  writes recipients (operator matched by identifier, unmatched reported),
  customer channels, transmit methods and one `sent` attempt per invoice
  (`origin = migration`, repeatable) for the company only, then calls
  `sending_stop`; an unknown key refuses everything. Switch back is refused
  with `migration.sending_on` while this add-on's sending is on.
- `auto_install` next to the sending add-on and the migration tool.
- Tests: 13.
