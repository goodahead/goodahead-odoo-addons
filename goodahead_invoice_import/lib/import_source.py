# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The source interface's value objects (core/source split; contracts/public-api.md
§1.1, §1.3). Pure Python: nothing here imports the ERP.

* `ImportResult` — what `goodahead.invoice.import._import_from_invoice_data`
  hands back to the source that called it.
* `MatchingPolicy` — which seller identifiers may SET the vendor on a bill and
  which may only PROPOSE one. Two constants: `DEFAULT_POLICY` is the
  matcher as it has always been (the AI source passes it), `STRICT_POLICY` is
  the one structured e-invoices pass.
* `SourceReader` — what a source add-on registers so the core can route mail
  attachments and import jobs to it in a fixed order.
"""

from dataclasses import dataclass, field

#: The seller identifiers the matcher knows, strongest first. The order is the
#: matcher's own (`res.partner._ai_find_vendor`).
KEY_VAT = "vat"
KEY_BUSINESS_ID = "business_id"
KEY_PHONE = "phone"
KEY_DOMAIN = "domain"
KEY_NAME = "name"
ALL_KEYS = (KEY_VAT, KEY_BUSINESS_ID, KEY_PHONE, KEY_DOMAIN, KEY_NAME)

#: `conflict`: what happens when the assigning identifiers point at more than
#: one vendor company (several companies share one identifier, or the VAT
#: number and the Business ID name different companies).
#: ``leave_empty`` — no vendor, every one of them offered.
#: ``first_match`` — the matcher as it was before the core/source split: the first identifier
#: in `ALL_KEYS` order that finds a vendor wins, and an identifier shared by
#: several companies resolves to the established one. Only `DEFAULT_POLICY`
#: uses it, so the AI import keeps its behaviour.
CONFLICT_LEAVE_EMPTY = "leave_empty"
CONFLICT_FIRST_MATCH = "first_match"

OUTCOME_IMPORTED = "imported"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class MatchingPolicy:
    """Which identifiers assign the vendor and which only propose candidates."""

    assign_by: tuple = ALL_KEYS
    propose_by: tuple = ()
    conflict: str = CONFLICT_FIRST_MATCH
    #: How many weak (proposed) candidates are offered. Vendors offered because
    #: an ASSIGNING identifier was ambiguous are never capped.
    candidate_limit: int = 3
    #: True: the decoder may only LOOK UP a bank account the vendor already
    #: holds and never creates one; an account registered to another partner
    #: is not attached, not suggested, and its owner is named in the chatter
    #:. False keeps the AI import as it always was.
    bank_account_lookup_only: bool = False

    def __post_init__(self):
        unknown = (set(self.assign_by) | set(self.propose_by)) - set(ALL_KEYS)
        if unknown:
            raise ValueError(f"Unknown matching keys: {sorted(unknown)}")
        if self.conflict not in (CONFLICT_LEAVE_EMPTY, CONFLICT_FIRST_MATCH):
            raise ValueError(f"Unknown conflict rule: {self.conflict!r}")

    @property
    def is_legacy(self):
        """The matcher as it was before the core/source split — run unchanged, through the
        same methods, so the AI import's behaviour cannot drift."""
        return (
            tuple(self.assign_by) == ALL_KEYS
            and self.conflict == CONFLICT_FIRST_MATCH
        )

    def assigns(self, key):
        return key in self.assign_by

    def proposes(self, key):
        return key in self.propose_by or key in self.assign_by


#: Today's behaviour: every identifier assigns (the AI source).
DEFAULT_POLICY = MatchingPolicy()

#: Structured e-invoices: only the VAT number and the Business ID set
#: the vendor; phone, web/email domain and name only propose candidates; an
#: ambiguous or conflicting strong match leaves the vendor empty; a bank account
#: printed on the document is only looked up on the vendor, never created.
STRICT_POLICY = MatchingPolicy(
    assign_by=(KEY_VAT, KEY_BUSINESS_ID),
    propose_by=(KEY_PHONE, KEY_DOMAIN, KEY_NAME),
    conflict=CONFLICT_LEAVE_EMPTY,
    bank_account_lookup_only=True,
)


@dataclass(frozen=True)
class SourceReader:
    """One installed reader of import records (core/source split).

    ``key`` is the value the record stores in ``reader`` and the key every
    source hook of `goodahead.invoice.import` dispatches on. ``priority`` is
    the order in which the mail intake asks the readers whether they take an
    attachment: lower first, ties broken by ``key`` — never by module load
    order. ``policy`` is the `MatchingPolicy` the reader imports and
    re-imports under. ``structured`` is True for a reader of structured data:
    no AI reading, so no AI usage is recorded, not even on a re-import."""

    key: str
    priority: int
    policy: MatchingPolicy = DEFAULT_POLICY
    structured: bool = False


def readers_in_order(readers):
    """The keys of ``readers`` (a ``{key: SourceReader}`` dict) in the order
    the core asks them: priority, then key. Independent of the dict's own
    order, which is the order the add-ons happened to load in."""
    return [
        reader.key
        for reader in sorted(readers.values(), key=lambda r: (r.priority, r.key))
    ]


@dataclass
class ImportResult:
    """contracts/public-api.md §1.1.

    ``move`` and ``duplicate_of`` are `account.move` recordsets (possibly
    empty) — or None when the caller had no environment to make an empty one.
    ``code`` and ``message`` are set when ``outcome`` is failed. ``detail`` is
    the short text a queue job reports as its result."""

    outcome: str
    move: object = None
    duplicate_of: object = None
    code: str = ""
    message: str = ""
    detail: str = field(default="", compare=False)

    @property
    def ok(self):
        return self.outcome == OUTCOME_IMPORTED
