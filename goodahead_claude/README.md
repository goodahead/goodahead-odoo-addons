# Goodahead Claude

A shared client for the Claude API (Anthropic), used by other Goodahead add-ons
such as AI invoice import. It holds the per-company connection settings, keeps a
catalogue of the models the API offers and gives other add-ons one method to
call the API.

## Install

Depends on `base`, `mail` and `queue_job` (OCA `queue`), and the Python package
`requests`. Install it like any other add-on:

```
odoo-bin -d <database> -i goodahead_claude
```

Add-ons that use Claude depend on it, so it is usually installed with them.

## Configuration

Settings → General Settings → **Claude Connection** (administrators only):

- **API Key** — the company's Anthropic API key (Anthropic console → Settings →
  API Keys). **Test Connection** checks it.
- **Default Model** — the model used when a calling add-on does not choose one.
  Click **Sync Models** first to load the catalogue from the API.
- **Max Output Tokens** — the default cap on the length of an answer (4096).
  A calling add-on may set its own.

The settings are per company. Neutralizing a database copy (`odoo-bin
neutralize`) clears every company's API key, so a test or staging copy never
calls Claude on the production account.

## Usage

- Settings → Technical → Claude → **Models** lists the synced models, with their
  release date, input limit and whether they read PDF files.
- **Sync Models** adds new models, archives the ones the API no longer lists and
  restores a model that is listed again. Models are never deleted. If an archived
  model was the company default, the sync says so; pick a new default.
- When no model is chosen, the client picks the newest listed model of the larger
  tiers. A caller that sends a PDF only gets a model that reads PDF files.

For developers: other add-ons call the abstract model `goodahead.claude.client`:

```python
client = self.env["goodahead.claude.client"]
response = client.messages_create(
    messages=[{"role": "user", "content": "Hello"}],
    company=self.env.company,
)
text = client.extract_text(response)
```

`messages_create` also takes `model`, `max_tokens`, `system`, `timeout` and
`requires_pdf`, and supports prompt caching.

The client's methods are for server code only: they cannot be called over
RPC (`/web/dataset/call_kw`), whoever is logged in. **Test Connection** and
**Sync Models** need an administrator.

## Limits

Every request is billed to the company's key, so the server caps what one
request may ask for, whatever the caller passes. Both are system parameters
(Settings → Technical → System Parameters):

| Parameter | Default | Meaning |
|---|---|---|
| `goodahead_claude.max_output_tokens_ceiling` | 64000 | Most output tokens one request may ask for |
| `goodahead_claude.timeout_ceiling` | 300 | Longest wait for one request, in seconds |

The defaults are above what the shipped add-ons ask for (AI invoice import:
16,000 tokens and 300 seconds).

With worker processes (`workers` > 0) Odoo stops a request that runs longer
than `limit_time_real` (default 120 s). A request that is still waiting then
has been billed and its answer is lost, so the client keeps the timeout 15
seconds below `limit_time_real` (never below 30 s) and logs a warning when it
shortens one. For AI invoice import raise `limit_time_real` above 315 s on the
workers that run its jobs.

## Tests

```
odoo-bin -d <database> -i goodahead_claude --test-tags /goodahead_claude --stop-after-init
```

## Licence

LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl). Copyright 2026 Goodahead Oy.
