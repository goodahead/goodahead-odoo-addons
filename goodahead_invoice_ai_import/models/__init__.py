# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

# Before `res_company`, which points at it.
from . import prompt_profile
from . import res_company
from . import res_config_settings

# `claude_model` is gone: `goodahead.claude.model` is declared by
# `goodahead_claude`, which this module now depends on. Declaring it twice gave
# the two copies different `_order` and a different field set — and this
# module's copy had no `created_at`, the field the newest-model resolution
# depends on.
#
# The generic models (vendor matching, identifiers, candidates, suggestions,
# expense categories, the core profile, the import record itself) live in
# `goodahead_invoice_import` since the split into a core and sources; this module extends what it reads.
from . import account_move
from . import account_journal
from . import ai_invoice_import
