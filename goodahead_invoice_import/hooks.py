# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Install and uninstall hooks of goodahead_invoice_import.

PRE-INSTALL — the in-place rename of the import record.
Until the core/source split the import record was `ai.invoice.import`, declared by
`goodahead_invoice_ai_import`. That add-on now depends on this one, so
upgrading it (`-u goodahead_invoice_ai_import`) installs this core FIRST, and
this hook runs before the core creates any table:

- no old table                                     -> fresh install, do nothing
- old table, the AI add-on not being upgraded now  -> refuse with an instruction
- background jobs for the old model still queued   -> refuse, naming the count
- otherwise rename table, sequence, constraints, every model-name reference,
  the reflection xmlids, and move ownership of every record this core now
  declares from the AI add-on to the core.

🔴 OWNERSHIP IS THE PART THAT LOSES DATA WHEN IT IS WRONG, in both directions.
At the end of the upgrade Odoo deletes every record whose ONLY external id
belongs to an updated module that did not declare it this run
(`ir.model.data._process_end`). So:

* an id the core declares but that stays owned by the AI add-on is orphaned —
  and uninstalling the AI part later deletes core data with it;
* an id moved to the core that the core does NOT declare is deleted at the end
  of this very upgrade. For a field that means its column is dropped. This is
  why the AI add-on's own fields on the import record (`extraction_model`,
  `input_tokens`, `output_tokens`) keep their AI ownership: the core does not
  declare them, the AI add-on re-declares them, and Odoo reflects a field's
  external id under every module that declares it (`ir.model.fields.
  _reflect_fields`: `module in field._modules`, or the model's original module,
  or a module that introduced it through `_inherit`).

So nothing here is a hand-written list. What moves is computed from what the
core itself declares: the ids in its own manifest data files, and the models
and fields of its own model classes (Odoo has imported the core's Python before
calling this hook, so `MetaModel` already holds them).

Last, the state values of ADR-0002/ADR-0008 are mapped (`map_states`):
draft and processing -> waiting, done -> imported, failed stays. A database
that installed this core at 19.0.1.0.0 (before the new lifecycle) gets the
same mapping from `migrations/19.0.1.1.0`.

POST-INSTALL — the normalised bill-number expression index the duplicate rule
looks bills up by (the ORM cannot declare an expression index), the backfill
of `source`, `invoice_number_normalized`, `seller_vat` and
`seller_business_id` on records imported before (`backfill`), then the
install-time seeding of `goodahead.partner.identifier` and the substitute-PDF
policy, which used to be the AI add-on's `post_init_hook`.

UNINSTALL — drops that index.
"""

import ast
import csv
import logging
import os
from collections import defaultdict

from lxml import etree

from odoo import fields as odoo_fields
from odoo import models as odoo_models
from odoo.exceptions import UserError

from .lib import import_record

_logger = logging.getLogger(__name__)

OLD_MODEL = "ai.invoice.import"
NEW_MODEL = "goodahead.invoice.import"
OLD_TABLE = "ai_invoice_import"
NEW_TABLE = "goodahead_invoice_import"
AI_MODULE = "goodahead_invoice_ai_import"
CORE_MODULE = "goodahead_invoice_import"

#: queue_job states in which a job may still run against the old model name.
ACTIVE_JOB_STATES = ("pending", "enqueued", "started", "wait_dependencies")

#: Fields every model the core DEFINES carries without declaring them.
MAGIC_FIELDS = frozenset(
    {"id", "display_name", "create_uid", "create_date", "write_uid", "write_date"}
)

#: Column names that hold a model's technical name as text. Swept across every
#: table, so a module that is not installed here simply has no such column.
#: Only text columns are touched: on some versions the same names are integer
#: links to `ir_model`, whose ids never change.
MODEL_NAME_COLUMN_NAMES = (
    "model",
    "res_model",
    "model_name",
    "res_model_name",
    "parent_res_model",
    "relation",
    "model_id",
)
#: Text columns that hold a model name under some other column name.
EXTRA_MODEL_NAME_COLUMNS = (("ir_exports", "resource"),)

#: (ir.model.data model, old name prefix, new name prefix) — the reflection
#: xmlids Odoo derives from the model name.
REFLECTION_XMLID_PREFIXES = (
    ("ir.model.fields", "field_ai_invoice_import__", "field_goodahead_invoice_import__"),
    (
        "ir.model.fields.selection",
        "selection__ai_invoice_import__",
        "selection__goodahead_invoice_import__",
    ),
    (
        "ir.model.inherit",
        "model_inherit__ai_invoice_import__",
        "model_inherit__goodahead_invoice_import__",
    ),
    (
        "ir.model.constraint",
        "constraint_ai_invoice_import_",
        "constraint_goodahead_invoice_import_",
    ),
)

BILL_REF_INDEX = "goodahead_invoice_import_move_ref_norm_idx"

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# What the core declares
# ---------------------------------------------------------------------------


def core_manifest():
    """This add-on's manifest, read from disk (no ERP API, so it ports)."""
    with open(os.path.join(_HERE, "__manifest__.py"), encoding="utf-8") as fh:
        return ast.literal_eval(fh.read())


