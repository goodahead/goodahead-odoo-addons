# `goodahead_invoice_import_apix_migration` — changelog

Newest first. Test counts are the gate at that version.

---

19.0.1.1.2 — release clean-up: no functional change.

- Licence notices, comments, tests and documentation tidied for the public
  release. Upgrade: `-u goodahead_invoice_import_apix_migration`.

---

19.0.1.1.1 — the migration tool menu under Configuration.

- Menu *APIX migration tool* moved from *Accounting ▸ Vendors* to
  *Accounting ▸ Configuration ▸ E-invoicing*, between *APIX connection*
  and *Unit codes*; accounting administrators only, as before. Same menu
  id: an upgrade moves it in place. README updated.
- Tests: 4 new (menu parent and order, administrator sees it, a billing
  user does not).

---

19.0.1.1.0 — release check.

- Release check: a ticket id / vendor name removed from comments and docs. No code change.

---

19.0.1.0.0 — first version: the migration tool in its own add-on, with migration sources.

- The migration tool moved out of `goodahead_invoice_import_apix` 19.0.1.5.1
  unchanged in behaviour: carry over (never a web password), shadow run and
  counts, switch to live (pause, clear the shadow results, live, one fresh
  run), switch back (unconfirmed marks and running package jobs refuse,
  queued package jobs cancelled, resume), one week of shadow recommended,
  administrators of the company only.
- **Migration sources:** the tool knows no connector. Adapters register a
  source code (`_migration_sources`, `_migration_source_label`) and implement
  `_migration_<code>_detect / _status / _pause / _resume` and, optionally,
  `_migration_<code>_find_bill`. See the README.
- Several sources with credentials for a company: the Administrator picks
  one. Every installed source is paused at the switch to live and resumed at
  the switch back; any source reporting a changed connector refuses the switch.
- `migrated_from` on the APIX connection (the source code, shown as
  *Migrated from*) replaces `goodahead_apix`'s `migrated_from_community`.
- Answers the receiving add-on's hooks: receiving goes live and back through
  the tool only while a source is installed or the connection was migrated;
  the shadow classification and the live recognition ask the source's
  `find_bill` first.
- Menu *Accounting ▸ Vendors ▸ APIX migration tool* for accounting
  administrators.
- Install: comes with an adapter (e.g. automatically where the community APIX
  modules are installed) or by hand. Tests: 22 (tool, source registry, US-06
  end to end), with test-only sources.
