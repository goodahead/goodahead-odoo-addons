# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import json
import logging
import re

from lxml import etree
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.addons.base.models.res_bank import sanitize_account_number
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare

from ..lib.bank_numbers import (
    aba_checksum_fails,
    iban_checksum_fails,
    payee_account_number,
)
from ..lib.invoice_data import (
    CURRENCY_INFERRED_KEY,
    CURRENCY_MARKING_KEY,
    CURRENCY_SYMBOL_KEY,
    TOLERANCE_ABS,
    nothing_was_printed,
    one_country_on_both_sides,
)
from ..lib.import_source import (
    DEFAULT_POLICY,
    KEY_BUSINESS_ID,
    KEY_DOMAIN,
    KEY_NAME,
    KEY_PHONE,
    KEY_VAT,
    MatchingPolicy,
)
from ..adapters.ubl_generator import generate_peppol_invoice_xml
from .res_partner import MATCHING_POLICY_CONTEXT_KEY, like_literal

_logger = logging.getLogger(__name__)

# `iban_checksum_fails` is re-exported for callers (and tests) that have always
# imported it from here. The implementation moved to `lib/bank_numbers.py` when
# the US routing-number check joined it — see that module for why the two
# guards are not symmetrical in what they can protect.
__all__ = ["iban_checksum_fails"]

# 🔴 THE SAFE HALF, AND NOTHING ELSE. Each pair is (a `res.partner` field, the
# key the extraction carries it under). A field in this tuple is written WITHOUT
# ASKING ANYBODY when the vendor's own field is EMPTY, because filling a blank
# cannot lose anything — and this is where the value is: phone, email and
# website, the three things that make the NEXT bill match, are usually the empty
# ones.
#
# 🔴 WHAT IS NOT HERE IS THE POINT:
#   * `vat` and a bank account are absent EVEN WHEN EMPTY. Invoice fraud IS a
#     changed bank account, and vendor invoices do carry printed warnings
#     about that exact scam.
#   * `name` is absent from this list AND from the suggestions. It is the one
#     field somebody curated on purpose — `NORTHWIND - Smith reps` against the
#     `Northwind` the manufacturer prints on its own paper.
#
# ⚠️ `street2` has NO key in the extraction schema (`lib/claude_client.py` emits
# a single `street`), so it never fires today. It is listed because the field is
# in scope for the rule and a schema that grows a second address line should not
# also need this tuple remembered.
_AI_VENDOR_AUTO_FIELDS = (
    ("street", "street"),
    ("street2", "street2"),
    ("city", "city"),
    ("zip", "postal_code"),
    # 🔴 COUNTRY BEFORE STATE, and the order is load-bearing rather than tidy. A
    # state code is only unique inside a country, so `_ai_resolve_location`
    # looks the state up against the vendor's `country_id` — which, on a vendor
    # record that had neither, is the one this very loop has just written. With
    # the pair the other way round the state silently declines on exactly the
    # empty records this feature exists to fill.
    ("country_id", "country_code"),
    ("state_id", "state_code"),
    ("phone", "phone"),
    ("email", "email"),
    ("website", "website"),
)

#: The contact details a document anybody can send must not fill by itself
#: (`MatchingPolicy.contact_details_need_review`): where the vendor's mail
#: goes, and what the matcher learns the vendor by.
_AI_CONTACT_FIELDS = ("phone", "email", "website")