def _local_xmlid(xmlid):
    """``name`` for an id this module declares, None for another module's."""
    if "." not in xmlid:
        return xmlid
    module, name = xmlid.split(".", 1)
    return name if module == CORE_MODULE else None


def core_data_xmlids(manifest=None):
    """Every external id the core's own data files declare: XML ``record``,
    ``menuitem`` and ``template`` ids, and the ``id`` column of CSV files."""
    manifest = manifest or core_manifest()
    names = set()
    for rel_path in manifest.get("data", []):
        path = os.path.join(_HERE, *rel_path.split("/"))
        if rel_path.endswith(".xml"):
            tree = etree.parse(path)
            for node in tree.iter("record", "menuitem", "template"):
                xmlid = node.get("id")
                name = xmlid and _local_xmlid(xmlid)
                if name:
                    names.add(name)
        elif rel_path.endswith(".csv"):
            with open(path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    name = row.get("id") and _local_xmlid(row["id"])
                    if name:
                        names.add(name)
    return names


def _core_model_classes():
    """The core's model definition classes. Odoo imports an add-on's Python
    package before its pre-install hook runs, so they are registered already."""
    meta = odoo_models.MetaModel
    registry = getattr(meta, "_module_to_models__", None)
    if registry is None:  # 16.0 / 17.0 / 18.0 spelling
        registry = getattr(meta, "module_to_models", {})
    return list(registry.get(CORE_MODULE, []))


def _declared_field_names(cls):
    return {
        name
        for name, value in vars(cls).items()
        if isinstance(value, odoo_fields.Field)
    }


def _parents(cls):
    return [p for p in (cls._inherit or []) if p not in (cls._name, "base")]


def core_defined_models():
    """Models the core DEFINES (``_name`` not in its own ``_inherit``)."""
    return {
        cls._name
        for cls in _core_model_classes()
        if cls._name not in (cls._inherit or [])
    }


def core_parent_models():
    """Every model a core class names as a parent or extends."""
    parents = set()
    for cls in _core_model_classes():
        parents.update(p for p in (cls._inherit or []) if p != "base")
    return parents


def core_owned_fields(env):
    """``{model: {field names}}`` whose definition the core owns.

    For a model the core only extends, that is what its classes declare. For a
    model it DEFINES, it is also every field the model gets without declaring
    it: the magic fields and the fields of its parents (`mail.thread`).

    ⚠️ A parent's fields are read from `ir_model_fields` as well as from the
    registry. When this runs, the registry holds only the add-ons loaded before
    this one, and an add-on loaded later may extend `mail.thread` too; its
    fields are on the import record all the same, and the database already
    lists them under the parent. Whatever else the table carries belongs to an
    extending add-on and keeps that add-on's ownership.
    """
    defined = core_defined_models()
    owned = defaultdict(set)
    for cls in _core_model_classes():
        owned[cls._name] |= _declared_field_names(cls)
        if cls._name not in defined:
            continue
        owned[cls._name] |= MAGIC_FIELDS
        for parent in _parents(cls):
            if parent in env.registry:
                owned[cls._name] |= set(env[parent]._fields)
            env.cr.execute("SELECT name FROM ir_model_fields WHERE model = %s", (parent,))
            owned[cls._name] |= {name for (name,) in env.cr.fetchall()}
    return dict(owned)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _table_exists(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table,),
    )
    return bool(cr.fetchone())


def _column_type(cr, table, column):
    cr.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cr.fetchone()
    return row[0] if row else None


