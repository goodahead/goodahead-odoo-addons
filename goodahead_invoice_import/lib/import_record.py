# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Pure helpers of the import record's lifecycle (core/source split).

Imports nothing from the ERP, so the rename hook, the 19.0.1.1.0 migration,
the model and the duplicate rule share one definition of each rule.
"""

import json
import re

#: Stored lifecycle values. "processing" is NOT one of them: it is
#: derived from the record's background job and only ever displayed.
STATE_WAITING = "waiting"
STATE_IMPORTED = "imported"
STATE_DUPLICATE = "duplicate"
STATE_FAILED = "failed"
STATE_POSSIBLY_LOST = "possibly_lost"
STATE_CLOSED = "closed"
STATE_PROCESSING = "processing"  # display only

#: Settled = nothing more will happen to the record (glossary, ADR-0008).
SETTLED_STATES = frozenset({STATE_IMPORTED, STATE_CLOSED})

#: Values of the lifecycle before the split into a core and sources -> their new value.
#: `failed` keeps its value and is therefore not listed.
LEGACY_STATE_MAP = {
    "draft": STATE_WAITING,
    "processing": STATE_WAITING,
    "done": STATE_IMPORTED,
}

#: queue_job states in which the record's job still has work to do, so the
#: record is shown as processing. `wait_dependencies` is not one: the import
#: job is never part of a graph.
ACTIVE_JOB_STATES = ("pending", "enqueued", "started")

#: A record shown as processing for longer than this is flagged (ADR-0008:
#: a job the worker lost stays "started" until the job runner requeues it).
STALE_PROCESSING_MINUTES = 30

#: Identity key of the import job. The legacy prefix is
#: what jobs enqueued before the split into a core and sources carry; the rename hook refuses while any
#: of them is still queued, so it is only ever seen on finished jobs.
IDENTITY_KEY_PREFIX = "invoice-import:"
LEGACY_IDENTITY_KEY_PREFIX = "ai_invoice_import:"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def map_legacy_state(value):
    """The lifecycle value an old stored ``value`` becomes."""
    return LEGACY_STATE_MAP.get(value, value)


def identity_key(record_id):
    return f"{IDENTITY_KEY_PREFIX}{record_id}"


def is_import_identity_key(key, record_id):
    """Whether ``key`` names the import job of ``record_id``, under the new or
    the pre-split prefix."""
    return key in (
        f"{IDENTITY_KEY_PREFIX}{record_id}",
        f"{LEGACY_IDENTITY_KEY_PREFIX}{record_id}",
    )


def normalize_invoice_number(value):
    """The invoice number as the duplicate rule compares it: case, spaces and
    separators ignored. Returns "" when nothing is left.

    ⚠️ ASCII letters and digits only, on purpose: the expression index on
    vendor bills (`hooks.create_bill_ref_index`) uses the same
    ``[^A-Za-z0-9]`` rule, and the two must agree or the lookup misses.
    """
    if value is None or value is False:
        return ""
    return _NON_ALNUM.sub("", str(value)).upper()


def normalize_vat(value):
    """A VAT number as the duplicate rule compares it: upper case, no
    whitespace (``"fi 2345 6789"`` -> ``"FI23456789"``). Returns "" when
    nothing is left.

    ⚠️ The duplicate rule's SQL compares the partners' VAT with
    ``upper(replace(vat, ' ', ''))``; keep the two in step."""
    if value is None or value is False:
        return ""
    return "".join(str(value).split()).upper()


def parse_extraction(extraction_data):
    """The stored extraction JSON as a dict; {} when absent or unreadable."""
    if not extraction_data:
        return {}
    try:
        data = json.loads(extraction_data)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def invoice_identity_values(data):
    """``invoice_number_normalized``, ``seller_vat``, ``seller_business_id``
    from an extraction dict (False where unknown, as the ORM stores it).
    ``seller_vat`` is stored normalised (`normalize_vat`), so the duplicate
    rule's lookup of an earlier record compares like with like."""
    data = data if isinstance(data, dict) else {}
    seller = data.get("seller")
    seller = seller if isinstance(seller, dict) else {}
    return {
        "invoice_number_normalized": normalize_invoice_number(data.get("invoice_number"))
        or False,
        "seller_vat": normalize_vat(seller.get("vat")) or False,
        "seller_business_id": _clean(seller.get("business_id")),
    }


def backfill_values(source_email, extraction_data):
    """The values a record imported before the split into a core and sources gets:
    the source (email when it came by mail, else upload) and the numbers
    the duplicate rule looks it up by."""
    vals = {"source": "email" if source_email else "upload"}
    vals.update(invoice_identity_values(parse_extraction(extraction_data)))
    return vals


def _clean(value):
    if value is None or value is False:
        return False
    value = str(value).strip()
    return value or False
