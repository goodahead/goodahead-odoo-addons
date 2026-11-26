# `goodahead_claude` — changelog

Newest first.

---

19.0.1.4.3 — security fixes.

### Security

- The client's methods (`messages_create`, `extract_text`, `extract_usage`,
  `test_connection`, `sync_models`) can no longer be called over RPC; they are
  for server code only. Test Connection and Sync Models need an administrator.
- The output-token limit and the timeout of a request are capped on the
  server (system parameters `goodahead_claude.max_output_tokens_ceiling`,
  default 64000, and `goodahead_claude.timeout_ceiling`, default 300 s).
- With worker processes, a request's timeout stays 15 s below Odoo's
  `limit_time_real`, so a slow request ends before its worker is stopped.
- Neutralizing a database copy clears every company's Claude API key.
- Upgrade: `-u goodahead_claude`.

---

19.0.1.4.2 — release clean-up: invented test data, no functional change.

- Comments, docstrings and tests no longer carry data from real documents
  or databases; test documents are invented, same shapes.
  Upgrade: `-u goodahead_claude`.

---

19.0.1.4.1 — release clean-up: no functional change.

- README and CHANGELOG added; the release notes moved out of the manifest.
  Licence header on every Python file; author is Goodahead Oy.
- Upgrade: `-u goodahead_claude`.

---

19.0.1.4.0 — a caller that sends a PDF gets a model that can read it.

- `messages_create` takes `requires_pdf` (default off). When it is on and no
  model is named, only models that report PDF support are picked. A company
  default that cannot read PDF files is refused while a PDF-capable model exists.
- If no listed model reports PDF support at all, the client logs a warning and
  falls back to the normal choice instead of failing.
- Callers that name a model, or do not pass `requires_pdf`, behave as before.
- Upgrade: `-u goodahead_claude`.

---

19.0.1.3.0 — Sync Models retires models as well as adding them.

- A model the API no longer lists is archived (never deleted); a model listed
  again is restored. The sync names any company default it has made invalid.
- An archived company default raises a clear error instead of sending an id the
  API no longer serves. A model named by the caller is used as given.
- The fallback model is the newest of the larger tiers, not simply the most
  recent release.
- Upgrade: `-u goodahead_claude`, then click **Sync Models**.

---

19.0.1.2.0 — per-call timeout.

- `messages_create` takes an optional `timeout` in seconds. Without it the
  transport's 120 s applies, so existing callers do not change.
- Upgrade: `-u goodahead_claude`.

---

19.0.1.1.0 — default model from the synced catalogue.

- The default model is chosen from the synced models instead of a fixed id
  (the old fixed id had been retired by the API).
- Models record their release date and are listed newest first.
- Upgrade: `-u goodahead_claude`, then click **Sync Models**.