def _module_state(cr, name):
    cr.execute("SELECT state FROM ir_module_module WHERE name = %s", (name,))
    row = cr.fetchone()
    return row[0] if row else None


def _module_id(cr, name):
    cr.execute("SELECT id FROM ir_module_module WHERE name = %s", (name,))
    row = cr.fetchone()
    return row[0] if row else None


def assert_supported_path(cr, old_model=OLD_MODEL):
    """Refuse outside the one supported path. Changes nothing."""
    if _module_state(cr, AI_MODULE) != "to upgrade":
        raise UserError(
            f"The invoice import core ({CORE_MODULE}) can only be installed on "
            f"this database by upgrading {AI_MODULE}: it renames that module's "
            f"import records ({old_model}) in place, and that has to happen in "
            f"the same run as the {AI_MODULE} upgrade. Run "
            f"`-u {AI_MODULE}` instead."
        )
    if not _table_exists(cr, "queue_job"):
        return
    cr.execute(
        "SELECT count(*) FROM queue_job WHERE model_name = %s AND state IN %s",
        (old_model, ACTIVE_JOB_STATES),
    )
    pending = cr.fetchone()[0]
    if pending:
        raise UserError(
            f"{pending} invoice import job(s) for {old_model} are still queued or "
            "running. Let them finish (or cancel them) and run the upgrade again; "
            "no job may survive the rename of the model it runs on."
        )


# ---------------------------------------------------------------------------
# The rename
# ---------------------------------------------------------------------------


def _rename_table(cr):
    cr.execute(f'ALTER TABLE "{OLD_TABLE}" RENAME TO "{NEW_TABLE}"')
    cr.execute(f'ALTER SEQUENCE IF EXISTS "{OLD_TABLE}_id_seq" RENAME TO "{NEW_TABLE}_id_seq"')
    # Constraint and index names carry the table name, and Odoo finds its own
    # foreign keys BY NAME (`<table>_<column>_fkey`); left alone, it would add
    # a second copy of each under the new name.
    prefix = OLD_TABLE + "_"
    cr.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass "
        "AND left(conname, %s) = %s",
        (NEW_TABLE, len(prefix), prefix),
    )
    for (conname,) in cr.fetchall():
        new_name = NEW_TABLE + conname[len(OLD_TABLE):]
        cr.execute(f'ALTER TABLE "{NEW_TABLE}" RENAME CONSTRAINT "{conname}" TO "{new_name}"')
    cr.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = %s AND left(indexname, %s) = %s",
        (NEW_TABLE, len(prefix), prefix),
    )
    for (indexname,) in cr.fetchall():
        new_name = NEW_TABLE + indexname[len(OLD_TABLE):]
        cr.execute(f'ALTER INDEX "{indexname}" RENAME TO "{new_name}"')
    cr.execute(
        "UPDATE ir_model_constraint SET name = %s || substr(name, %s) "
        "WHERE left(name, %s) = %s AND model = (SELECT id FROM ir_model WHERE model = %s)",
        (NEW_TABLE + "_", len(prefix) + 1, len(prefix), prefix, OLD_MODEL),
    )


def model_name_columns(cr):
    """Every text column, in every table, that holds a model name."""
    cr.execute(
        "SELECT c.table_name, c.column_name FROM information_schema.columns c "
        "JOIN information_schema.tables t ON t.table_schema = c.table_schema "
        "AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE' "
        "WHERE c.table_schema = current_schema() "
        "AND c.data_type IN ('character varying', 'text') "
        "AND c.column_name IN %s ORDER BY 1, 2",
        (MODEL_NAME_COLUMN_NAMES,),
    )
    columns = cr.fetchall()
    for table, column in EXTRA_MODEL_NAME_COLUMNS:
        if _column_type(cr, table, column) in ("character varying", "text"):
            columns.append((table, column))
    return columns


