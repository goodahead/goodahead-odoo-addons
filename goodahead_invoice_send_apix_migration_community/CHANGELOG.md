# `goodahead_invoice_send_apix_migration_community` — changelog

Newest first. Test counts are the gate at that version.

---

19.0.1.1.4 — release clean-up: no functional change.

- README: link to OCA/l10n-finland for l10n_fi_edicode.
  Upgrade: `-u goodahead_invoice_send_apix_migration_community`.

---

19.0.1.1.3 — release clean-up: invented test data, no functional change.

- Comments, docstrings and tests no longer carry data from real documents
  or databases; test documents are invented, same shapes.
- README: each community module's own licence, as its manifest states.
  Upgrade: `-u goodahead_invoice_send_apix_migration_community`.

---

19.0.1.1.2 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_send_apix_migration_community`.

---

19.0.1.1.1 — licence.

- Relicensed from AGPL-3 to LGPL-3; the add-on still depends on the
  community APIX modules, which keep their own licences (`connector_apix` and
  `account_invoice_transmit_method` AGPL-3, `account_edi_finvoice` LGPL-3).
  No code change.

---

19.0.1.1.0 — release notes.

- README: install order and switching back. No code change.

---

19.0.1.0.0 — first version: the community modules as the sending source
`community`.

- `_migration_community_sending_status / _read / _stop / _resume` on
  `goodahead.apix.connection`; `res.company.goodahead_community_sending_stopped`
  is the adapter's only write.
- Stop: `account.move.action_einvoice_send` refuses and `einvoice_send` skips
  a stopped company; its queued community send jobs are cancelled; the
  `finvoice_3_0` EDI format does not apply to its invoices.
- `auto_install` next to `goodahead_invoice_send_apix` and `connector_apix`.
- Tests: 10.
