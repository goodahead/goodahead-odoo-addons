# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

{
    "name": "AI Invoice Import",
    "summary": "Import vendor bills from PDF using Claude AI and Peppol BIS 3.0",
    # The changelog lives in `CHANGELOG.md`, not here: Odoo reads a manifest with
    # `ast.literal_eval`, capped at 102,400 characters, and a manifest over the
    # cap is skipped silently (logged at DEBUG only). Keep this file short.
    # A short description stops Odoo from rendering README.md as RST.
    "description": "Reads vendor invoice PDFs with Claude AI and hands the "
    "result to the invoice import core (goodahead_invoice_import). "
    "See README.md.",
    "version": "18.0.4.2.4",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    "depends": [
        "account",
        "account_edi_ubl_cii",
        "mail",
        "queue_job",
        # the core/source split: the import record, vendor matching, suggestions, bank checks
        # and bill decoding moved to the shared core. Upgrading this add-on
        # installs it first, and its pre-install hook renames
        # `ai.invoice.import` to `goodahead.invoice.import` in place.
        "goodahead_invoice_import",
        # One Claude client in the tree, and it is this one.
        # It brings the API key, the model registry and the HTTP transport.
        "goodahead_claude",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/queue_job_channel.xml",
        "data/prompt_profile.xml",
        # 🔴 NO `data/mail_alias.xml`. The inbound alias is created BY HAND
        # by each installation, with the address it chooses.
        # Shipping one would mean the module creates `vendor-invoices@` and the
        # installation then creates its own beside it — two live inbound addresses,
        # one of which nobody expects, each billing a Claude call per PDF.
        # `goodahead.invoice.import` still inherits `mail.thread` and still implements
        # `message_new`, so ANY alias pointed at this model works; the module
        # simply does not decide which one exists. See `models/ai_invoice_import.py`
        # for the guards that apply to whatever alias is created.
        "views/prompt_profile.xml",
        # Inherits the core's import-record views (model and token columns).
        "views/ai_invoice_import.xml",
        # Inherits the core's settings block (extraction model and prompt).
        "views/res_config_settings.xml",
    ],
    # No assets and no `post_init_hook` since 19.0.4.0.0: the failed-imports
    # systray and the install-time seeding of `goodahead.partner.identifier`
    # (plus the substitute-PDF policy) moved to `goodahead_invoice_import`,
    # which is installed before this add-on and runs them.
    "installable": True,
    "auto_install": False,
    "application": False,
}