def rename_model_references(cr, old_model, new_model):
    """Rewrite every stored model name: registry rows, actions, views,
    messages, followers, attachments, activities, filters, templates, job
    records. Returns ``{(table, column): rows}`` for the log."""
    counts = {}
    for table, column in model_name_columns(cr):
        cr.execute(
            f'UPDATE "{table}" SET "{column}" = %s WHERE "{column}" = %s',  # noqa: S608
            (new_model, old_model),
        )
        if cr.rowcount:
            counts[(table, column)] = cr.rowcount
    # queue_job keeps the model in three more shapes besides `model_name`: the
    # serialized recordset (a done job whose records name a model that no
    # longer exists cannot even be opened), the display string, and the job
    # function's name, which the channel method mirrors.
    if _column_type(cr, "queue_job", "records") == "jsonb":
        cr.execute(
            "UPDATE queue_job SET records = jsonb_set(records, '{model}', to_jsonb(%s::text)) "
            "WHERE records->>'model' = %s",
            (new_model, old_model),
        )
        counts[("queue_job", "records")] = cr.rowcount
    old_call, new_call = old_model + "(", new_model + "("
    old_fn, new_fn = f"<{old_model}>.", f"<{new_model}>."
    for table, column, old, new in (
        ("queue_job", "func_string", old_call, new_call),
        ("queue_job", "channel_method_name", old_fn, new_fn),
        ("queue_job_function", "name", old_fn, new_fn),
    ):
        if _column_type(cr, table, column) not in ("character varying", "text"):
            continue
        cr.execute(
            f'UPDATE "{table}" SET "{column}" = %s || substr("{column}", %s) '  # noqa: S608
            f'WHERE left("{column}", %s) = %s',
            (new, len(old) + 1, len(old), old),
        )
        if cr.rowcount:
            counts[(table, column)] = cr.rowcount
    return counts


def _rename_reflection_xmlids(cr):
    """The external ids Odoo derives from the model name. They keep their
    owning module here; `move_ownership` decides which ones move.

    ⚠️ EVERY MODULE, NOT ONLY THE AI ADD-ON. Odoo gives an inheritance row an
    id under each module that contributes a class to it, so `portal`, `sms` and
    `snailmail` (which extend `mail.thread`) each hold a
    `model_inherit__ai_invoice_import__mail_thread` of their own (measured on
    the rehearsal database). Left under the old name they are stale ids nobody
    re-declares."""
    renames = [("ir.model", "model_ai_invoice_import", "model_goodahead_invoice_import")]
    renames += list(REFLECTION_XMLID_PREFIXES)
    for model, old, new in renames:
        cr.execute(
            "UPDATE ir_model_data d SET name = %s || substr(d.name, %s) "
            "WHERE d.model = %s AND left(d.name, %s) = %s AND NOT EXISTS ("
            "SELECT 1 FROM ir_model_data x WHERE x.module = d.module "
            "AND x.name = %s || substr(d.name, %s))",
            (new, len(old) + 1, model, len(old), old, new, len(old) + 1),
        )


