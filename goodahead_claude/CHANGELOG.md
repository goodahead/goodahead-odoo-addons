# Changelog

## 18.0.1.4.0

- First release for Odoo 18.0, with the same features as 19.0.1.4.0 (listed below).

## 19.0.1.4.0

- `messages_create`, `_resolve_model` and `_fallback_model` take an optional `requires_pdf`
  argument, so callers that send documents are resolved to a model with PDF input support.
- If no synced model reports PDF support, resolution logs a warning and falls back instead of
  failing; a company default without PDF support raises when a PDF-capable model exists.

## 19.0.1.3.0

- "Sync Models" archives models the API no longer lists (never deletes them) and restores
  models that reappear; it reports configuration that points at an archived model.
- `_resolve_model` raises on an archived company default; an explicitly passed model is
  still returned unchanged.
- `_newest_model` is replaced by `_fallback_model`, which avoids picking the small model
  tier as the default.

## 19.0.1.2.0

- `messages_create` accepts an optional per-call `timeout`; `None` keeps the transport
  default of 120 seconds.

## 19.0.1.1.0

- The default model is resolved from the synced `goodahead.claude.model` records instead
  of a hardcoded model id.
- Adds `goodahead.claude.model.created_at` (the API release timestamp) and orders models
  newest first.
