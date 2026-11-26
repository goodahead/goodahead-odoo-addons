# `goodahead_invoice_import_apix_migration_community` — changelog

Newest first. Test counts are the gate at that version.

---

19.0.1.0.4 — release clean-up: no functional change.

- README: Install and Usage sections, link to OCA/l10n-finland for l10n_fi_edicode.
  Upgrade: `-u goodahead_invoice_import_apix_migration_community`.

---

19.0.1.0.3 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_import_apix_migration_community`.

---

19.0.1.0.2 — README: where the migration tool menu is.

- README names *Accounting ▸ Configuration ▸ E-invoicing ▸ APIX migration
  tool*. No code change.

---

19.0.1.0.1 — licence.

- Relicensed from AGPL-3 to LGPL-3; the add-on still depends on the
  community APIX modules, which keep their own AGPL-3 licence. No code change.

---

19.0.1.0.0 — the community bridge becomes the `community` migration source.

- **Renamed** from `goodahead_invoice_import_apix_community` (last version
  19.0.1.0.1, never deployed) and versioned afresh under the new name.
  Nothing was deployed, so there is no data migration; a database that had
  the old add-on installs this one instead.
- Implements the migration-source interface of
  `goodahead_invoice_import_apix_migration` (see its README):
  registers `community` in `_migration_sources`, and
  `_migration_community_detect` (the four credential fields, never the web
  password), `_migration_community_status` (the community methods and fields
  it relies on), `_migration_community_pause` (cancels the company's queued
  community listings and downloads) and `_migration_community_resume` (queues
  one community fetch). No `find_bill`: the community modules keep no
  package-to-bill record.
- Unchanged: the `apix.backend` overrides that skip a live company in the
  community listing, download and recurring fetch, refuse its re-fetch, and
  the banner on the backend form.
- Depends on `goodahead_invoice_import_apix_migration` and `connector_apix`;
  `auto_install` on `goodahead_invoice_import_apix` + `connector_apix`, so it
  brings the migration tool along. The menu moved to the migration tool.
- Install: automatic. Tests: 9 with the community modules installed.

## Before the rename (`goodahead_invoice_import_apix_community`)

19.0.1.0.1 — followed the guarded receiving state of `goodahead_invoice_import_apix` 19.0.1.5.0.

19.0.1.0.0 — first version: pause the community APIX modules per company
(listing, download and re-fetch skipped for a live company; pause cancels its
queued community downloads; resume queues one fetch; a status check refuses
the switch when the connector changed).