def move_ownership(env, from_module=AI_MODULE, to_module=CORE_MODULE):
    """Hand every record the core declares from ``from_module`` to the core.

    Returns ``{what: rows}`` for the log and the tests."""
    cr = env.cr
    moved = {}
    defined = sorted(core_defined_models())
    owned_fields = core_owned_fields(env)
    from_id = _module_id(cr, from_module)
    to_id = _module_id(cr, to_module)

    def _update(label, query, params):
        cr.execute(query, params)
        moved[label] = moved.get(label, 0) + cr.rowcount

    def _move_xmlids(label, from_clause, where, params):
        # Idempotent: an id the core already holds under the same name is left
        # where it is rather than colliding with it.
        _update(
            label,
            f"UPDATE ir_model_data d SET module = %s {from_clause} "
            f"WHERE d.module = %s AND {where} AND NOT EXISTS ("
            "SELECT 1 FROM ir_model_data x WHERE x.module = %s AND x.name = d.name)",
            (to_module, from_module, *params, to_module),
        )

    # 1. Records of the core's data files: views, actions, menus, access
    #    rights, shipped expense categories — under their unchanged names.
    data_names = sorted(core_data_xmlids())
    if data_names:
        _move_xmlids("data", "", "d.name = ANY(%s::text[])", (data_names,))
    # 2. The models the core defines.
    if defined:
        _move_xmlids(
            "ir.model",
            "FROM ir_model m",
            "d.model = 'ir.model' AND d.res_id = m.id AND m.model = ANY(%s::text[])",
            (defined,),
        )
    # 3. Fields, and the selection values of those fields — only the ones the
    #    core declares (see the module docstring for why that matters).
    for model, names in sorted(owned_fields.items()):
        names = sorted(names)
        _move_xmlids(
            "ir.model.fields",
            "FROM ir_model_fields f",
            "d.model = 'ir.model.fields' AND d.res_id = f.id "
            "AND f.model = %s AND f.name = ANY(%s::text[])",
            (model, names),
        )
        _move_xmlids(
            "ir.model.fields.selection",
            "FROM ir_model_fields_selection s JOIN ir_model_fields f ON f.id = s.field_id",
            "d.model = 'ir.model.fields.selection' AND d.res_id = s.id "
            "AND f.model = %s AND f.name = ANY(%s::text[])",
            (model, names),
        )
    # 4. Inheritance rows towards a parent a core class names. Where the AI
    #    add-on still contributes the same row, it re-declares its own id.
    parents = sorted(core_parent_models())
    if parents:
        _move_xmlids(
            "ir.model.inherit",
            "FROM ir_model_inherit i JOIN ir_model p ON p.id = i.parent_id",
            "d.model = 'ir.model.inherit' AND d.res_id = i.id AND p.model = ANY(%s::text[])",
            (parents,),
        )
    # 5. SQL constraints and many2many tables of the models the core defines.
    #    `ir_model_constraint.module` decides who DROPS a constraint on
    #    uninstall, so the column moves as well as the id. A foreign key on a
    #    column the core does not declare stays with its module.
    if defined and from_id and to_id:
        kept_fkeys = []
        for model in defined:
            table = model.replace(".", "_")
            cr.execute("SELECT name FROM ir_model_fields WHERE model = %s", (model,))
            kept_fkeys += [
                f"{table}_{name}_fkey"
                for (name,) in cr.fetchall()
                if name not in owned_fields.get(model, ())
            ]
        _update(
            "ir.model.constraint",
            "UPDATE ir_model_constraint c SET module = %s FROM ir_model m "
            "WHERE c.model = m.id AND c.module = %s AND m.model = ANY(%s::text[]) "
            "AND NOT c.name = ANY(%s::text[]) AND NOT EXISTS ("
            "SELECT 1 FROM ir_model_constraint x WHERE x.module = %s AND x.name = c.name)",
            (to_id, from_id, defined, kept_fkeys, to_id),
        )
        _move_xmlids(
            "ir.model.constraint xmlid",
            "FROM ir_model_constraint c",
            "d.model = 'ir.model.constraint' AND d.res_id = c.id AND c.module = %s",
            (to_id,),
        )
        _update(
            "ir.model.relation",
            "UPDATE ir_model_relation r SET module = %s FROM ir_model m "
            "WHERE r.model = m.id AND r.module = %s AND m.model = ANY(%s::text[]) "
            "AND NOT EXISTS (SELECT 1 FROM ir_model_relation x "
            "WHERE x.module = %s AND x.name = r.name)",
            (to_id, from_id, defined, to_id),
        )
    return moved


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


def map_states(cr, table=NEW_TABLE):
    """Rewrite the pre-split state values in place: draft and
    processing -> waiting, done -> imported; failed and every new value stay.
    Plain SQL, so it runs before the model declares the new selection as
    well as after. Returns ``{old: rows}``."""
    if _column_type(cr, table, "state") not in ("character varying", "text"):
        return {}
    counts = {}
    for old, new in sorted(import_record.LEGACY_STATE_MAP.items()):
        cr.execute(
            f'UPDATE "{table}" SET state = %s WHERE state = %s',  # noqa: S608
            (new, old),
        )
        if cr.rowcount:
            counts[old] = cr.rowcount
    return counts


def backfill(env):
    """Fill `source`, `invoice_number_normalized`, `seller_vat` and
    `seller_business_id` on records imported before the core/source split, so the duplicate
    rule sees them too (data-model.md, staged script 02).

    🔴 SQL, NOT `record.write`: an ORM write would stamp every old record
    with a new `write_date` and `write_uid`, and the failed-imports bubble
    orders by `write_date`. Idempotent: only rows whose number is still
    empty are read, and a row whose extraction has no number is rewritten
    with the same values on a second run."""
    cr = env.cr
    columns = ("source", "invoice_number_normalized", "seller_vat", "seller_business_id")
    if not all(_column_type(cr, NEW_TABLE, column) for column in columns):
        return 0
    cr.execute(
        f'SELECT id, source_email, extraction_data FROM "{NEW_TABLE}" '  # noqa: S608
        "WHERE invoice_number_normalized IS NULL OR invoice_number_normalized = ''"
    )
    rows = cr.fetchall()
    for record_id, source_email, extraction_data in rows:
        vals = import_record.backfill_values(source_email, extraction_data)
        cr.execute(
            f'UPDATE "{NEW_TABLE}" SET source = %s, invoice_number_normalized = %s, '  # noqa: S608
            "seller_vat = %s, seller_business_id = %s WHERE id = %s",
            (
                vals["source"],
                vals["invoice_number_normalized"] or None,
                vals["seller_vat"] or None,
                vals["seller_business_id"] or None,
                record_id,
            ),
        )
    if rows:
        env[NEW_MODEL].invalidate_model(list(columns))
    _logger.info("Backfilled source and invoice numbers on %s import record(s)", len(rows))
    return len(rows)


