# `goodahead_claude` — changelog

Newest first.

---

18.0.1.4.1 — release clean-up: no functional change.

- README added; the changelog rewritten in the release format; the manifest
  description shortened to point at the README.
- Upgrade: `-u goodahead_claude`.

---

18.0.1.4.0 — first release for Odoo 18.0.

- The same features as 19.0.1.4.0, listed below.

---

19.0.1.4.0 — a caller that sends a PDF gets a model that can read it.

- `messages_create` takes `requires_pdf` (default off). When it is on and no
  model is named, only models that report PDF support are picked. A company
  default that cannot read PDF files is refused while a PDF-capable model exists.
- If no listed model reports PDF support at all, the client logs a warning and
  falls back to the normal choice instead of failing.
- Callers that name a model, or do not pass `requires_pdf`, behave as before.

---

19.0.1.3.0 — Sync Models retires models as well as adding them.

- A model the API no longer lists is archived (never deleted); a model listed
  again is restored. The sync names any company default it has made invalid.
- An archived company default raises a clear error instead of sending an id the
  API no longer serves. A model named by the caller is used as given.
- The fallback model is the newest of the larger tiers, not simply the most
  recent release.

---

19.0.1.2.0 — per-call timeout.

- `messages_create` takes an optional `timeout` in seconds. Without it the
  transport's 120 s applies, so existing callers do not change.

---

19.0.1.1.0 — default model from the synced catalogue.

- The default model is chosen from the synced models instead of a fixed id
  (the old fixed id had been retired by the API).
- Models record their release date and are listed newest first.