class AccountMove(models.Model):
    _inherit = "account.move"

    # ------------------------------------------------------------------
    # F27 part 2 — what the bill offers, where the accountant already looks
    # ------------------------------------------------------------------
    #
    # 🔴 NOTHING GOES IN A NEW PLACE. The first design was a "Vendor Data" tab
    # and it was rejected on sight: buried in the last tab it gets lost, the
    # accountant just searches for a vendor and the data never improves. A tab
    # is optional work for somebody with a bill to finish, and optional work
    # does not happen. So: the candidates sit under the Vendor field the
    # accountant is already clicking, and the conflicts are ONE line beside it.

    # 🔴 `groups=` ON THESE THREE IS NOT A REFINEMENT. WITHOUT IT, NOBODY OUTSIDE ACCOUNTING CAN
    # OPEN AN INVOICE AT ALL.
    #
    # The failure it prevents: a salesperson opening the invoice on an order got
    #
    #     Access Error — Failed to read field account.move.ai_vendor_candidate_ids
    #     You are not allowed to access 'Suggested vendor for an unrecognised bill'
    #     (goodahead.vendor.candidate) records.
    #
    # The cause is the ordinary shape of this mistake and it is worth naming precisely: these are
    # relational fields on a CORE model that EVERYONE reads, pointing at models whose
    # `ir.model.access` grants only `account.group_account_invoice`. The form view lists them, the
    # web client therefore reads them for whoever opened it, and the ACL refuses. The module is
    # about vendor bills, so it was only ever exercised by accountants — and a customer invoice is
    # the same `account.move`, opened by people who are not.
    #
    # ⚠️ THE FIX IS NOT TO WIDEN THE ACL. `goodahead.vendor.data.suggestion` carries
    # `invoice_value` / `record_value` and an `is_sensitive` flag precisely because those columns
    # can hold a vendor's BANK DETAILS. Granting every internal user read on it to stop an error
    # would trade a broken screen for a data leak, which is the worse of the two.
    #
    # So the fields are restricted instead. `get_view` strips a field the reader's groups do not
    # allow, so a non-accounting user never requests it, the ACL is never consulted, and nothing
    # is exposed to anybody. Accounting users see exactly what they saw before.
    #
    # 🔴 THE COUNT NEEDS IT TOO, AND THAT IS THE EASY ONE TO MISS. `_compute_ai_vendor_suggestion_count`
    # reads `ai_vendor_suggestion_ids`, so leaving the count unrestricted leaves the same
    # AccessError one step further in — raised from a compute rather than a read, which is harder
    # to recognise as the same defect.
    _AI_VENDOR_GROUPS = "account.group_account_invoice"

    ai_vendor_candidate_ids = fields.One2many(
        "goodahead.vendor.candidate",
        "move_id",
        string="Suggested vendors",
        groups=_AI_VENDOR_GROUPS,
    )
    ai_vendor_suggestion_ids = fields.One2many(
        "goodahead.vendor.data.suggestion",
        "move_id",
        string="Vendor detail suggestions",
        groups=_AI_VENDOR_GROUPS,
    )
    ai_vendor_suggestion_count = fields.Integer(
        compute="_compute_ai_vendor_suggestion_count",
        string="Details that differ",
        groups=_AI_VENDOR_GROUPS,
    )
    ai_vendor_review_deferred = fields.Boolean(
        string="Vendor review deferred",
        copy=False,
        help="“Not now”. The suggestions stay PENDING — they are the queue's "
        "work, not this bill's — and the line stops asking the person who is "
        "trying to file a bill. Never her problem twice.",
    )

    # 🔴 THE BILL CARRIES THE PDF'S LINES AND NOTHING ELSE; A DIFFERENCE IS A
    # RED BANNER (19.0.3.31.0). 19.0.3.30.0 added a "Sales tax (as printed on
    # the invoice)" line; the requirement since is no extra lines — the PDF's
    # lines only — and any discrepancy shown as a red banner for a person to
    # review.
    #
    # Why there is a discrepancy at all: a company with no
    # active purchase tax, so Odoo's decoder maps a printed rate to nothing and
    # an imported bill comes in without its sales tax. That is now shown, not
    # repaired: the printed total and tax are stored here, and the form shows
    # both against the bill's LIVE figures until they agree.
    #
    # `ai_check_total` exists so that bills imported before 19.0.3.30.0, which
    # have nothing stored, never show the banner.
    ai_document_total = fields.Monetary(
        string="Invoice total (as printed)",
        currency_field="currency_id",
        copy=False,
        readonly=True,
        help="The payable total printed on the imported invoice.",
    )
    ai_document_tax = fields.Monetary(
        string="Invoice tax (as printed)",
        currency_field="currency_id",
        copy=False,
        readonly=True,
        help="The tax printed on the imported invoice.",
    )
    ai_check_total = fields.Boolean(copy=False, readonly=True)
    ai_total_mismatch = fields.Boolean(
        string="Total differs from the invoice",
        compute="_compute_ai_total_mismatch",
    )
    ai_tax_mismatch = fields.Boolean(
        string="Tax differs from the invoice",
        compute="_compute_ai_total_mismatch",
    )

    @api.depends(
        "ai_check_total", "ai_document_total", "ai_document_tax",
        "amount_total", "amount_tax",
    )
    def _compute_ai_total_mismatch(self):
        """Does the bill's total, or its tax, differ from what was printed?

        `ai_total_mismatch` is the banner's switch and is true when EITHER
        differs: a bill whose total is right but whose tax is wrong (tax booked
        as an expense line by hand, say) still needs a person to look.
        `ai_tax_mismatch` only decides whether the banner mentions tax.

        ⚠️ `TOLERANCE_ABS` ALONE, NOT THE RELATIVE TERM. These are the same
        figures restated, not a sum of parts the vendor rounded, so the check
        is the one `claude_client._reconciles(..., accumulated=False)` makes
        for `payable` against `tax_inclusive`. Half a percent of a $10,000 bill
        would let $50 through.
        """
        for move in self:
            if not move.ai_check_total:
                move.ai_total_mismatch = False
                move.ai_tax_mismatch = False
                continue
            # `float_compare` at the currency's rounding, not a bare `>`:
            # 180.90 - 180.88 is 0.020000000000010232 in floating point, and
            # a difference of exactly the tolerance must not raise the banner.
            rounding = move.currency_id.rounding or 0.01
            total_off = float_compare(
                abs(move.amount_total - move.ai_document_total),
                TOLERANCE_ABS,
                precision_rounding=rounding,
            ) > 0
            tax_off = float_compare(
                abs(move.amount_tax - move.ai_document_tax),
                TOLERANCE_ABS,
                precision_rounding=rounding,
            ) > 0
            move.ai_tax_mismatch = tax_off
            move.ai_total_mismatch = total_off or tax_off

    # ------------------------------------------------------------------
    # "Is this the right contact?" — the vendor's own history, beside the name
    # ------------------------------------------------------------------
    #
    # The requirement: a number on or beside the vendor name saying how many
    # vendor bills this vendor has, so the user can always be sure this is the
    # right contact.
    #
    # 🔴 THE NUMBER THAT MATTERS IS ZERO, and that is why this is not a quiet
    # grey counter. A vendor the import filed the bill against is either one we
    # have bought from before — in which case the count is reassurance and
    # should take no attention at all — or one we have NOT, which is either a
    # genuinely new supplier or the matcher picking the wrong record out of a
    # contact book that holds `NORTHWIND`, `NORTHWIND - Smith reps` and
    # `Northwind - Smith Reps`. The second case is the only one worth a person's
    # time, so 0 is the loud state (an amber line that says it in words) and
    # every other value is plain text. A grey "0" beside a grey "7" is the
    # shape the eye skims, which would make the feature decorative.

    ai_from_import = fields.Boolean(
        string="Came from the AI import",
        compute="_compute_ai_vendor_bill_count",
        help="This bill was created by the AI PDF import, so its vendor was "
        "chosen by a matcher rather than typed by a person.",
    )
    ai_vendor_bill_count = fields.Integer(
        string="Previous bills from this vendor",
        compute="_compute_ai_vendor_bill_count",
        help="POSTED vendor bills and refunds this company has already had "
        "from this vendor, not counting this one.",
    )
    ai_vendor_draft_bill_count = fields.Integer(
        string="Draft bills from this vendor",
        compute="_compute_ai_vendor_bill_count",
        help="DRAFT vendor bills and refunds this company already has from "
        "this vendor, not counting this one. Reported beside the posted number "
        "and never added into it — a draft is not evidence that this vendor "
        "has been paid before.",
    )
    ai_can_create_vendor = fields.Boolean(
        string="Can create the vendor from the invoice",
        compute="_compute_ai_can_create_vendor",
        help="There is a readable extraction on this bill carrying a seller "
        "name, so a contact could be created from it.",
    )

    @api.depends("partner_id", "state")
    def _compute_ai_can_create_vendor(self):
        """Is there anything to create a contact FROM?

        🔴 IT ASKS THE EXTRACTION, NOT THE BILL. The button's offer has to be
        false when it would fail, and it fails on a bill whose stored extraction
        has no seller name — `action_ai_create_vendor_from_extraction` raises
        exactly there. Offering a button that cannot work is worse than offering
        none: the person presses it, gets a dialog, and learns to distrust the
        screen.

        ⚠️ `_origin` and the unsaved case. A bill being created in a form has no
        id, so it has no extraction and no import record either — the answer is
        False and no query is needed. Not stored, for the same reason as
        `ai_vendor_bill_count`: it is read while one form is open.
        """
        for move in self:
            if move.partner_id or move.state != "draft" or not move._origin.id:
                move.ai_can_create_vendor = False
                continue
            seller = (move._origin._ai_stored_extraction() or {}).get("seller")
            move.ai_can_create_vendor = bool(
                isinstance(seller, dict) and (seller.get("name") or "").strip()
            )

    @api.depends("ai_vendor_suggestion_ids.state")
    def _compute_ai_vendor_suggestion_count(self):
        for move in self:
            move.ai_vendor_suggestion_count = len(
                move.ai_vendor_suggestion_ids.filtered(
                    lambda row: row.state == "pending"
                )
            )

    @api.depends("commercial_partner_id", "company_id")
    def _compute_ai_vendor_bill_count(self):
        """Both fields in one pass, and NOT stored.

        ⚠️ `store=True` is wrong here and would be expensive in the one place
        that matters. The number changes whenever ANY bill for that vendor is
        posted or reset, so a stored field would mean a tree-wide recompute
        chasing `account.move.state` — on a table holding this database's whole
        purchase history — to keep a hint fresh that is only ever read while one
        form is open. Two `_read_group`s per form open is the cheaper half by a
        wide margin.

        🔴 THE COUNT ITSELF IS NOT COMPUTED HERE. It comes from
        `_ai_posted_bill_counts`, the same helper the candidate cards use, so
        the cards and this line cannot disagree — see that method for why that
        matters on this particular screen.

        `_origin` throughout, as core's own `_compute_duplicated_ref_ids` does
        (`account/models/account_move.py:2065-2070`): a record being edited in a
        form is a `NewId`, and a `NewId` in an SQL domain is not a filter, it is
        a crash.
        """
        saved = self._origin.filtered("id")
        imported_ids = set()
        if saved:
            # ONE query for the whole recordset. `ai.invoice.import` is the
            # record the import writes `move_id` onto (`_finalize_import`), and
            # asking whether such a row exists is the cheapest honest test of
            # "this bill came from the AI import" — see the field's view note
            # for why the question is asked at all.
            imported_ids = {
                move.id
                for move, in self.env["goodahead.invoice.import"]
                .sudo()
                ._read_group(
                    [("move_id", "in", saved.ids)], groupby=["move_id"]
                )
            }

        counted = self.filtered(
            lambda move: move._origin.id in imported_ids
            and move.move_type in ("in_invoice", "in_refund")
            and move.commercial_partner_id
        )

        # 🔴 MULTI-COMPANY IS NOT COSMETIC IN THIS DATABASE. Alpha LLC and
        # Beta Inc. buy from each other, so a vendor can have a long
        # history in one company and none at all in the other: "how many bills
        # does this vendor have" has no answer until you say WHOSE bills.
        # Counting across every company the reader happens to have ticked would
        # show a reassuring 7 on a Beta bill for a vendor Beta has never bought
        # from, which is precisely the wrong answer to the question asked. So
        # the key is (company, vendor) and the query is run once per company.
        totals = {}
        for company in counted.company_id:
            group = counted.filtered(lambda move: move.company_id == company)
            for partner, states in self._ai_bill_counts(
                group.commercial_partner_id, company=company
            ).items():
                totals[(company, partner)] = states

        for move in self:
            move.ai_from_import = move._origin.id in imported_ids
            if move not in counted:
                move.ai_vendor_bill_count = 0
                move.ai_vendor_draft_bill_count = 0
                continue
            states = totals.get((move.company_id, move.commercial_partner_id), {})
            # ⚠️ EXCLUDING THIS BILL IS A SUBTRACTION, NOT A DOMAIN. Doing it in
            # the query would mean one query per record — and doing it once for
            # a whole group would make two bills for the same vendor each
            # exclude the other.
            #
            # 🔴 AND THE SUBTRACTION IS PER STATE, WHICH IS THE WHOLE REASON THE
            # DRAFT NUMBER NEEDED THINKING ABOUT RATHER THAN COPYING. The old
            # comment here said "a draft bill is not in `total` to begin with,
            # which is why the test is on the posted state" — true while only
            # posted rows were counted, and false the moment drafts are. A bill
            # being looked at is almost always a DRAFT and is now in its own
            # total, so without this it would report itself: bill
            # D would read "1 draft bill from this vendor" meaning itself.
            own_state = move._origin.state
            move.ai_vendor_bill_count = max(
                states.get("posted", 0) - (1 if own_state == "posted" else 0), 0
            )
            move.ai_vendor_draft_bill_count = max(
                states.get("draft", 0) - (1 if own_state == "draft" else 0), 0
            )

    @api.model
    def _ai_posted_bill_counts(self, partners, company=None):
        """``{partner: how many POSTED bills/refunds that vendor has}``.

        🔴 ONE DEFINITION, TWO SCREENS. This is called by
        `_ai_vendor_candidates` (the number on each candidate card) and by
        `_compute_ai_vendor_bill_count` (the number beside the Vendor field once
        the bill HAS one). They are the same question, asked a few pixels apart,
        of the same vendor — a person picks a card reading "7 bills" and must
        then see 7 on the line that replaces it. Two `search_count`s with
        slightly different domains would eventually differ, and the screen would
        be arguing with itself with no way to tell which half was right.

        🔴 COUNTED, NOT PROXIED. `supplier_rank` is the obvious shortcut and is
        not bills: it is incremented by other things and never decremented. The
        question asked is whether the candidates already have earlier bills,
        so the number has to be bills or it must not be called bills.

        ⚠️ IT COUNTS EVERYTHING, INCLUDING THE CALLER'S OWN BILL. "Not this
        one" is the caller's business, because the two callers mean different
        things by it — a candidate card has no bill of its own to drop, and the
        form line has exactly one — and a shared `exclude` that took a whole
        recordset would make two bills for the same vendor each exclude the
        other. What is shared is the DEFINITION of a bill, which is the part
        that must not drift.

        `company` defaults to `self.env.company` rather than to "no filter":
        there is no such thing as a company-less count here, and a default of
        "everything the reader can see" is the failure mode described in
        `_compute_ai_vendor_bill_count`.
        """
        return {
            partner: states.get("posted", 0)
            for partner, states in self._ai_bill_counts(
                partners, company=company
            ).items()
        }

    @api.model
    def _ai_draft_bill_counts(self, partners, company=None):
        """``{partner: how many DRAFT bills/refunds that vendor has}``.

        🔴 WHY THIS EXISTS. A vendor can have **no posted and several draft**
        bills, and the screen then said *"No posted bills from this vendor yet —
        check this is the right contact."* Every word of that is true and the
        conclusion a reader draws from it is wrong.

        ⚠️ A DRAFT IS NOT A POSTED BILL AND IS NOT COUNTED AS ONE. The posted
        number stays exactly what it was — it is the one that means *this vendor
        has been paid before* — and the draft number is reported BESIDE it.
        Adding drafts into `bill_count` would have made the amber zero-state
        disappear on precisely the bills that produced it, which is the opposite
        of the feature.
        """
        return {
            partner: states.get("draft", 0)
            for partner, states in self._ai_bill_counts(
                partners, company=company
            ).items()
        }

    @api.model
    def _ai_bill_counts(self, partners, company=None):
        """``{partner: {'posted': n, 'draft': n}}`` — ONE query, both states.

        🔴 THE COUNTING IS NOT FORKED, AND THAT WAS A CONSTRAINT RATHER THAN A
        PREFERENCE. `_ai_posted_bill_counts` and `_ai_draft_bill_counts` are
        both thin projections of this method, so there is still exactly one
        definition of "a bill from this vendor" — `move_type`, company and
        `commercial_partner_id` — and the two numbers on the screen cannot
        drift into disagreeing about which rows they are counting. The only
        difference between them is which `state` bucket they read.

        `_read_group` groups on BOTH keys, so the extra number costs no extra
        query. Rows in any other state (`cancel`) are excluded by the domain and
        therefore simply absent from the inner dict, which is why both
        projections use `.get(..., 0)`.
        """
        if not partners:
            return {}
        company = company or self.env.company
        domain = [
            ("move_type", "in", ("in_invoice", "in_refund")),
            ("state", "in", ("posted", "draft")),
            ("company_id", "=", company.id),
            ("commercial_partner_id", "in", partners.ids),
        ]
        counts = {}
        for partner, state, count in self.env["account.move"]._read_group(
            domain,
            groupby=["commercial_partner_id", "state"],
            aggregates=["__count"],
        ):
            counts.setdefault(partner, {})[state] = count
        return {partner: counts.get(partner, {}) for partner in partners}

    def action_ai_review_vendor_suggestions(self):
        """Open this bill's pending suggestions in the review dialog.

        `target: new` on purpose — the person is in the middle of a bill, and
        navigating away from a half-finished document to a list is how the half
        that is left gets forgotten. (`_open_for` sets that; it is said here
        because it is the reason the button exists in this shape.)

        🔴 IT USED TO OPEN THE QUEUE'S OWN LIST VIEW IN THE DIALOG, and that
        was not readable for a human. Nine columns in a modal, the vendor repeated on every row of a
        one-vendor list, and the two columns being compared squeezed to
        `ar@northwind….` and `(212) 555-0…`. The dialog is now a wizard —
        `goodahead.vendor.data.review` — which holds no rules of its own and
        applies through each suggestion's existing `action_apply`.
        """
        self.ensure_one()
        return self.env["goodahead.vendor.data.review"]._open_for(
            self.ai_vendor_suggestion_ids, move=self
        )

    def action_ai_defer_vendor_suggestions(self):
        """“Not now”. 🔴 THE ROWS STAY PENDING — that is the whole mechanism.
        They are not dismissed, they are handed on: the queue is filtered on
        `state = pending`, so deferring here is what puts them in front of
        whoever owns vendor data instead of in front of whoever happens to be
        entering a bill."""
        self.ai_vendor_review_deferred = True
        return True

    @api.model
    def _ai_as_number(self, value):
        """A figure from the stored JSON as a float, or `None`.

        ⚠️ THE SAME COERCION THE UBL GENERATOR APPLIES. `action_reimport_from_json`
        does not re-validate, so a hand-edited "14.77" (a string) reaches here,
        and `ubl_generator._amount_of` turns it into 14.77 on the bill. Dropping
        it here instead would store a tax of 0.00 against a bill that carries
        the tax, and show a false red banner. `bool` is refused: `True` is an
        `int` in Python and is not an amount.
        """
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @api.model
    def _ai_printed_tax(self, extracted):
        """The tax the invoice prints, as a magnitude, or `None` if it states none.

        The subtotals when there are any, else the lines' own tax — the same
        source `claude_client._validate_arithmetic` reconciles against. A null
        line tax adds nothing: that is the receipt that prints one total only,
        and its subtotal carries the figure.

        ⚠️ THE SIGN COMES FROM `payable`. A credit note arrives in either
        convention (see `ubl_generator._normalise_credit_note_signs`), and the
        bill holds magnitudes on both an `in_invoice` and an `in_refund`.
        """
        subtotals = [s for s in extracted.get("tax_subtotals") or [] if isinstance(s, dict)]
        if subtotals:
            amounts = [s.get("tax_amount") for s in subtotals]
        else:
            amounts = [
                line.get("tax_amount")
                for line in extracted.get("lines") or []
                if isinstance(line, dict)
            ]
        amounts = [self._ai_as_number(a) for a in amounts]
        amounts = [a for a in amounts if a is not None]
        if not amounts:
            return None
        payable = self._ai_as_number((extracted.get("totals") or {}).get("payable"))
        sign = -1.0 if payable is not None and payable < 0 else 1.0
        return sign * sum(amounts)

    def _ai_record_printed_totals(self, invoice, extracted):
        """Remember what the invoice prints. Add nothing to the bill.

        🔴 NO LINE IS ADDED, AND NONE MUST BE (19.0.3.31.0). 19.0.3.30.0 booked
        the missing tax as an extra "Sales tax (as printed on the invoice)"
        line so the totals agreed. The requirement since: no extra lines, the
        PDF's lines only, and any discrepancy shown as a banner for a person to
        review. The bill carries exactly the document's lines; the
        two figures stored here drive the red banner (`ai_total_mismatch`).

        Nothing is stored when the payable is missing or not a number, so a
        hand-damaged extraction never produces a banner claiming "$0.00".
        """
        if invoice.state != "draft":
            return
        payable = self._ai_as_number((extracted.get("totals") or {}).get("payable"))
        if payable is None:
            return
        printed_tax = self._ai_printed_tax(extracted)
        invoice.write(
            {
                "ai_document_total": abs(payable),
                "ai_document_tax": printed_tax or 0.0,
                "ai_check_total": True,
            }
        )

    # ------------------------------------------------------------------
    # Claude extraction → UBL → Odoo import (shared by upload & email)
    # ------------------------------------------------------------------

    @api.model
    def _ai_currency_for_country(self, country_code):
        """The ISO name of a country's own currency, or None.

        `res.country.currency_id` is a real Many2one on core
        (`odoo/addons/base/models/res_country.py:60`, beside `code` at :40) and
        it is POPULATED: in core's own country data every `res.country` row
        carries one — US→USD, FI→EUR, CN→CNY. So the data that was assumed to
        exist does exist; the only question was whether we are allowed to use
        it, which `_resolve_currency_from_country` answers.

        🔴 IT ANSWERS FOR AN ARCHIVED CURRENCY TOO, and 19.0.3.3.0 did not.
        What currency a country uses is a fact about the COUNTRY; whether this
        database trades in it is a fact about `res.currency.active`, and
        `_ai_currency_is_active` is where that one is asked. Collapsing the two
        into a single `None` cost both halves at once — see that method, and
        the archived block in `_resolve_currency_from_country` for the reason
        the write still declines.
        """
        code = str(country_code or "").strip().upper()
        if len(code) != 2:
            return None
        country = self.env["res.country"].search([("code", "=", code)], limit=1)
        currency = country.currency_id
        if not currency:
            return None
        return currency.name

    @api.model
    def _ai_surviving_currency_marker(self, extracted):
        """The marker, re-validated against the extraction it arrived with —
        and REMOVED from it when it no longer holds.

        The marker is a CLAIM, and the chatter note makes three statements out
        of it: nothing was printed on the page, both parties are in one named
        country, and the currency on the bill is that country's. Each is a
        question `_resolve_currency_from_country` asked once, at extraction
        time. On a re-import nobody asks them again, and the JSON in between
        has been open in a text editor.

        So they are asked here, against the data as it is NOW:

        1. The page still printed nothing. Since 19.0.3.18.0 that is the PDF's
           own text layer, recorded under `currency_marking` at extraction time
           and re-read from the stored JSON here, with the model's
           `currency_symbol` as the fallback for a document that has no text
           layer. Otherwise the note's opening clause is contradicted by the
           document under it.
           ⚠️ A hand-edit of `currency_symbol` therefore no longer moves this
           answer on a PDF with a text layer. What that buys is narrower than
           19.0.3.18.0 claimed, and the claim is corrected here rather than
           left standing: the recorded verdict is a fact about bytes only at
           the moment it is RECORDED. By the time it arrives back through
           `action_reimport_from_json` it is a line in a JSON file a user
           has had open in a text editor, and
           `test_the_marker_is_re_validated_against_the_stored_verdict` edits
           exactly that line to prove the marker drops. Edited the other way
           it would make the note appear. That is the same latitude a
           hand-edited `currency` has always had — it is deliberate, question
           3 below is what checks it, and it is not immutability.
           🆕 19.0.3.20.0: when the stored JSON carries no USABLE verdict —
           absent (every extraction from 19.0.3.3.0 to 19.0.3.17.0), `{}`, or
           `unknown` because the attachment could not be read at the time —
           `ai.invoice.import.action_reimport_from_json` computes one from the
           PDF still on the record, through `_ai_reask_the_pdf`, before the UBL
           is built and therefore before this is called. The back catalogue is
           re-decided from bytes rather than from the model.
           ⚠️ IT IS NOT `_finalize_import`, WHICH IS WHERE THIS PARAGRAPH SENT
           PEOPLE UNTIL 19.0.3.21.0 (found in review). 19.0.3.19.0 did
           put the recompute there; 19.0.3.20.0 moved it out and said so in a
           comment at that site, because the resolver has to run before the XML
           is generated and `_finalize_import` runs after. A docstring naming
           the method that deliberately no longer does this sends a maintainer
           looking for the heal in the one place it is documented as absent.
        2. Both countries still read the same, and still read as the marker.
        3. That country's currency is still the one on the extraction. This is
           the question the hand-edit breaks, and the one the resolver calls
           the single check that makes marking a value we did not produce safe.

        🔴 THE KEY IS DROPPED, NOT JUST IGNORED. `extracted` is what gets
        written to `extraction_data` and to the `extraction.json` attachment a
        few lines below, so a claim that no longer holds must not survive into
        them — otherwise the NEXT re-import inherits it and the question has to
        be asked a third time.

        ⚠️ An archived currency is deliberately NOT asked about. Core imports
        one (`active_test=False`, see `_ai_currency_is_active`), so the bill
        carries it either way and suppressing the note would remove the only
        visibility for no gain.
        """
        marker = extracted.get(CURRENCY_INFERRED_KEY)
        if not marker:
            return None

        marker = str(marker).strip().upper()
        currency = str(extracted.get("currency") or "").strip().upper()
        holds = (
            nothing_was_printed(extracted)
            and one_country_on_both_sides(extracted) == marker
            and currency
            and self._ai_currency_for_country(marker) == currency
        )
        if holds:
            return marker

        _logger.info(
            "AI invoice import: the stored extraction is marked as inferred "
            "from %s but that no longer holds (currency %r, text-layer verdict "
            "%r, model currency_symbol %r) — the marker is dropped and no note "
            "is posted",
            marker,
            extracted.get("currency"),
            (extracted.get(CURRENCY_MARKING_KEY) or {}).get("text_layer")
            if isinstance(extracted.get(CURRENCY_MARKING_KEY), dict)
            else None,
            extracted.get(CURRENCY_SYMBOL_KEY),
        )
        extracted.pop(CURRENCY_INFERRED_KEY, None)
        return None

    @api.model
    def _ai_currency_is_active(self, currency_name):
        """Does this company trade in that currency?

        🔴 THIS IS NOT WHAT 19.0.3.3.0 SAID IT WAS, AND THE CLAIM IT RESTED ON
        WAS FALSE. That version asserted core's `_import_currency` matches
        `res.currency.name` through an ordinary `search`, excluding inactive
        rows, and then falls back to the company currency. It does not. At the
        pinned revision `189df353`, `account_edi_common.py:744-758` searches
        `with_context(active_test=False)`, FINDS the archived row, uses it, and
        only appends *"The currency '%s' is not active."* to the import log.
        The company-currency substitution happens only when no row of that name
        EXISTS — which is the case this predicate also catches, by answering no
        for a name nothing matches.

        So core accepts an archived currency, and the reason to decline one is
        ours rather than core's: `active = False` is an administrator saying
        the company does not trade in it, there is no maintained rate, and an
        unmaintained rate is 1.0. Only the WRITE path asks this. A currency
        already on the extraction is imported whatever we think, so the chatter
        note is not suppressed for it.

        ⚠️ `active_test=False` is essential here, or an archived currency would
        read as "no such currency" and the two cases would be one again.
        """
        name = str(currency_name or "").strip()
        if not name:
            return False
        currency = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "=", name)], limit=1)
        )
        return bool(currency) and currency.active

    @api.model
    def _ai_generate_ubl_from_extracted(self, extracted, company):
        """Generate Peppol UBL XML from an already-extracted invoice dict.

        No Claude call — used both by _ai_extract_and_generate_ubl and by the
        re-import-from-JSON recovery action (so a hand-fixed extraction can be
        re-imported without paying for another extraction).
        """
        self._ai_drop_invalid_bank_numbers(extracted)
        # the dual-core design: the currency fallback is the COMPANY's own, not the
        # literal "USD" that used to be compiled into the generator. It only
        # fires on a re-import of a stored extraction that states no currency
        # — `_validate_extraction` refuses one at extraction time — but that
        # is exactly where a Finnish installation used to get a dollar bill.
        return generate_peppol_invoice_xml(
            extracted, default_currency=company.currency_id.name
        )

    @api.model
    def _ai_drop_invalid_bank_numbers(self, extracted):
        """Never turn a misread bank number into a bank account.

        An extraction can read a vendor's IBAN with a digit too many, and do it
        the same way on every copy. A number like that becomes a vendor bank
        account, and on the next bill it is the account we pay into — one that
        does not exist. Both an IBAN and a US ABA routing number carry their own
        check digits, so a misread one is caught here, before the UBL is built
        and before anything downstream can record it.

        A rejected number moves to ``seller["<field>_rejected"]``: kept in the
        stored extraction so it can be compared with the PDF, and announced on
        the bill by ``_ai_post_process``. Mutates ``extracted`` in place.

        🔴 A US ACCOUNT NUMBER IS NOT CHECKED, AND CANNOT BE. The ABA checksum
        covers the nine-digit routing number, which identifies the BANK. The
        account number itself has no checksum — nothing distinguishes
        ``1234567890`` from ``1234567891``. So on a US bill this guard is
        meaningfully weaker than on a European one, and that is a property of
        the data rather than something more code would fix. The routing number
        being wrong is caught; the account number being wrong is not. Said out
        loud so nobody reads a green import as "the account was verified".
        """
        seller = extracted.get("seller")
        if not isinstance(seller, dict):
            return

        # 🔴 EVERY CHECK RUNS OVER EVERY FIELD. Do NOT reduce this to
        # "mod-97 on `iban`, ABA on `routing_number`" — that dispatches on the
        # field the MODEL chose, and the model choosing wrongly is the whole
        # reason these guards exist. A misread IBAN that Claude puts in
        # `account_number` would never be checksummed, and would be recorded as
        # a `res.partner.bank` on the vendor.
        #
        # ⚠️ There is no second line of defence here: `base_iban` is NOT
        # installed on this database, so Odoo itself does not validate an IBAN
        # written into a bank account either.
        #
        # Running both over all three costs nothing, because both are
        # SHAPE-gated: `iban_checksum_fails` declines anything that is not
        # IBAN-shaped and `aba_checksum_fails` declines anything that is not
        # nine digits. `test_bank_numbers.py` pins that inertness in both
        # directions, which is what makes this safe rather than merely tidy.
        for field in ("iban", "account_number", "routing_number"):
            value = seller.get(field)
            if not value:
                continue
            if iban_checksum_fails(value) or aba_checksum_fails(value):
                seller[f"{field}_rejected"] = value
                seller[field] = None

        # A routing number that was rejected leaves an account number that
        # cannot be paid into — US ACH needs both. Drop it together, so the bill
        # states no account at all rather than half of one, which would look
        # usable and is not.
        if seller.get("routing_number_rejected") and seller.get("account_number"):
            seller["account_number_rejected"] = seller["account_number"]
            seller["account_number"] = None

    @api.model
    def _ai_decode_ubl_into_move(self, move, xml_bytes, extracted, attachment=None,
                                 new=True):
        """Run Odoo's native UBL decoder directly on ``move``. Returns an error
        string, or None on success.

        This is the ONE bill-building path for both entrypoints. It deliberately
        avoids journal._create_document_from_attachment, whose internal commit's
        flush hits an empty-user access check in the queue-job context (leaving
        empty bills). Running the decoder directly on a move has no internal
        commit.

        ``attachment`` is the generated-XML ir.attachment; the native decoder
        reads ``file_data['attachment']`` directly (and, for a purchase document,
        stores it as the bill's ``ubl_cii_xml_file``), so the key must always be
        present — an empty recordset is fine.

        The ai_seller_* context must be set on ``self`` BEFORE _get_edi_decoder,
        because the decoder is resolved off that env and everything downstream
        inherits it — including `res.partner._import_retrieve_customer_from_ai_business_id`,
        the search-plan step that matches a vendor by business id and country.

        ⚠️ On this tree's core that step is reached through
        `account.edi.ubl._import_ubl_retrieve_customer_search_plan`, NOT through
        `res.partner._retrieve_partner` as on the source project's older core.
        The context relay is the same; what reads it moved. See
        models/account_edi_ubl.py.
        """
        # Before anything is decoded onto `move`. On a core this module has not
        # been measured against, the hooks below are silent no-ops and core's
        # own auto-create would mint a vendor from a PDF — so refuse rather than
        # degrade. Inert on both measured builds; see models/core_profile.py.
        self._ai_assert_core_profile()

        try:
            xml_tree = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError as e:
            return f"Generated UBL XML is malformed: {e}"

        ubl_file_data = {
            "name": "invoice.xml",
            "raw": xml_bytes,
            "xml_tree": xml_tree,
            "type": "xml",
            "filename": "invoice.xml",
            "mimetype": "application/xml",
            # The decoder accesses these by key (not .get) — always provide them.
            "attachment": attachment or self.env["ir.attachment"],
        }

        seller = extracted.get("seller")
        seller = seller if isinstance(seller, dict) else {}
        self_ctx = self.with_context(
            ai_seller_country_code=(seller.get("country_code") or "").strip() or None,
            ai_seller_business_id=(seller.get("business_id") or "").strip() or None,
            # The website has no UBL element core decodes, so unlike the phone
            # and the email — which now ride in `cac:Contact` and are read by
            # core at `account_edi_ubl.py:2654,2659` — it can only reach the
            # plan step this way. See
            # `res.partner._import_retrieve_customer_from_ai_domain`.
            ai_seller_website=(seller.get("website") or None)
            if isinstance(seller.get("website"), str)
            else None,
            # 🆕 19.0.3.5.0 — the WHOLE seller dict, beside the three single
            # keys above and not instead of them. An adapter on EITHER 19.0
            # import pipeline can rebuild the extraction from this one key and
            # hand it to `res.partner._ai_find_vendor` without reading anything
            # core decoded, which is what makes one matcher serve both. The
            # three single keys stay because the search-plan steps read them,
            # and because they are what tells those steps apart from a genuine
            # Peppol document, which arrives with none of this set.
            ai_seller=seller,
            # Marks the decode as OURS. `account.edi.ubl` is a shared abstract
            # model, and `_import_ubl_create_missing_customer` is suppressed
            # only for an AI extraction — not for a genuine Peppol document.
            ai_invoice_import=True,
        )
        # Odoo 18.0 contract: the decoder is returned as a callable, and it
        # returns True when it imported the document.
        decoder = super(AccountMove, self_ctx)._get_edi_decoder(ubl_file_data, new)
        if not decoder:
            return "No UBL decoder found for the generated XML"
        if not decoder(move, ubl_file_data, new):
            return "Odoo's UBL importer could not read the generated XML"
        return None

    @api.model
    def _ai_post_process(self, invoice, extracted, usage, model_name, company,
                         source_label=None):
        """Post-process an imported invoice: partner, categories, chatter.

        Called after the native UBL decoder has populated the invoice.

        ``source_label`` (core/source split) names a non-AI source for the
        chatter; ``usage`` and ``model_name`` are the AI reading's, None for a
        structured source. Which note is posted is `_import_origin_note`'s
        business, so the wording about an AI reading lives in the AI add-on.
        """
        # Match a partner if the native import didn't. 🔴 MATCH, never create —
        # see `_create_partner_from_extraction`.
        if not invoice.partner_id:
            self._create_partner_from_extraction(invoice, extracted)

        # 🔴 THE SAFE HALF HAPPENS BY ITSELF. With a vendor resolved, every
        # EMPTY field on that vendor is filled from the invoice and logged, the
        # invoice's phone and web domain become identifier rows so the NEXT bill
        # matches without anybody, and every CONTRADICTION becomes a pending
        # suggestion instead of a write. Nothing here can overwrite anything.
        if invoice.partner_id:
            invoice._ai_apply_vendor_data(
                invoice.commercial_partner_id, extracted, automatic=True
            )

        # Apply expense category accounts
        self._apply_expense_categories(invoice, extracted, company)

        # Whatever the decoder made of the document's bank details, the account
        # on the bill has to be the payee's.
        self._ai_enforce_document_bank_side(invoice, extracted)

        # ...and it has to be PAYABLE, which for a US vendor needs the routing
        # number the decoder throws away.
        self._ai_record_routing_number(invoice, extracted)

        seller = extracted.get("seller") or {}
        for field, label in (
            ("iban", "IBAN"),
            ("routing_number", "routing number"),
            ("account_number", "account number"),
        ):
            rejected = seller.get(f"{field}_rejected")
            if not rejected:
                continue
            if field == "account_number":
                # Not rejected on its own account — it has no checksum. It went
                # with the routing number, because half a US account cannot be
                # paid into and a half-stated one looks usable.
                body = Markup(
                    "<p>The account number read from the document (%s) was not "
                    "used either: it came with a routing number that failed its "
                    "checksum, and a US account needs both.</p>"
                ) % rejected
            else:
                body = Markup(
                    "<p>The %s read from the document (%s) fails its checksum, "
                    "so it was not used. Check it against the PDF and add the "
                    "vendor's account by hand if needed.</p>"
                ) % (label, rejected)
            invoice.message_post(body=body)

        # 🔴 THE CURRENCY WAS NOT PRINTED ON THE DOCUMENT — SAY SO ON THE BILL.
        # 19.0.2.3.0's principle is that a wrong currency is invisible and
        # nobody ever sees it, which is why a null one became a terminal
        # failure. 19.0.3.3.0 resolves one narrow case of it (a domestic bill
        # with bare amounts) instead of failing, so the visibility that the
        # failure used to provide has to come from somewhere: this note is it.
        # It is posted ONLY when the inference actually fired — the key is set
        # by `_resolve_currency_from_country` and by nothing else — so an
        # ordinary import's chatter is unchanged.
        #
        # 🔴 THE SENTENCE MAY NOT SAY WHY, and that is a decision, not style.
        # The marker now also fires when the MODEL did the inference (prompt
        # CURRENCY step 4), and that set includes a document whose currency
        # actually came from an ABA routing number or an IBAN — CURRENCY step
        # 3 — on which the address is NOT the reason. Nothing in the extraction
        # separates those, so the note states only what `currency_symbol: null`
        # proves: nothing was printed, so the value was deduced rather than
        # read. The countries are given as corroboration a human can check
        # against the PDF, not as the cause. The wording it replaced —
        # "because the vendor's address and ours are both there" — was a claim
        # the document does not always support.
        #
        # 🔴 AND THE CLAIM IS RE-ASKED HERE, NOT TRUSTED.
        # `_resolve_currency_from_country` asks three questions and sets the
        # key only if all three answer yes — but it runs inside
        # `_validate_extraction`, and `action_reimport_from_json` never goes
        # there. It generates UBL from the stored JSON and arrives straight
        # here. So on a re-import the marker is whatever the JSON says, and
        # `currency` is read fresh from the same JSON with nothing keeping the
        # two in step.
        #
        # They come apart on an ORDINARY failure. The resolver runs FIRST in
        # `_validate_extraction`, before the required-field sweep, the shape
        # checks and `_validate_arithmetic` — every one of which raises with
        # `data=data`, which `ai.invoice.import` persists verbatim. The marked
        # JSON therefore reaches `extraction_data` on a FAILED import, and that
        # stored JSON is exactly what a user then edits by hand — having to edit
        # the JSON to correct the currency is how this feature was born.
        # The user fixes the total, sees on the PDF that the bill is in CAD, corrects
        # `currency`, and leaves alone a key he has never heard of. The note
        # then reads *"it was deduced as CAD … both in United States, whose
        # currency is CAD"* — a hand-typed value presented as a machine
        # deduction, and USD is the currency of the United States. Both halves
        # false, in the direction that looks like success.
        inferred_from = self._ai_surviving_currency_marker(extracted)
        if inferred_from:
            country = self.env["res.country"].search(
                [("code", "=", str(inferred_from).strip().upper())], limit=1
            )
            # 🔴 THE NOTE NAMES ITS OWN SOURCE (19.0.3.19.0, review/§5).
            # It used to open *"No currency symbol or ISO code is printed
            # anywhere on this document"* as a flat absolute, and that sentence
            # is only ever as good as the instrument behind it: on a PDF with
            # no readable text layer it is the model's word, and the model has
            # been seen to flip on identical bytes. Worse, the one marking this
            # file knowingly declines to match — a bare ISO code against a
            # round amount — makes the absolute false on a page that prints the
            # code in plain sight. "Was found" is what we can actually say, and
            # the second sentence says who looked.
            verdict = extracted.get(CURRENCY_MARKING_KEY)
            read_the_bytes = (
                isinstance(verdict, dict)
                and verdict.get("decided_by") == "text_layer"
            )
            source = (
                _(
                    "That is this document's own text layer, annotations and "
                    "form fields included."
                )
                if read_the_bytes
                else _(
                    "This document has no readable text layer, so that is the "
                    "extraction's own reading rather than a check of the bytes."
                )
            )
            invoice.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "No currency symbol or ISO code was found anywhere on this "
                    "document, so the currency was not read off the page — it "
                    "was deduced as %(currency)s. The vendor's address and ours "
                    "are both in %(country)s, whose currency is %(currency)s. "
                    "%(evidence)s Check it against the PDF before posting.",
                    currency=extracted.get("currency") or "",
                    country=country.display_name or inferred_from,
                    # Not `source`: that is the name of `_()`'s own first
                    # parameter in 18.0.
                    evidence=source,
                )
            )

        # The printed total and tax, for the red banner. Nothing is added to
        # the bill. See `_ai_record_printed_totals`.
        self._ai_record_printed_totals(invoice, extracted)

        # Save extraction JSON as attachment
        extraction_json = json.dumps(extracted, indent=2, ensure_ascii=False)
        json_attachment = self.env["ir.attachment"].create(
            {
                "name": "extraction.json",
                "raw": extraction_json.encode("utf-8"),
                "mimetype": "application/json",
                "res_model": "account.move",
                "res_id": invoice.id,
            }
        )

        # The check remittance address, when the vendor prints one that is not
        # their main address. It deliberately does NOT overwrite the partner:
        # "Remit To" is where a cheque goes, not where the company is, and
        # silently moving a vendor's address to their lockbox would be wrong on
        # every document that is not a payment. It is surfaced here so whoever
        # pays the bill sees it, which is the only thing it is for.
        remit_to = (seller.get("remit_to_address") or "").strip()
        if remit_to:
            invoice.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "The vendor asks for payment to be remitted to: %(address)s",
                    address=remit_to,
                )
            )

        # The note saying where the bill came from, with the extraction
        # attached. The AI add-on overrides it to record the model and tokens.
        self._import_origin_note(
            invoice, json_attachment, usage, model_name, source_label=source_label
        )

        # 🔴 THE IMPORT DOES NOT GRANT APPROVAL STEP 1, AND MUST NOT BE MADE TO AGAIN.
        #
        # It used to. The original design said the import module would trigger step 1 via a
        # hook, and that was built as "the import CLEARS step 1", so an
        # imported bill arrived already reviewed — by OdooBot — and went straight to the finance
        # director with no human having compared it against the PDF.
        #
        # ⚠️ THAT CONTRADICTED THE REASON AN APPROVAL ADD-ON THAT HOOKS INTO POSTING EXISTS:
        # an AI or OCR import makes the review gap larger, not smaller, because a bill can now
        # exist without anyone having read a line of it.
        # Step 1 is precisely the check that the vendor, the dates, the lines and the accounts
        # match the document — which is the check an extraction can get wrong, and the only one
        # that would catch it. Clearing it automatically removed the control exactly where the
        # risk is highest, and left a single approval on a document nobody had read.
        #
        # It was ruled out once an imported bill was seen arriving stamped
        # "Reviewed by OdooBot": an AI-imported bill now lands in the review queue like any other.
        # "Trigger step 1" means PUT IT INTO step 1, not complete it.
        #
        # So there is deliberately no call here. An imported bill is `to_review`, which is the
        # default state of a new bill, which is why this needs no code — only the absence of it,
        # and this note so the absence is not read as an oversight.

    @api.model
    def _import_origin_note(self, invoice, attachment, usage, model_name,
                            source_label=None):
        """Post the note naming where the bill was imported from, with the
        extracted data attached (core/source split).

        🔴 NEUTRAL IN THE CORE: a bill imported from a structured
        e-invoice must not be described as an AI reading and carries no AI
        usage. The AI add-on overrides this for its own reads (no
        ``source_label``) with the model and token counts."""
        # `no_new_invoice`: on 18.0 a message with attachments makes core try
        # to decode them into the bill and post "already contains lines" when
        # it cannot.
        invoice.with_context(no_new_invoice=True).message_post(
            body=Markup("<p>%s</p>")
            % _(
                "Imported from %(origin)s",
                # Not `source`: that is the name of `_()`'s own first
                # parameter in 18.0.
                origin=source_label or _("an invoice document"),
            ),
            attachment_ids=[attachment.id],
        )

    # ------------------------------------------------------------------
    # Post-processing helpers
    # ------------------------------------------------------------------

    @api.model
    def _ai_routing_field(self):
        """The bank-account field that holds a US routing number, or None.

        Odoo 18.0 has no such field in ``base``; ``l10n_us`` adds
        ``aba_routing``.
        """
        fields = self.env["res.partner.bank"]._fields
        return "aba_routing" if "aba_routing" in fields else None

    @api.model
    def _ai_record_routing_number(self, invoice, extracted):
        """Put the US routing number on the bank account, or it is unpayable.

        Core's UBL import collects the account number but never looks at
        ``FinancialInstitutionBranch/ID``, which is where the generator puts
        the routing number. Without this step the bill ends up with a
        `res.partner.bank` holding a bare US account number, which nobody can
        pay, because ACH needs both halves.

        On 18.0 the field is ``aba_routing`` (from ``l10n_us``), which accepts
        one to nine digits. Nothing is overwritten: an account that already
        carries a routing number keeps it, because a human may have corrected
        it. Without ``l10n_us`` there is nowhere to store it, so it is stated
        on the bill instead of being dropped.
        """
        seller = (extracted or {}).get("seller") or {}
        routing = (seller.get("routing_number") or "").strip()
        if not routing:
            return

        account_number = sanitize_account_number(payee_account_number(seller))
        if not account_number:
            return

        field = self._ai_routing_field()
        # `l10n_us` accepts 1 to 9 digits and nothing else. The value is checked
        # here rather than by catching the constraint: in 18.0 a write that fails
        # validation has already put the value in the cache, and it would be
        # flushed at commit anyway.
        digits = re.sub(r"\D", "", routing)
        storable = bool(re.fullmatch(r"\d{1,9}", digits))
        if not field or not storable:
            reason = (
                _("no routing number field exists (install US accounting, l10n_us)")
                if not field
                else _("it is not a number of at most nine digits")
            )
            self._ai_post_once(
                invoice,
                _(
                    "The routing number read from the document is %(routing)s. "
                    "It is not saved on the bank account because %(reason)s.",
                    routing=routing,
                    reason=reason,
                ),
            )
            return

        # The account on the bill, plus any account of the vendor carrying this
        # number — the decoder may have attached one without the bill stating
        # it (a received credit note does exactly that).
        candidates = invoice.partner_bank_id
        vendor = invoice.commercial_partner_id
        if vendor:
            candidates |= self.env["res.partner.bank"].search(
                [
                    ("sanitized_acc_number", "=", account_number),
                    ("partner_id", "child_of", vendor.id),
                ]
            )

        for bank in candidates:
            if bank.sanitized_acc_number != account_number or bank[field]:
                continue
            bank[field] = digits

    @api.model
    def _ai_post_once(self, invoice, text):
        """Post ``text`` on ``invoice`` unless an identical note is already there.

        For notes a re-import or an applied suggestion would otherwise repeat.
        """
        body = Markup("<p>%s</p>") % text
        if any(str(body) == str(message.body) for message in invoice.message_ids):
            return
        invoice.message_post(body=body)

    def _ai_enforce_document_bank_side(self, invoice, extracted=None):
        """Keep ``partner_bank_id`` on the payee's side of the document.

        Which side that is depends on who is owed: the vendor on a bill we pay,
        ourselves on a credit note we are refunded on. Odoo already names it —
        ``bank_partner_id`` is the vendor for an outbound document and the
        company for an inbound one — so the account has to belong to that
        partner, and an account belonging to the other party is an instruction
        to pay the wrong place.

        An import can get this wrong in more than one way (core reads a received
        credit note's account as ours), so the side is checked after
        the fact rather than trusted of every writer in the chain. A wrong one
        is dropped, a clean one put in its place, and the correction posted to
        the chatter: quietly swapping a bank account on an accounting document
        is not something anybody should have to discover later.

        Ownership alone is too weak a test, because the same number can sit on
        both partners — uniqueness is per number AND partner — and that is the
        state this very bug leaves behind: a vendor's IBAN recorded against the
        company. Such a row answers to the right owner and would be picked up
        again by the compute, so an account is rejected when the other party
        holds the same number, and the replacement is chosen from what is left.
        """
        bank = invoice.partner_bank_id
        if not bank:
            return

        expected = invoice.bank_partner_id
        we_are_the_payee = expected == invoice.company_id.partner_id
        counterparty = (
            invoice.commercial_partner_id
            if we_are_the_payee
            else invoice.company_id.partner_id
        )
        # Archived ones count: a vendor account someone archived off our partner
        # is still that vendor's account, and the number is what identifies it.
        theirs = set(
            counterparty.with_context(active_test=False).bank_ids.mapped(
                "sanitized_acc_number"
            )
        )
        if we_are_the_payee:
            # The account printed on a document we received is the sender's,
            # whoever's record happens to carry it — so on a credit note the
            # number in the file is theirs by definition. Taking it from the
            # file rather than from the vendor's record is what makes the check
            # hold when the only copy of that number in the database is the
            # stray row on our own partner, which is the case that started this.
            seller = (extracted or {}).get("seller") or {}
            theirs.add(sanitize_account_number(payee_account_number(seller)))
        theirs.discard(False)

        # 🔴 THE NUMBER TEST APPLIES ONLY WHEN **WE** ARE THE PAYEE, and that
        # asymmetry is the whole point rather than an oversight.
        #
        # When we are the payee (a received credit note, or an invoice we
        # issued), the number printed on the document belongs to the OTHER
        # party, so a row carrying it must be refused even though it hangs off
        # our own partner — that stray row is exactly what an earlier bug left behind, and
        # ownership alone would accept it.
        #
        # When the VENDOR is the payee — an ordinary bill we pay — the number on
        # the document is the vendor's BY DEFINITION. A copy of it also sitting
        # on the company partner is residue of the same bug, and says nothing
        # about the vendor's own row. Applying the number test here stripped a
        # CORRECT vendor account off the bill, found no replacement (every
        # candidate failed the same test), cleared `partner_bank_id`, and told
        # the accountant the account "was not the account of the party being
        # paid" — which is not what happened. Ownership is sufficient on this
        # side: a genuinely wrong account is one belonging to somebody who is
        # not the payee, and the first half of the condition catches that.
        number_is_suspect = we_are_the_payee and bank.sanitized_acc_number in theirs

        if (
            bank.partner_id.commercial_partner_id == expected.commercial_partner_id
            and not number_is_suspect
        ):
            return

        replacement = (
            expected.bank_ids.filtered_domain(
                [
                    *self.env["res.partner.bank"]._check_company_domain(
                        invoice.company_id
                    ),
                    ("active", "=", True),
                ]
            )
            # Same asymmetry as above: only exclude numbers the other party also
            # holds when WE are the payee. On a bill we pay, the vendor's own
            # accounts are the candidates, and excluding the ones that also
            # appear on the company partner is what left this with nothing to
            # choose and cleared the field.
            .filtered(
                lambda b: not we_are_the_payee
                or b.sanitized_acc_number not in theirs
            )
            # The order core's own compute would use: matching currency first,
            # then an account we are allowed to pay out of.
            .sorted(
                lambda b: (
                    bool(b.currency_id) and b.currency_id != invoice.currency_id,
                    not b.allow_out_payment,
                )
            )[:1]
        )

        # The number never reaches the log — an account number in a server log
        # is an account number out of its context. The id is enough to find it.
        _logger.warning(
            "AI import: %s carried bank account id %s of %s; expected an "
            "account of %s that is not also the other party's — replaced with %s",
            invoice.display_name,
            bank.id,
            bank.partner_id.display_name,
            expected.display_name,
            replacement.id or "nothing",
        )
        invoice.partner_bank_id = replacement
        # Say which of the two things actually happened. "Not the account of the
        # party being paid" is true when the row belongs to somebody else; it is
        # NOT true when the row is the payee's but carries a number the other
        # party also holds, and telling an accountant the wrong one sends them
        # looking for a problem that is not there.
        if bank.partner_id.commercial_partner_id != expected.commercial_partner_id:
            reason = Markup(
                "<p>The bank account on this document belonged to %s, not to "
                "the party being paid (%s) — removed and replaced with %s.</p>"
            ) % (
                bank.partner_id.display_name,
                expected.display_name,
                replacement.display_name or "no account",
            )
        else:
            reason = Markup(
                "<p>The bank account on this document is recorded against %s, "
                "the party being paid, but the same number is also held by the "
                "other party — so it cannot be told apart from an account "
                "copied onto the wrong partner by an earlier import. Replaced "
                "with %s; check it against the document.</p>"
            ) % (
                expected.display_name,
                replacement.display_name or "no account",
            )
        invoice.message_post(body=reason)

    @api.model
    def _ai_expense_category_for(self, code, company):
        """The expense category a code resolves to for THIS company.

        A company-specific category wins over a shared one with the same code.
        `_check_code_is_unambiguous` makes that pair impossible to create, so
        this is belt-and-braces — but it is spelled as TWO SEARCHES rather than
        one ordered one.

        🔴 `order="company_id desc"` does NOT express this. PostgreSQL's DESC
        implies NULLS FIRST, so the SHARED row (company_id NULL) sorts to the
        top and wins — the exact opposite of the intent, and a comment claiming
        otherwise is worse than no comment. The same trap is documented in
        `goodahead_claude._newest_model`, where Odoo's `_order` parser also
        refuses a raw `NULLS LAST`.

        🔴 IT IS A METHOD BECAUSE THE TEST ON IT WAS COPYING IT (found in
        review). `test_us_shape.test_a_company_specific_category_beats_a_shared_one`
        wrote the two searches out again and asserted its own result, so it
        could not fail for any change to production — the shape this suite
        already names twice elsewhere. One body, one caller, one test on it.
        """
        Category = self.env["goodahead.expense.category"]
        return Category.search(
            [("code", "=", code), ("company_id", "=", company.id)], limit=1
        ) or Category.search(
            [("code", "=", code), ("company_id", "=", False)], limit=1
        )

    @api.model
    def _apply_expense_categories(self, invoice, extracted_data, company):
        """Apply expense category accounts to invoice lines without products."""
        lines_data = extracted_data.get("lines", [])
        invoice_lines = invoice.invoice_line_ids.filtered(
            lambda l: l.display_type not in ("line_section", "line_note")
        )

        warnings = []
        for idx, (line_data, inv_line) in enumerate(zip(lines_data, invoice_lines)):
            if inv_line.product_id:
                continue

            cat_code = line_data.get("expense_category")
            if not cat_code:
                continue

            category = self._ai_expense_category_for(cat_code, company)

            # 🔴 `with_company`, because `account_id` is `company_dependent`.
            # Without it the account resolves in whatever company the env
            # happens to carry — which for a queue_job is not necessarily the
            # bill's — and the two companies have DIFFERENT CHARTS, so the
            # wrong one is not a near miss, it is an account that does not
            # belong to this company's books.
            account = category.with_company(company).account_id if category else None

            if not account:
                line_name = line_data.get("name", f"Line {idx + 1}")
                # Name the company and distinguish the two failures. "No account
                # configured" used to cover both "this code does not exist" and
                # "it exists but nobody set an account for THIS company", and
                # the second is the one multi-company makes likely.
                if not category:
                    reason = (
                        f"no category with code '{cat_code}' is available to "
                        f"{company.display_name}"
                    )
                else:
                    reason = (
                        f"category '{cat_code}' has no account set for "
                        f"{company.display_name} (it may be set for another "
                        f"company — the account is per company)"
                    )
                warnings.append(
                    f"Line {idx + 1} '{line_name}': {reason} — using journal default"
                )
                continue

            inv_line.account_id = account

        if warnings:
            invoice.message_post(
                body=Markup(
                    "<p><strong>Expense category warnings:</strong></p><ul>%s</ul>"
                ) % Markup("").join(Markup("<li>%s</li>") % w for w in warnings),
            )

    @api.model
    def _ai_match_existing_vendor(self, seller, company=None, policy=None):
        """Find an existing vendor for the extracted seller.

        ⚠️ `company` IS THE BILL'S, AND IT WAS NOT PASSED AT ALL UNTIL
        19.0.3.26.0 (PR #336 review finding 1). Without it `_ai_find_vendor`
        falls back to `self.env.company`, and the import runs in a queue job
        whose `env.company` need not be the company the bill was filed on — so
        the guard protected the wrong company, which refuses a legitimate
        intercompany vendor and lets the real defect through. It stays optional
        because a caller holding only a seller dict is a documented use of this
        method; every caller with a document in hand passes it.

        🔴 THE BODY MOVED TO `res.partner._ai_find_vendor` IN 19.0.3.5.0
        (the dual-core design) AND THIS IS NOW NOTHING BUT THE CALL. The ordered search,
        the measurements behind it and the uniqueness rule are documented
        there; do not restate them here, because two docstrings is how two
        implementations start.

        Why it moved: on the older 19.0 core (`legacy`) nothing ever reaches
        `account.move` during vendor retrieval — there is no search plan, and
        `account.edi.common._import_partner` asks `res.partner` directly. A
        matcher sitting on `account.move` is reachable from this tree's pipeline
        only, so it would install clean and never run there. `res.partner` is
        the one model BOTH pipelines hand the question to.

        Why the name stayed: a dozen tests, the F27 chatter path and
        `_create_partner_from_extraction` call it. Renaming it would be tidier
        and would also be a second change hiding inside a move.

        ``policy`` (core/source split): the source's `MatchingPolicy`, else the one
        on the context, else the default. A non-default policy is answered by
        `res.partner._ai_vendor_decision`; the default one by `_ai_find_vendor`
        as before.
        """
        Partner = self.env["res.partner"]
        policy = Partner._ai_matching_policy(policy)
        if policy.is_legacy:
            # 🔴 EXACTLY THE CALL IT ALWAYS WAS — tests stub `_ai_find_vendor`
            # with its two-argument signature, and the AI source's behaviour
            # must not move.
            return Partner._ai_find_vendor(seller, company=company)
        return Partner._ai_vendor_decision(seller, company=company, policy=policy)[
            "vendor"
        ]

    def action_ai_create_vendor_from_extraction(self):
        """Create the contact from what was read off the document — ON REQUEST.

        🔴 THIS IS NOT A RETURN TO AUTOMATIC CREATION, AND THE DIFFERENCE IS THE
        ONLY REASON IT IS ALLOWED TO EXIST. Read
        `_create_partner_from_extraction` below before touching this: automatic
        creation was removed in 19.0.3.0.0 on a measurement (a sample set of
        seven vendor bills — six minted a duplicate beside an active, frequently
        billed vendor), core's own two creates are suppressed, and the source
        project's older version created vendors silently, which is exactly what
        must not happen.

        What was asked for, on bill C: a one-button way to create the contact
        from the document. A person pressing a button, having read the chatter note listing what was
        on the document and which existing vendors come closest, is a decision.
        A matcher guessing is not. The chatter note has invited this in words
        since F27 — *"Pick the right vendor below, or create one deliberately"* —
        and until now that sentence pointed at nothing.

        Four things it must not do, each of which is tested:

        1. 🔴 **It must not write a party that can never be a vendor.** The
           dedupe lookup goes through `res.partner._ai_find_vendor`, which
           applies this branch's guard, so the button cannot LINK the company's
           own partner, a child of it, one of our logins, or a contact owned by
           another company. And before creating anything it asks
           `_ai_vendor_refusal_for_seller`: if the seller block resolves ONLY to
           such a party, creating a fresh contact from it would mint a second
           copy of our own company as a vendor — the same defect in a new hat —
           so it refuses and says which rule stopped it.
        2. 🔴 **It must not create a duplicate.** `_ai_find_vendor` runs FIRST,
           every time, with the full ordered search. Pressing the button twice,
           or pressing it on a second bill from the same vendor, finds what the
           first press made and links it. That matching is reused rather than
           rewritten: this module's history is a list of duplicate-vendor
           defects, and a fresh search here would be the next one.
        3. It must leave a trail. `_ai_apply_vendor_data` posts the note, with a
           first line naming who pressed and which bill — the same shape
           `vendor_candidate.action_use_this` uses, for the same reason.
        4. It must not fire on a bill that already has a vendor. The arch hides
           it, and this method checks it too — a button's visibility rule is UI
           only, and an RPC is not the UI.
        """
        self.ensure_one()
        if self.partner_id:
            raise UserError(
                _(
                    "This bill already has a vendor (%s). Change it on the "
                    "field rather than creating another contact.",
                    self.partner_id.display_name,
                )
            )
        if self.state != "draft":
            raise UserError(_("Only a draft bill's vendor can be set this way."))

        # 🔴 UNDER THE POLICY THE IMPORT USED (core/source split).
        # The dedupe lookup below is the matcher; on a bill a strict source
        # (Finvoice, APIX) imported, the default matcher would LINK the
        # established vendor that merely shares a VAT number with others —
        # the very decision the strict import refused to take.
        move = self._ai_with_import_policy()
        if move is not self:
            return move.action_ai_create_vendor_from_extraction()

        extracted = self._ai_stored_extraction()
        seller = (extracted or {}).get("seller")
        seller = seller if isinstance(seller, dict) else {}
        name = (seller.get("name") or "").strip()
        if not name:
            raise UserError(
                _(
                    "Nothing was read off this document that could name a "
                    "vendor, so there is nothing to create a contact from."
                )
            )

        Partner = self.env["res.partner"]
        company = self.company_id or self.env.company

        # (2) Never a duplicate — the guarded matcher, first, every time.
        # Under a strict policy only a vendor its assigning identifiers settle
        # on is linked; an ambiguous or name-only match creates the contact
        # the person asked for (the candidates stay one click away).
        policy = Partner._ai_matching_policy()
        if policy.is_legacy:
            existing = Partner._ai_find_vendor(seller, company=company)
        else:
            existing = Partner._ai_vendor_decision(
                seller, company=company, policy=policy
            )["vendor"]
        if existing:
            self.partner_id = existing
            # ⚠️ POSTED HERE, NOT PASSED AS `origin`. Same reason as the create
            # branch below: `_ai_apply_vendor_data`'s note is built from its
            # summary and is skipped when the summary is empty, so on a contact
            # that already holds everything the document says, the one line
            # explaining why nobody was created would not be written.
            self.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "%(user)s asked for a contact to be created from this "
                    "bill; %(partner)s already existed, so the bill was filed "
                    "against it instead of creating a second one.",
                    user=self.env.user.display_name,
                    partner=existing.display_name,
                )
            )
            self._ai_apply_vendor_data(existing.commercial_partner_id, extracted)
            self.ai_vendor_candidate_ids.unlink()
            return True

        # (1) …and never a party that can never be a vendor.
        refused, reason = Partner._ai_vendor_refusal_for_seller(
            seller, company=company
        )
        if reason:
            raise UserError(Partner._ai_refused_vendor_message(refused, reason))

        vendor, refused_vat = self._ai_create_vendor(
            self._ai_vendor_vals_from_seller(seller)
        )
        self.partner_id = vendor

        # (3) THE TRAIL, POSTED HERE AND NOT LEFT TO `_ai_apply_vendor_data`.
        # 🔴 THAT WAS THE FIRST VERSION AND IT POSTED NOTHING. That method's
        # note is built from its SUMMARY — what it filled, learned or found
        # contradictory — and `_ai_post_vendor_data_note` returns early when the
        # summary is empty (`account_move.py`, "skipped entirely when the
        # summary is empty"). A contact just created FROM this document has
        # nothing left to fill and nothing to contradict, so the summary is
        # empty by construction and the one event that most needs recording was
        # the one event with no note. Caught by
        # `test_it_records_who_asked_and_from_which_bill`.
        body = Markup("<p><strong>%s</strong></p><p>%s</p>") % (
            _(
                "%(user)s created this contact from bill %(bill)s, using "
                "what was read off the document.",
                user=self.env.user.display_name,
                bill=self.display_name,
            ),
            _(
                "Nothing was invented: the name, address and tax id come "
                "from this invoice, and no bank account was written. Check "
                "them against the document before posting.",
            ),
        )
        if refused_vat:
            body += Markup("<p>%s</p>") % _(
                "The tax id %(vat)s read off the document was not stored: "
                "Odoo's VAT check does not accept it for this contact's "
                "country. It is waiting in Vendor Data Suggestions; compare "
                "it with the document before applying it.",
                vat=refused_vat,
            )
        self.message_post(body=body)

        # `_ai_apply_vendor_data` still runs, for the half that is not the note:
        # it turns the invoice's phone and web domain into
        # `goodahead.partner.identifier` rows, which is what makes the NEXT bill
        # from this vendor match without anybody — the whole point of creating
        # the contact rather than leaving the bill empty.
        self._ai_apply_vendor_data(vendor, extracted)
        self.ai_vendor_candidate_ids.unlink()
        return True

    def _ai_create_vendor(self, vals):
        """Create the contact; drop a tax id that Odoo's VAT check refuses.

        Odoo 18.0: with ``base_vat`` installed, a number that is not a VAT
        number in Odoo's eyes (a Canadian business number, a US EIN, a misread
        digit) makes ``create`` raise, and the person pressing the button gets
        a validation error instead of a contact. The rest of the document is
        still good, so the contact is created without the tax id, which stays
        on the bill as a suggestion. Returns ``(partner, refused_vat)``.
        """
        Partner = self.env["res.partner"]
        vat = vals.get("vat")
        if not vat:
            return Partner.create(vals), False
        try:
            with self.env.cr.savepoint():
                return Partner.create(vals), False
        except ValidationError:
            _logger.info(
                "Invoice import: tax id %r refused by the VAT check; "
                "creating the contact without it.",
                vat,
            )
        vals = {key: value for key, value in vals.items() if key != "vat"}
        return Partner.create(vals), vat

    def _ai_vendor_vals_from_seller(self, seller):
        """The `res.partner` values for a contact created from `seller`.

        ⚠️ `vat` IS INCLUDED HERE AND NOWHERE ELSE IN THIS MODULE, and that is
        not an oversight being corrected. F27's rule — *"Two fields are NEVER
        automatic, empty or not: bank account and tax ID"* — is about writing
        onto an EXISTING vendor, where a changed tax identity is the shape of
        invoice fraud. On a contact a person has just asked to be created from
        this document there is no previous value to overwrite and no record to
        impersonate; omitting the tax id would leave the person to retype what
        is on the screen in front of them. **The bank account is still never
        written**, by this method or any other.

        🔴 NO `supplier_rank` IS SET. Core increments it when a bill is
        confirmed. Setting it here would claim a purchase history that does not
        exist, and `supplier_rank` is the tiebreak the candidate ranking uses.
        """
        country = self._ai_resolve_location(
            "country_id", (seller.get("country_code") or "").strip() or None
        )
        state = self._ai_resolve_location(
            "state_id", (seller.get("state_code") or "").strip() or None,
            country=country,
        )
        vals = {
            "name": (seller.get("name") or "").strip(),
            # `parent_id = False` is implied, and `is_company = True` is right
            # here even though it is NOT the matcher's test: a record created
            # from an invoice's seller block IS a company, whatever the flag
            # happens to say on rows imported years ago.
            "is_company": True,
            "street": (seller.get("street") or "").strip() or False,
            "city": (seller.get("city") or "").strip() or False,
            "zip": (seller.get("postal_code") or "").strip() or False,
            "country_id": country.id if country else False,
            "state_id": state.id if state else False,
            "phone": (seller.get("phone") or "").strip() or False,
            "email": (seller.get("email") or "").strip() or False,
            "website": (seller.get("website") or "").strip() or False,
        }
        vat = "".join((seller.get("vat") or "").split()).upper()
        if vat:
            vals["vat"] = vat
        business_id = (seller.get("business_id") or "").strip()
        if business_id:
            # The key `_import_retrieve_customer_from_ai_business_id` matches on,
            # so the next bill from this vendor is found by its registration
            # number rather than by its printed name.
            vals["company_registry"] = business_id
        return {key: value for key, value in vals.items() if value is not False}

    def _create_partner_from_extraction(self, invoice, extracted_data):
        """Match an existing vendor for the extracted seller. 🔴 NEVER CREATE ONE.

        ⚠️ "NEVER" STILL MEANS NEVER *HERE*, AND SINCE 19.0.3.24.0 THERE IS A
        BUTTON THAT DOES CREATE ONE — `action_ai_create_vendor_from_extraction`
        above. The two are not in tension and the distinction is the whole
        design: this method runs inside the import, with nobody watching, and
        must never invent a vendor. The button runs when a person has read the
        chatter note and pressed it. Automatic creation is still forbidden; a
        deliberate one is a decision, and it goes through this branch's guard
        and through `_ai_find_vendor` for dedupe before it writes anything.

        🔴 THIS IS A DELIBERATE BEHAVIOUR CHANGE AND THE BIGGEST RISK IN
        19.0.3.0.0: A BILL WITH NO VENDOR CANNOT BE CONFIRMED. It is chosen
        anyway, on the measurement on a sample set of seven vendor bills — six
        were filed against a brand-new partner standing beside an active,
        frequently billed vendor. Each of those costs: no purchase
        history, no payment terms, no bank details, an aged payable split across
        two records per vendor, and a duplicate that does not even sort to the
        top of the vendor dropdown, so the next person picks the old one and
        deepens the split rather than healing it. With a vendor billed that often
        that is cheap to prevent now and expensive to merge later.

        So: *a bill nobody filed is visible; a bill filed against the wrong
        vendor is not.* The bill is left with an empty vendor and ONE chatter
        note saying so, naming what was read off the invoice and listing the
        closest existing vendors with how many posted bills we have had from
        each — the tiebreak a person actually uses.

        ⚠️ THE METHOD KEEPS ITS NAME, which now describes what it no longer
        does. Renaming it would be tidier and would also silently break every
        caller and test that names it, including the re-import path. The name is
        left and the docstring says what happened; `__manifest__.py` 19.0.3.0.0
        says it louder.

        ⚠️ NOTHING NEW IS STORED FOR THE LATER PASS, because the extraction is
        already stored twice: `ai.invoice.import.extraction_data` holds the
        whole JSON on every record (`models/ai_invoice_import.py`, the
        `_finalize_import` write), and `_ai_post_process` attaches the same JSON
        to the move as `extraction.json`. F27 §Part 2 says so in as many words —
        *"It needs no new stored data"* — and a third copy could only drift from
        the other two.
        """
        seller = extracted_data.get("seller", {}) or {}
        name = (seller.get("name") or "").strip()
        if not name:
            return

        # 🔴 THE BILL'S COMPANY, AND IT WAS MISSING (PR #336 review finding 1).
        # `_ai_find_vendor` now refuses on every one of its six steps — the
        # business-id step was the one that did not — but it can only refuse
        # against the company it is given, and this caller gave it none. In a
        # queue job `self.env.company` is not reliably the bill's.
        Partner = self.env["res.partner"]
        policy = Partner._ai_matching_policy()
        if not policy.is_legacy:
            # A source's own policy (core/source split): weak identifiers propose,
            # an ambiguous or conflicting strong one leaves the vendor empty
            # and offers every vendor it pointed at.
            decision = Partner._ai_vendor_decision(
                seller, company=invoice.company_id, policy=policy
            )
            if decision["vendor"]:
                invoice.partner_id = decision["vendor"]
                return
            self._ai_post_unrecognised_vendor(invoice, seller, decision=decision)
            return

        existing = self._ai_match_existing_vendor(
            seller, company=invoice.company_id
        )
        if existing:
            invoice.partner_id = existing
            return

        self._ai_post_unrecognised_vendor(invoice, seller)

    @api.model
    def _ai_post_unrecognised_vendor(self, invoice, seller, decision=None):
        """ONE chatter note: the vendor was not recognised, here is what was read,
        and here are the closest existing vendors.

        ``decision`` (core/source split) is `res.partner._ai_vendor_decision`'s
        answer under a source's own matching policy. When an assigning
        identifier pointed at more than one vendor company, the note says so
        and EVERY one of them is offered, uncapped and ranked by posted bills;
        otherwise the weak candidates are the policy's proposing
        identifiers."""
        read_off = [
            (_("Name"), seller.get("name")),
            (_("Phone"), seller.get("phone")),
            (_("Email"), seller.get("email")),
            (_("Website"), seller.get("website")),
            (_("Street"), seller.get("street")),
            (_("City"), seller.get("city")),
            (_("State"), seller.get("state_code")),
            (_("ZIP"), seller.get("postal_code")),
            (_("Country"), seller.get("country_code")),
            (_("Tax ID"), seller.get("vat")),
            (_("Business id"), seller.get("business_id")),
        ]
        # `Markup(...) % value` escapes the value; `Markup(f"...")` would not,
        # and these values come straight out of a model's reading of a PDF. See
        # the `chatter_html.py` gate.
        rows = Markup("").join(
            Markup("<li><strong>%s:</strong> %s</li>") % (label, value)
            for label, value in read_off
            if isinstance(value, str) and value.strip()
        )
        body = Markup(
            "<p><strong>%s</strong></p><p>%s</p><p>%s</p><ul>%s</ul>"
        ) % (
            _("This vendor was not recognised, so the bill has no vendor yet."),
            _(
                "No new contact was created for it on purpose: an invented "
                "vendor carries no history, no payment terms and no bank "
                "details, and splits the aged payable across two records. "
                "Pick the right vendor below, or create one deliberately."
            ),
            _("Read off the invoice:"),
            rows,
        )

        # The BILL's company, not the reader's: the count on each card is "bills
        # this company has had from that vendor", and on an import running in a
        # queue job the reader is whoever uploaded the PDF.
        if decision is None:
            candidates = self._ai_vendor_candidates(
                seller, company=invoice.company_id
            )
        elif decision["candidates"]:
            candidates = self._ai_strong_candidates(
                decision, company=invoice.company_id
            )
            body += Markup("<p>%s</p>") % self._ai_ambiguity_sentence(decision)
        else:
            policy = self.env["res.partner"]._ai_matching_policy()
            candidates = self._ai_vendor_candidates(
                seller,
                limit=policy.candidate_limit,
                company=invoice.company_id,
                policy=policy,
            )
        # 🔴 THE SAME LIST BECOMES ROWS. The chatter keeps the note (it is the
        # record of what the import read), but a note is read and not acted on —
        # which is exactly the objection raised against burying this
        # in a tab. The rows are what appears under the Vendor field, each with
        # a button, where the person is already looking.
        self.env["goodahead.vendor.candidate"]._build_for_move(invoice, candidates)
        if candidates:
            lines = Markup("").join(
                Markup("<li><strong>%s</strong>%s — %s, %s</li>")
                % (
                    candidate["partner"].display_name,
                    # `self.env._`, not `_()`: in a generator expression 18.0's
                    # `_()` cannot find the language from the frame.
                    self.env._(" (archived)") if candidate["archived"] else "",
                    candidate["reason"],
                    # No plural forms in `_()`, so two translatable strings.
                    self.env._("1 posted bill")
                    if candidate["bill_count"] == 1
                    else self.env._("%s posted bills", candidate["bill_count"]),
                )
                for candidate in candidates
            )
            body += Markup("<p>%s</p><ul>%s</ul>") % (
                _("Closest existing vendors:"),
                lines,
            )

        invoice.message_post(body=body)

    @api.model
    def _ai_strong_candidates(self, decision, company=None):
        """Every vendor company an assigning identifier pointed at when it
        could not decide — NO cap, ranked by posted bills. Same
        row shape as `_ai_vendor_candidates`."""
        partners = decision["candidates"]
        if not partners:
            return []
        company = company or self.env.company
        states = self._ai_bill_counts(partners, company=company)
        ranked = partners.sorted(
            key=lambda partner: (
                -states.get(partner, {}).get("posted", 0),
                -partner.supplier_rank,
                partner.id,
            )
        )
        labels = {
            KEY_VAT: _("same VAT number"),
            KEY_BUSINESS_ID: _("same Business ID"),
            KEY_PHONE: _("same phone"),
            KEY_DOMAIN: _("same domain"),
            KEY_NAME: _("same name"),
        }
        reason = ", ".join(labels[key] for key in decision["keys"] if key in labels)
        return [
            {
                "partner": partner,
                "reason": reason,
                "bill_count": states.get(partner, {}).get("posted", 0),
                "draft_bill_count": states.get(partner, {}).get("draft", 0),
                "archived": not partner.active,
            }
            for partner in ranked
        ]

    @api.model
    def _ai_ambiguity_sentence(self, decision):
        """Why a strong identifier set no vendor."""
        labels = {
            KEY_VAT: _("VAT number"),
            KEY_BUSINESS_ID: _("Business ID"),
            KEY_PHONE: _("phone"),
            KEY_DOMAIN: _("web domain"),
            KEY_NAME: _("name"),
        }
        keys = " / ".join(labels[key] for key in decision["keys"] if key in labels)
        if decision["reason"] == "conflict":
            return _(
                "The invoice's %(keys)s point to different vendors, so none of "
                "them was set. Check which one is right and pick it below.",
                keys=keys,
            )
        return _(
            "The invoice's %(keys)s matches more than one vendor, so none of "
            "them was set. Pick the right one below; the others may be "
            "duplicates to merge.",
            keys=keys,
        )

    @api.model
    def _ai_vendor_candidates(self, seller, limit=3, company=None, policy=None):
        """The best existing partners to OFFER for an unrecognised vendor.

        Returns a list of ``{"partner", "reason", "bill_count", "archived"}``,
        ordered ``supplier_rank desc, id asc`` — the established vendor first.

        Found by, strongest signal first: a shared phone or a shared non-public
        domain (a signal, but not a unique one, which is exactly why it is a
        candidate here and not a match in `_ai_match_existing_vendor`); then a
        LOOSE name match — `ilike` on the whole printed name and on its longest
        word. `Northwind` finds `NORTHWIND - Smith reps` that way, which is the case
        F27 exists for: the bill comes from the manufacturer and the ledger
        holds the rep agency that sells for them, so no rule derives one from
        the other and a person has to say so once.

        🔴 `bill_count` IS COUNTED, NOT PROXIED. It is the number of POSTED
        `in_invoice` / `in_refund` moves for that commercial partner. The
        obvious shortcut — `supplier_rank` — is not the same number and is not
        bills: it is incremented by other things and never decremented. The
        question asked is whether the candidates already have earlier bills,
        so the column has to be bills or it must not be called bills.

        Archived partners are INCLUDED and marked. One of them may well be the
        right answer, and hiding it would send somebody off to create the
        duplicate by hand.

        `company` is the bill's company, not the reader's — see the
        multi-company note in `_compute_ai_vendor_bill_count`. It defaults to
        `self.env.company` so a caller
        that has no move in hand still gets a company-scoped number rather than
        a cross-company one.

        ``policy`` (core/source split): given a non-default `MatchingPolicy`, only
        its proposing identifiers offer candidates — the name as an exact,
        case-insensitive name — and they are ranked by posted bills, as spec
        AC-04 states. Without one (the AI source) the list is exactly what it
        was.
        """
        seller = seller or {}
        policy = policy if policy is not None and not policy.is_legacy else None
        Partner = self.env["res.partner"].with_context(active_test=False)
        Identifier = self.env["goodahead.partner.identifier"]

        reasons = {}

        def remember(partners, reason):
            for partner in partners:
                reasons.setdefault(partner.id, reason)

        if policy is None or policy.proposes(KEY_PHONE):
            phone = Identifier._normalise_phone(seller.get("phone"))
            remember(Identifier._partners_for("phone", [phone]), _("same phone"))

        if policy is None or policy.proposes(KEY_DOMAIN):
            domains = [
                Identifier._normalise_domain(seller.get("email")),
                Identifier._normalise_domain(seller.get("website")),
            ]
            remember(Identifier._partners_for("domain", domains), _("same domain"))

        name = str(seller.get("name") or "").strip()
        if name and policy is not None and policy.proposes(KEY_NAME):
            # 🔴 UNDER A SOURCE'S OWN POLICY THE NAME IS AN EXACT NAME (spec
            # AC-04: "exact name", ignoring case). A structured e-invoice
            # states its seller's legal name; offering every vendor whose
            # name merely CONTAINS a word of it ("Oy", "Finland") is noise on
            # a screen meant to be acted on. The loose search below stays the
            # AI reading's, whose printed names are what F27 was built for.
            # `%` and `_` in the printed name are escaped: they are letters
            # of the name here, not wildcards.
            exact = like_literal(name)
            remember(
                Partner.search(
                    [("name", "=ilike", exact), ("parent_id", "=", False)], limit=20
                ),
                _("same name"),
            )
            remember(
                Partner.search(
                    [("name", "=ilike", exact), ("parent_id", "!=", False)], limit=20
                ).commercial_partner_id,
                _("same name on a contact"),
            )
        elif name and policy is None:
            # 🔴 `%` and `_` are letters of the printed name, not wildcards
            # (security fix): a name of `%` must not offer every partner.
            loose = like_literal(name)
            # A generous cap rather than `limit`: the ranking below decides which
            # three are offered, and a `limit` here would let the database's own
            # ordering decide instead.
            #
            # 🔴 `parent_id = False`, NEVER `is_company = True` — see the long
            # note in `_ai_match_existing_vendor`. With `is_company` this method
            # returned an EMPTY LIST for `Northwind`, measured against a
            # migrated test database, because `NORTHWIND - Smith reps` carries the
            # flag unset like a large share of the vendors do. The candidate list is the
            # whole first screen of F27, and it produced nothing on the one case
            # the feature was designed around.
            remember(
                Partner.search(
                    [("name", "ilike", loose), ("parent_id", "=", False)], limit=20
                ),
                _("similar name"),
            )
            # 🔴 AND THE CHILD CONTACTS, THROUGH THEIR COMMERCIAL PARTNER —
            # a review finding. `_ai_retrieve_partner_with_exact_name`
            # ships unguarded on the argument that *"every partner core's
            # substring step could have matched is already in the set
            # `_ai_vendor_candidates` offers"*. It was not: core's legacy step
            # (`account/models/partner.py:956` at `ca1f0aea`) carries no
            # `parent_id` filter, so a bill printing a name that occurs only in
            # a CHILD CONTACT's name failed both halves at once — the narrowed
            # step stopped matching it AND the candidate list excluded it. The
            # resemblance was dropped rather than moved from a silent decision
            # to a visible candidate, and the claim says *every*.
            #
            # 🔴 `.commercial_partner_id`, AND THE FIRST VERSION OF THIS FIX
            # OFFERED THE CONTACT ITSELF. Three things were wrong with that,
            # and the third is the one that matters:
            #   1. `_ai_posted_bill_counts` groups on `commercial_partner_id`,
            #      so a contact's card always read "0 bills" however
            #      established its parent — i.e. "new vendor, safe to skip" on
            #      exactly the record a human was being asked to judge.
            #   2. the ranking is `(-supplier_rank, id)` and `supplier_rank`
            #      lives on the commercial partner, so every contact arrived at
            #      rank 0 — and against another rank-0 candidate the tiebreak
            #      is `id asc`, so an old contact could displace a
            #      phone-matched or domain-matched vendor. "Strictly additive"
            #      was true of the reason and false of the PLACE.
            #   3. `vendor_candidate.action_use_this` writes
            #      `move.partner_id = candidate` and then applies the seller's
            #      phone, email and address to it — so one click would have
            #      filed the bill against a sub-contact and written the
            #      company's details onto it.
            # Mapping through the commercial partner keeps every candidate a
            # party a bill may be filed against, which is the vocabulary
            # `_partners_for` already uses for the phone and domain keys, and
            # it is still strictly additive: the party is offered where it was
            # not offered before.
            #
            # ⚠️ The reason names the CONTACT as the thing that matched, because
            # the parent's own name may not contain the printed name at all —
            # which is precisely the case this search exists for.
            remember(
                Partner.search(
                    [("name", "ilike", loose), ("parent_id", "!=", False)], limit=20
                ).commercial_partner_id,
                _("similar name on a contact"),
            )
            words = sorted(
                (w for w in re.split(r"[^0-9A-Za-z]+", name) if len(w) >= 3),
                key=len,
                reverse=True,
            )
            if words:
                remember(
                    Partner.search(
                        [("name", "ilike", words[0]), ("parent_id", "=", False)],
                        limit=20,
                    ),
                    _("name contains “%s”", words[0]),
                )

        if not reasons:
            return []

        # 🔴 NEVER OFFER THIS BILL'S OWN COMPANY, ONE OF OUR LOGINS, OR ANOTHER
        # COMPANY'S CONTACT (19.0.3.23.0). The phone and domain signals arrive
        # through `_partners_for`, which has dropped our company partners since
        # 19.0.3.0.0 — but the three NAME searches above do not go through it,
        # so a seller whose printed name resembles our own reached the card.
        # `vendor_candidate.action_use_this` writes `move.partner_id = candidate`
        # and then applies the seller's phone, email and address to it, so such a
        # card is one click from BOTH halves of bill A.
        #
        # ⚠️ `company` is the BILL's company (see this method's docstring), so
        # another company's PARTNER is still offered — the intercompany case —
        # while a contact OWNED by another company is not. On a two-company
        # database a share of the vendors belong to the second company; offering
        # one of those on an Alpha LLC bill is rule 3 being broken.
        offered = self.env["res.partner"]._ai_drop_impossible_vendors(
            Partner.browse(list(reasons)), company=company or self.env.company
        )
        if policy is not None:
            # AC-04: "ranked by their number of posted bills".
            posted = self._ai_posted_bill_counts(offered, company=company)
            ranked = offered.sorted(
                key=lambda partner: (
                    -posted.get(partner, 0),
                    -partner.supplier_rank,
                    partner.id,
                )
            )[:limit]
        else:
            ranked = offered.sorted(
                key=lambda partner: (-partner.supplier_rank, partner.id)
            )[:limit]
        if not ranked:
            return []

        # 🔴 THE SAME HELPER THE FORM LINE USES. The card says "7 bills" and the
        # line that replaces it once the card is clicked must say 7 too; two
        # domains written out twice is how those two numbers start to differ.
        # 🔴 ONE CALL, BOTH NUMBERS. `_ai_bill_counts` groups on state as well
        # as partner, so the draft number costs no extra
        # query and cannot disagree with the posted one about which rows count.
        states = self._ai_bill_counts(ranked, company=company)
        return [
            {
                "partner": partner,
                "reason": reasons[partner.id],
                "bill_count": states.get(partner, {}).get("posted", 0),
                "draft_bill_count": states.get(partner, {}).get("draft", 0),
                "archived": not partner.active,
            }
            for partner in ranked
        ]

    # ------------------------------------------------------------------
    # Applying what the invoice says to the vendor it was filed against
    # ------------------------------------------------------------------

    @api.model
    def _ai_resolve_location(self, field_name, code, country=None):
        """A `res.country` or `res.country.state` for a printed code.

        🔴 LOOKED UP, NEVER CREATED, because an import that creates them
        invents US "states" — say a SECOND "New Jersey" whose code is `1`. The
        state code is what Odoo sends to a carrier, and a carrier refuses a
        booking over it. A code nothing matches is therefore NOT applied, and the bill's
        note says it was not — a blank field is visible and somebody fixes it; a
        state called `1` is not.

        A state is only unique inside a country, so the caller passes the
        country it is working with — which during an apply is the one this very
        run may have just filled in, and is therefore NOT readable off the
        partner yet. With no country there is no lookup, and that is a decline
        rather than a guess: `NJ` is a state in several countries' lists.
        """
        code = (code or "").strip()
        if not code or not code.isascii() or not code.isalnum():
            # A code is letters and digits; anything else (a `%`, a `_`) is
            # not one, and is never used as a search pattern.
            return None
        if field_name == "country_id":
            return self.env["res.country"].search(
                [("code", "=ilike", code)], limit=1
            ) or None
        if not country:
            return None
        return self.env["res.country.state"].search(
            [("code", "=ilike", code), ("country_id", "=", country.id)], limit=1
        ) or None

    def _ai_import_policy(self):
        """The matching policy the bill's import ran under: its import
        record's reader's — derived, not stored, so it is the same
        answer the import and a re-import got. The default policy for a bill
        no import created, or one whose record no installed reader claims."""
        self.ensure_one()
        if not self.id:
            return DEFAULT_POLICY
        record = (
            self.env["goodahead.invoice.import"]
            .sudo()
            .search([("move_id", "=", self.id)], order="id desc", limit=1)
        )
        return record._import_reader_policy() if record else DEFAULT_POLICY

    def _ai_with_import_policy(self):
        """This bill with its import's policy on the context, for the
        vendor buttons a person presses after the import (create the contact,
        a candidate's "Use this"): they decide under the same rules the import
        did, bank-account rule included. ``self`` unchanged when a policy is on
        the context already or the import's is the default one."""
        self.ensure_one()
        if self.env.context.get(MATCHING_POLICY_CONTEXT_KEY):
            return self
        policy = self._ai_import_policy()
        if (
            policy.is_legacy
            and not policy.bank_account_lookup_only
            and not policy.contact_details_need_review
        ):
            return self
        return self.with_context(**{MATCHING_POLICY_CONTEXT_KEY: policy})

    def _ai_stored_extraction(self):
        """The extraction JSON for this bill, from wherever it still lives.

        ⚠️ NOTHING NEW IS STORED FOR THIS. F27 §Part 2 says so in as many words
        — *"It needs no new stored data"* — and the extraction is already kept
        TWICE: `ai.invoice.import.extraction_data` and the `extraction.json`
        attachment on the move. A third copy could only drift from the other
        two. The import record is preferred because a person can edit it and
        re-import; the attachment is the fallback for a bill whose import
        record was cleaned up.

        Returns `{}` rather than raising. A bill with no readable extraction is
        ordinary — a bill entered by hand has none — and every caller here is a
        button, not a pipeline step.
        """
        self.ensure_one()
        record = self.env["goodahead.invoice.import"].search(
            [("move_id", "=", self.id)], order="id desc", limit=1
        )
        candidates = [record.extraction_data] if record else []
        attachment = self.env["ir.attachment"].search(
            [
                ("res_model", "=", "account.move"),
                ("res_id", "=", self.id),
                ("name", "=", "extraction.json"),
            ],
            order="id desc",
            limit=1,
        )
        if attachment:
            candidates.append(attachment.raw)
        for raw in candidates:
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    def _ai_apply_vendor_data(self, partner, extracted, origin=None, automatic=False):
        """Fill the vendor's EMPTY fields from this invoice; queue the rest.

        🔴 ``automatic`` (the import itself, nobody involved) under a policy
        whose ``contact_details_need_review`` is set — an AI-read PDF, anything
        that arrived by email — sends the document's email, phone and website
        to pending suggestions even when the vendor's field is empty, and
        learns NO identifiers: a forged document in a real vendor's name must
        not redirect the vendor's mail or teach the matcher the forger's
        domain. The identifiers are learned when a person confirms the vendor
        (the vendor buttons call this without ``automatic``) or posts the bill
        (`_post`).

        Returns a summary dict — ``filled``, ``learned``, ``conflicts``,
        ``withheld`` — which is the SAME object the chatter note is built from
        and the tests assert on, so a note that says something the code did not
        do is not expressible.

        🔴 THE SPLIT, ONE MORE TIME, BECAUSE IT IS THE DESIGN:
          * the vendor's field is EMPTY  → written, nobody asked, logged.
          * the vendor's field DIFFERS   → a pending
            `goodahead.vendor.data.suggestion`. Replacing something a person
            typed is a deliberate act, not a default.
          * a BANK ACCOUNT or a TAX ID   → a pending row EVEN WHEN EMPTY.
            Invoice fraud is a changed bank account; vendor invoices do print
            warnings about that scam.
          * the vendor's NAME            → nothing at all, ever. Not written,
            not suggested. `NORTHWIND - Smith reps` is the curated answer and
            `Northwind` is what the manufacturer prints.

        🔴 AND THE IDENTIFIERS ARE ADDED WHATEVER HAPPENS. The phone and the
        web domain printed on the invoice become `goodahead.partner.identifier`
        rows on the vendor whether the contact fields were empty, equal or in
        conflict — add-only, never replacing. That is the half that makes the
        NEXT bill from this vendor match with nobody involved, and it is why
        picking a candidate once is enough to learn Northwind.
        """
        self.ensure_one()
        seller = (extracted or {}).get("seller") or {}
        partner = partner.commercial_partner_id if partner else partner
        summary = {
            "filled": [],
            "learned": [],
            "conflicts": [],
            "withheld": [],
        }
        if not partner or not seller:
            return summary

        Partner = self.env["res.partner"]
        review_contacts = automatic and self._ai_contact_details_need_review()
        labels = {
            name: description["string"]
            for name, description in Partner.fields_get(
                [field for field, _key in _AI_VENDOR_AUTO_FIELDS],
                attributes=["string"],
            ).items()
        }

        to_write = {}
        pending = []
        for field_name, key in _AI_VENDOR_AUTO_FIELDS:
            raw = seller.get(key)
            # `isinstance(raw, str)` rather than a truth test: the extraction is
            # a model's reading of a PDF, so a `True` or a `17` in a text field
            # is a real possibility — and `str(True)` once became the country
            # code `TR` and therefore a Peppol scheme meaning TURKEY
            # (19.0.2.2.0). A non-string is not a contact detail.
            if not isinstance(raw, str) or not raw.strip():
                continue
            raw = raw.strip()
            label = labels.get(field_name, field_name)

            if field_name in ("state_id", "country_id"):
                # The country this run is working with: the one already on the
                # vendor, or the one the loop staged a moment ago for a vendor
                # that had none. `to_write` holds an id, hence the browse.
                country = partner.country_id or self.env["res.country"].browse(
                    to_write.get("country_id")
                )
                resolved = self._ai_resolve_location(field_name, raw, country)
                if not resolved:
                    summary["withheld"].append(
                        (
                            label,
                            raw,
                            _(
                                "Odoo has no such record and one is never "
                                "created here — an invented state is a code "
                                "the carrier rejects."
                            ),
                        )
                    )
                    continue
                current = partner[field_name]
                if not current:
                    to_write[field_name] = resolved.id
                    summary["filled"].append((label, resolved.display_name))
                elif current != resolved:
                    # 🔴 THE CODE, NOT THE DISPLAY NAME. `action_apply` resolves
                    # `invoice_value` through this same lookup, and
                    # "New Jersey (US)" resolves to nothing — the row would read
                    # perfectly and refuse to apply. Storing what the lookup
                    # consumes is what makes the round trip work.
                    pending.append(
                        {
                            "field_name": field_name,
                            "field_label": label,
                            "invoice_value": resolved.code,
                            "record_value": current.code,
                        }
                    )
                continue

            current = partner[field_name]
            if not current and review_contacts and field_name in _AI_CONTACT_FIELDS:
                pending.append(
                    {
                        "field_name": field_name,
                        "field_label": label,
                        "invoice_value": raw,
                        "record_value": False,
                        "note": _(
                            "Read off a document anybody can send, so it is "
                            "not filled in by itself. Applying it fills the "
                            "empty field on the contact."
                        ),
                    }
                )
                continue
            if not current:
                to_write[field_name] = raw
                summary["filled"].append((label, raw))
                continue
            if self._ai_same_contact_value(field_name, current, raw):
                continue
            pending.append(
                {
                    "field_name": field_name,
                    "field_label": label,
                    "invoice_value": raw,
                    "record_value": current,
                    # Said on the row itself, because the queue is read by
                    # somebody who did not see this code: applying one of these
                    # ADDS the value as another way the vendor is known and
                    # leaves the contact field alone.
                    "note": _(
                        "Applying this records the value as another way the "
                        "vendor is reached; it does not replace what is on the "
                        "contact."
                    )
                    if field_name in ("phone", "email", "website")
                    else False,
                }
            )

        if to_write and not partner.has_access("write"):
            # No `sudo()`. These are real edits to a vendor record and the ACL
            # should mean something. On 19.0 writing a contact needs Contact
            # Creation (`base.group_partner_manager`), which an invoicing-only
            # user may lack: the fills then become suggestions for someone who
            # can apply them, instead of failing the whole import.
            for field_name, value in to_write.items():
                label = labels.get(field_name, field_name)
                if field_name in ("state_id", "country_id"):
                    value = self.env[partner._fields[field_name].comodel_name].browse(value).code
                pending.append(
                    {
                        "field_name": field_name,
                        "field_label": label,
                        "invoice_value": value,
                        "record_value": False,
                    }
                )
            summary["filled"] = []
            to_write = {}

        if to_write:
            # No `sudo()`, as above. The identifier rows below ARE sudo'd,
            # because those are a derivation the system maintains rather than
            # user data.
            partner.write(to_write)

        if not review_contacts:
            summary["learned"] = self._ai_learn_identifiers(partner, seller)
        pending.extend(self._ai_sensitive_suggestions(partner, seller, summary))

        rows = self.env["goodahead.vendor.data.suggestion"]._replace_pending(
            self, partner, pending
        )
        summary["conflicts"] = [
            (row.field_label, row.invoice_value, row.record_value)
            for row in rows.filtered(lambda row: not row.is_sensitive)
        ]
        self._ai_post_vendor_data_note(partner, summary, origin=origin)
        return summary

    def _ai_contact_details_need_review(self):
        """Whether this bill's import keeps the document's contact details
        for a person (`MatchingPolicy.contact_details_need_review`): the
        policy on the context, else the one its import ran under."""
        policy = self.env.context.get(MATCHING_POLICY_CONTEXT_KEY)
        if not isinstance(policy, MatchingPolicy):
            policy = self._ai_import_policy() if len(self) == 1 else DEFAULT_POLICY
        return bool(policy.contact_details_need_review)

    def _post(self, soft=True):
        """A person posting an imported bill confirms its vendor: learn the
        document's phone and web domain as that vendor's identifiers now, the
        half the import itself left for a person (`_ai_apply_vendor_data`)."""
        posted = super()._post(soft=soft)
        for move in posted.filtered(
            lambda m: m.move_type in ("in_invoice", "in_refund") and m.partner_id
        ):
            move._ai_learn_identifiers_after_review()
        return posted

    def _ai_learn_identifiers_after_review(self):
        self.ensure_one()
        record = self.env["goodahead.invoice.import"].sudo().search(
            [("move_id", "=", self.id)], order="id desc", limit=1
        )
        if not record or not record._import_reader_policy().contact_details_need_review:
            return
        seller = (self._ai_stored_extraction() or {}).get("seller")
        if not isinstance(seller, dict):
            return
        self._ai_learn_identifiers(self.commercial_partner_id, seller)

    @api.model
    def _ai_same_contact_value(self, field_name, current, printed):
        """Is the printed value the SAME THING as the stored one?

        🔴 WITHOUT THIS EVERY VENDOR WITH A PHONE IS A CONFLICT. The record
        holds `+1 212-555-0111` and the Bluebird invoice prints `212.555.0111`:
        the same telephone, measured, in the sample set. A straight string
        comparison would queue a "difference" on nearly every bill, the count in
        the line under the Vendor field would be noise, and a line that is
        always there is a line nobody reads.

        Phone compares on its last ten digits and a website on its host, through
        the same normalisers the MATCHING uses — so what the matcher calls one
        vendor, this calls one value. An EMAIL compares whole: `sales@` and
        `purchasing@` really are different addresses, and which one the ledger
        holds is a decision worth offering, even though the domain was learned
        either way.

        ⚠️ IT TOOK A ``current_country`` AND A ``printed_country`` BETWEEN
        19.0.3.11.0 AND 19.0.3.22.0, because a country-aware key had to
        normalise the STORED number under the partner's country and the PRINTED
        one under the document's. Both arguments went with that key. If a
        country-aware normaliser ever returns, they return with it — a single
        country on both sides is what queued the same phone "conflict" on every
        bill from one vendor, for ever (found in review).
        """
        Identifier = self.env["goodahead.partner.identifier"]
        if field_name == "phone":
            left = Identifier._normalise_phone(current)
            right = Identifier._normalise_phone(printed)
            return bool(left) and left == right
        if field_name == "website":
            left = Identifier._host_of(current)
            right = Identifier._host_of(printed)
            return bool(left) and left == right
        return str(current).strip().casefold() == printed.strip().casefold()

    def _ai_learn_identifiers(self, partner, seller):
        """Store the invoice's phone and web domain as identifier rows.

        Add-only, through `goodahead.partner.identifier._add_pairs` — the same
        method `res.partner`'s own create/write hook uses, deliberately, so
        there is ONE implementation of "work out what is missing, then create
        it" and neither caller can quietly start replacing instead of adding.
        """
        self.ensure_one()
        Identifier = self.env["goodahead.partner.identifier"]
        pairs = Identifier._derive_from_contact(
            phone=seller.get("phone"),
            email=seller.get("email"),
            website=seller.get("website"),
        )
        created = Identifier._add_pairs(
            {partner.id: pairs},
            _("AI import %(bill)s", bill=self.display_name),
        )
        return [(kind, value) for _partner_id, kind, value in created]

    def _ai_sensitive_suggestions(self, partner, seller, summary):
        """The two details that wait for a person EVEN WHEN THE FIELD IS EMPTY.

        🔴 THE REASON IS NOT FUSSINESS ABOUT MONEY. **Invoice fraud IS a changed
        bank account** — a real-looking invoice from a real-looking vendor,
        carrying somebody else's account — and vendor invoices do carry printed
        warnings about exactly that scam. An import
        that writes a bank account off a PDF is that attack, automated and
        trusted. A tax id is the same shape of claim about who the vendor is.

        ⚠️ A vendor whose field is EMPTY is the case that makes this real: on a
        typical US vendor book almost no vendor with `supplier_rank > 0` carries
        a bank account or a VAT. So "only when it is empty" — the rule that
        makes every other field safe — would make EVERY bank account on EVERY
        invoice an automatic write. The one rule that protects the rest is the
        one that cannot apply here.
        """
        self.ensure_one()
        rows = []

        vat = seller.get("vat")
        if isinstance(vat, str) and vat.strip():
            vat = vat.strip()
            stored = (partner.vat or "").strip()
            if stored.casefold() != vat.casefold():
                rows.append(
                    {
                        "field_name": "vat",
                        "field_label": _("Tax ID"),
                        "invoice_value": vat,
                        "record_value": stored or False,
                        "is_sensitive": True,
                        "note": _(
                            "A tax id is never filled in automatically, empty "
                            "or not — it is a claim about who the vendor is."
                        ),
                    }
                )
                summary["withheld"].append(
                    (
                        _("Tax ID"),
                        vat,
                        _("never automatic — it is waiting in the queue."),
                    )
                )

        account = payee_account_number(seller)
        if account and self._ai_post_foreign_bank_account(account, partner):
            return rows
        if account:
            sanitized = sanitize_account_number(account)
            held = partner.with_context(active_test=False).bank_ids.filtered(
                lambda bank: bank.sanitized_acc_number == sanitized
            )
            if not held:
                rows.append(
                    {
                        "field_name": "bank_account",
                        "field_label": _("Bank account"),
                        "invoice_value": account,
                        "record_value": ", ".join(
                            partner.bank_ids.mapped("acc_number")
                        )
                        or False,
                        "is_sensitive": True,
                        "note": _(
                            "🔴 Invoice fraud is a changed bank account. Check "
                            "it against something other than this invoice "
                            "before applying it."
                        ),
                    }
                )
                summary["withheld"].append(
                    (
                        _("Bank account"),
                        account,
                        _(
                            "never automatic — invoice fraud is precisely a "
                            "changed bank account."
                        ),
                    )
                )
        return rows

    def _ai_post_foreign_bank_account(self, account, partner):
        """AC-07, under a policy with ``bank_account_lookup_only``: a bank
        account printed on the invoice that is registered to a DIFFERENT
        partner than the matched vendor is never offered as a vendor data
        suggestion; the chatter names whose it is instead. Returns True when
        that happened. (That it is not attached to the bill or the vendor is
        `res.partner.bank._find_or_create_bank_account`'s side.)

        The default policy keeps the AI import as it was: the account becomes
        a sensitive suggestion like any other the vendor does not hold."""
        self.ensure_one()
        if not self.env["res.partner.bank"]._ai_bank_lookup_only():
            return False
        owners = self._ai_foreign_account_owners(account, partner)
        if not owners:
            return False
        self.message_post(
            body=Markup("<p>%s</p>")
            % _(
                "The bank account %(account)s on this invoice is registered to "
                "%(owner)s, not to %(vendor)s. It was not attached to the bill or "
                "the vendor and is not offered as a vendor data suggestion.",
                account=account,
                owner=", ".join(owners.mapped("display_name")),
                vendor=partner.commercial_partner_id.display_name,
            )
        )
        return True

    def _ai_foreign_account_owners(self, account, partner):
        """The partners other than ``partner``'s company that hold
        ``account`` — empty when the vendor holds it itself or nobody does."""
        sanitized = sanitize_account_number(account) if account else False
        if not sanitized or not partner:
            return self.env["res.partner"]
        holders = (
            self.env["res.partner.bank"]
            .sudo()
            .with_context(active_test=False)
            .search([("sanitized_acc_number", "=", sanitized)])
            .partner_id
        )
        vendor = partner.commercial_partner_id
        if not holders or vendor in holders.commercial_partner_id:
            return self.env["res.partner"]
        return holders.commercial_partner_id.sudo(False)

    def _ai_post_vendor_data_note(self, partner, summary, origin=None):
        """One note: what was filled, what was learned, and — in its own block —
        what was deliberately NOT applied and why.

        ⚠️ `Markup(...).format(...)`, NEVER `Markup(f"...")`. `message_post`
        escapes a plain `str`, so a body containing `<br/>` prints its own tags;
        but the wrong fix turns a cosmetic bug into stored XSS, because every
        value interpolated below is a model's reading of a PDF. `.format` on a
        `Markup` escapes its arguments; an f-string does not. The
        development checks read this file.
        """
        self.ensure_one()
        if not any(summary[key] for key in ("filled", "learned", "withheld")) and (
            not summary["conflicts"]
        ):
            return

        def bullets(items):
            return Markup("").join(
                Markup("<li><strong>{}:</strong> {}</li>").format(*item)
                for item in items
            )

        body = Markup("<p><strong>{}</strong></p>").format(
            _(
                "Vendor details from this invoice, applied to %(vendor)s",
                vendor=partner.display_name,
            )
        )
        if origin:
            body += Markup("<p>{}</p>").format(origin)
        if summary["filled"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _("Filled in, because the vendor record had nothing there:"),
                bullets(summary["filled"]),
            )
        if summary["learned"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _(
                    "Learned, so the next bill from this vendor matches by "
                    "itself:"
                ),
                bullets(
                    (
                        # `self.env._`: `_()` cannot find the language in a
                        # generator expression on 18.0.
                        self.env._("Phone")
                        if kind == "phone"
                        else self.env._("Web domain"),
                        value,
                    )
                    for kind, value in summary["learned"]
                ),
            )
        if summary["conflicts"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _(
                    "Not applied — the invoice contradicts the vendor record, "
                    "and replacing something a person typed is a deliberate "
                    "act. Waiting for review:"
                ),
                Markup("").join(
                    Markup(
                        "<li><strong>{}:</strong> {} — the record holds {}</li>"
                    ).format(label, printed, held or "—")
                    for label, printed, held in summary["conflicts"]
                ),
            )
        if summary["withheld"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _("Deliberately not applied:"),
                Markup("").join(
                    Markup("<li><strong>{}</strong> ({}) — {}</li>").format(
                        label, value, reason
                    )
                    for label, value, reason in summary["withheld"]
                ),
            )
        self.message_post(body=body)
