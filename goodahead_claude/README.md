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

The settings are per company.

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

## Tests

```
odoo-bin -d <database> -i goodahead_claude --test-tags /goodahead_claude --stop-after-init
```

## Licence

LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl). Copyright 2026 Goodahead Oy.