def pre_init_hook(env):
    cr = env.cr
    if not _table_exists(cr, OLD_TABLE):
        return  # fresh install
    assert_supported_path(cr)
    _logger.info("Renaming %s to %s in place", OLD_MODEL, NEW_MODEL)
    _rename_table(cr)
    references = rename_model_references(cr, OLD_MODEL, NEW_MODEL)
    _rename_reflection_xmlids(cr)
    moved = move_ownership(env)
    mapped = map_states(cr)
    _logger.info(
        "Renamed %s to %s: references %s; ownership moved to %s: %s; "
        "states mapped: %s",
        OLD_MODEL,
        NEW_MODEL,
        {f"{t}.{c}": n for (t, c), n in references.items()},
        CORE_MODULE,
        moved,
        mapped,
    )


def create_bill_ref_index(cr):
    """The duplicate rule looks vendor bills up by company and normalised
    reference. A plain CREATE INDEX: an install runs in one transaction, so
    CONCURRENTLY is not allowed; on a large bill table schedule accordingly."""
    cr.execute(
        f"CREATE INDEX IF NOT EXISTS {BILL_REF_INDEX} ON account_move "
        "(company_id, upper(regexp_replace(ref, '[^A-Za-z0-9]', '', 'g'))) "
        "WHERE move_type IN ('in_invoice', 'in_refund')"
    )


def drop_bill_ref_index(cr):
    cr.execute(f"DROP INDEX IF EXISTS {BILL_REF_INDEX}")


def post_init_hook(env):
    create_bill_ref_index(env.cr)
    # Before the early return below: on the rename path this is where the
    # records the AI add-on imported get their source and numbers. On a fresh
    # install there are no records and it costs one empty query.
    backfill(env)

    # 🔴 ONLY ON A DATABASE THAT NEVER RAN THE AI IMPORT. Where it ran, this
    # install is the rename path: the identifiers were seeded and the
    # substitute-PDF policy decided long ago, and the AI add-on's
    # `migrations/0.0.0` re-decides the policy on this very upgrade. Applying it
    # again here would overrule an administrator who turned the switch off, and
    # re-seeding would bring back identifiers somebody deleted.
    if _module_state(env.cr, AI_MODULE) in ("to upgrade", "installed"):
        return

    # 🔴 THE SEED RUNS FROM TWO PLACES AND BOTH ARE NEEDED. A `post_init_hook`
    # runs on INSTALL and never on upgrade; the AI add-on's
    # `migrations/19.0.3.0.0/post-*.py` script runs on UPGRADE and never on a
    # fresh install. Either one alone leaves a real population unseeded, and an
    # unseeded database is the failure this feature cannot survive: every phone
    # and domain step declines, the import goes on duplicating vendors, and
    # nothing anywhere reports a problem. `_seed_from_partners` is idempotent
    # (`ON CONFLICT DO NOTHING`), so running both costs one extra pass.
    env["goodahead.partner.identifier"]._seed_from_partners(
        source="seeded from res.partner (install)"
    )

    # Same install/upgrade split as above: the
    # `migrations/19.0.3.1.0/post-disable-substitute-pdf.py` script runs on an
    # UPGRADE and never on a fresh install, so without this line a newly
    # installed database keeps core's substitute "Generated by Odoo" PDF
    # enabled. Conditional on a core that READS the parameter — see
    # `account.move._ai_apply_substitute_pdf_policy`. The attachment-ordering
    # half of this fix is unconditional and lives in
    # `goodahead.invoice.import._finalize_import`.
    env["account.move"]._ai_apply_substitute_pdf_policy()


def uninstall_hook(env):
    drop_bill_ref_index(env.cr)
