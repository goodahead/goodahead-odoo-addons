# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Re-decide core's substitute-PDF switch on EVERY upgrade of this module.

🔴 WHY A `0.0.0` FOLDER AND NOT A VERSIONED ONE (found in review).
`migrations/19.0.3.1.0` and `migrations/19.0.3.10.0` each run once, on the single
crossing of their version. That was sound while the decision depended only on the
administrator. Since 19.0.3.10.0 it depends on **the core** — whether
`account.edi.ubl._import_attachments` is defined by core's own
`account_edi_ubl_cii.models.account_edi_ubl` — and an Odoo point-upgrade changes
that without touching this module at all.

Two reachable cases, both previously silent, and only one of them had a repair:

  1. Installed or upgraded on the LEGACY core, so the policy correctly wrote
     nothing. The deployment later moves onto the search-plan pipeline. Nothing
     re-ran, so the backstop is **absent for ever** and the module is relying
     solely on the ordering fix — which its own text says is *not* redundant
     with the parameter. 🔴 This is the case nothing repaired.
  2. The mirror image: a row this module wrote on a core that cannot read it.
     `migrations/19.0.3.10.0` repairs that once; this script keeps repairing it,
     because the move can happen again.

Odoo runs a `0.0.0` folder whenever the installed version is older than the
manifest's (`odoo/modules/migration.py:197`, `compare()`), and in the `post`
stage it is ordered LAST (`:168-173`), so it sees the end state the versioned
scripts produced rather than racing them.

🔴 AND UNTIL 19.0.3.17.0 IT DID NOT REPAIR CASE 1 EITHER (found in
review). The rule was *"write `True` only into an ABSENT key"*, with absence standing
in for "nobody has decided". On a search-plan core the key is never absent:
`account_edi_ubl_cii` ships `data/ir_config_parameter_data.xml` under
`noupdate="1"` holding `False`, and that addon is a direct `depends`. *(measured
with a read-only `SELECT` against a migrated test database)* the
`ir_config_parameter` row holds `False`, owned by that xmlid, `noupdate = t`. So after the move the
row exists holding `False`, this script reported *"left alone"*, and case 1 — the
case the paragraph above calls the one nothing repaired — stayed exactly as
unrepaired, on every upgrade of such a deployment, with a passing test above it that had
to DELETE core's own record to construct its premise.

🔴 IT NOW WRITES WHERE NOBODY HAS DECIDED, which is a different question from
"is there a row". `_ai_reassert_substitute_pdf_policy` holds the rule and the
argument, and `SUBSTITUTE_PDF_MARK_PARAM` is what distinguishes core's shipped
DEFAULT from an operator's DECISION — including an operator who turns the switch
off after we turned it on, which is still never undone. "Re-decided" and
"re-asserted" are different things and only the first one is wanted here.

⚠️ WHAT IT STILL CANNOT SEE, stated rather than left to be rediscovered: a core
upgraded under a module that is then never upgraded. Nothing runs, so nothing
re-decides. `CHANGELOG.md` says to call
`env['account.move']._ai_reassert_substitute_pdf_policy()` by hand after a core
migration, and that sentence is the only thing covering it.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # The install case belongs to `post_init_hook`, which calls the policy
    # itself. Odoo never runs a migration on an install anyway — the guard is
    # here so this file reads the same as its four versioned siblings.
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    outcome = env["account.move"]._ai_reassert_substitute_pdf_policy()
    _logger.info(
        "goodahead_invoice_ai_import: substitute-PDF switch re-decided against "
        "the core running now -> %s",
        outcome,
    )
