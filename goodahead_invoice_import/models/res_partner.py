# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import re

from odoo import _, api, fields, models
from odoo.tools.sql import escape_psql

from ..lib.import_source import (
    ALL_KEYS,
    CONFLICT_FIRST_MATCH,
    DEFAULT_POLICY,
    KEY_BUSINESS_ID,
    KEY_DOMAIN,
    KEY_NAME,
    KEY_PHONE,
    KEY_VAT,
    MatchingPolicy,
)

#: The context key the import puts its source's `MatchingPolicy` on, so both
#: core pipelines and the post-processing read the same one (core/source split).
MATCHING_POLICY_CONTEXT_KEY = "goodahead_matching_policy"

#: What a printed VAT number (whitespace removed, upper case) may look like to
#: be searched at all: letters and digits, with the separators some countries
#: print (``.``, ``-``, ``/``). Deliberately wider than the EU shape, so a US
#: EIN (``12-3456789``) still matches.
_VAT_SHAPE = re.compile(r"^[A-Z0-9][A-Z0-9./-]{2,19}$")


def like_literal(value):
    """``value`` for an ``=ilike`` / ``ilike`` domain, its ``%``, ``_`` and
    ``\\`` taken literally. A value read off a document is a value, never a
    pattern: a VAT number of ``%`` would otherwise match every partner."""
    return escape_psql(value or "")

# The three fields an identifier can be derived from. `res.partner.write` is a
# tree-wide hot path — over a hundred thousand partners on a large database,
# touched by the intercompany flow, web-shop imports and every contact edit — so the override below does nothing at
# all unless one of these three keys is in `vals`. Keep this tuple short.
_IDENTIFIER_SOURCE_FIELDS = ("phone", "email", "website")


class ResPartner(models.Model):
    _inherit = "res.partner"

    identifier_ids = fields.One2many(
        "goodahead.partner.identifier",
        "partner_id",
        string="Known identifiers",
        help="Every phone number and web domain this partner has been seen "
        "under. Rows accumulate: changing the phone on the form ADDS a row, it "
        "never replaces one, because a vendor is reached on more than one "
        "number and the newest is not the only true one.",
    )

    # ------------------------------------------------------------------
    # Keeping the identifiers current
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        touched = self.browse()
        for partner, vals in zip(partners, vals_list):
            if any(key in vals for key in _IDENTIFIER_SOURCE_FIELDS):
                touched |= partner
        if touched:
            touched._ai_add_contact_identifiers("res.partner.create")
        return partners

    def write(self, vals):
        result = super().write(vals)
        if not any(key in vals for key in _IDENTIFIER_SOURCE_FIELDS):
            return result
        self._ai_add_contact_identifiers("res.partner.write")
        return result

    def _ai_add_contact_identifiers(self, source):
        """ADD the identifiers derivable from phone/email/website.

        🔴 ADD, NEVER REPLACE, AND NEVER DELETE. A row is not a copy of the
        current field value; it is a record that this partner was once reachable
        that way, and that is precisely what makes the next bill match.
        Correcting a typo in a phone number therefore leaves the old row behind
        — deliberately. A wrong row costs one ambiguous lookup, which declines;
        a deleted row costs a duplicate vendor, which is the defect this whole
        change exists to stop.

        The dedupe-and-create itself lives in
        `goodahead.partner.identifier._add_pairs`, with `sudo()` on it, because
        an invoice applying its own contact details to a vendor
        (`account.move._ai_apply_vendor_data`, 19.0.3.0.0 part 2) has to add
        rows the same way. Two copies of "work out what is missing, then create
        it" is how one of them quietly starts replacing instead of adding.
        """
        Identifier = self.env["goodahead.partner.identifier"]
        wanted = {}
        for partner in self:
            pairs = Identifier._derive_from_contact(
                partner.phone, partner.email, partner.website
            )
            if pairs:
                wanted[partner.id] = set(pairs)
        Identifier._add_pairs(wanted, source)

    # ------------------------------------------------------------------
    # 🔴 WE ARE NEVER A VENDOR ON OUR OWN BILL — two rules, one set,
    #    SCOPED TO THE BILL'S OWN COMPANY
    #
    # The requirement came in two steps. The first: exclude any
    # company-related contact and any user. The second, answering what "our
    # own" means in a two-company estate: look in the current company for a
    # contact first, and then at contacts that do not belong solely to another
    # company.
    #
    # 🔴 THE SECOND STEP NARROWS THE FIRST, AND THE NARROWING IS THE WHOLE
    # POINT. Read literally, "any company-related contact" refuses **Beta
    # Inc.** — and a Beta document billing Alpha LLC is not a mistake, it is
    # the intercompany flow real orders travel. So:
    #
    #   1. the BILL'S OWN company partner, and its whole contact tree, is
    #      refused;
    #   2. any partner carrying a `res.users` row is refused — we are never a
    #      vendor on one of our own logins either;
    #   3. ANOTHER company's partner is ALLOWED. That is the intercompany case,
    #      and under the ownership rule it is a shared contact
    #      (`company_id = False`), so it is reachable.
    #
    # An auditor of this file will ask why "this company" and not "any company":
    # it was the requirement, the alternative was measured, and the measurement
    # is in `CHANGELOG.md`.
    #
    # Rules 1 and 2 are tested against the SAME set: the candidate, its
    # commercial partner, and every parent up the chain. 🔴 UPWARD ONLY, AND
    # THAT IS NOT A DETAIL. Walking DOWN would refuse an ordinary vendor because
    # one of its contacts happens to carry a portal login, and a large
    # database has **many child contacts under partners with `supplier_rank > 0`**. See
    # `test_a_vendor_whose_child_contact_has_a_user_is_still_accepted`.
    #
    # ⚠️ BLAST RADIUS, MEASURED BEFORE WIDENING (a migrated test database,
    # read-only), because a guard that refuses a real vendor is worse
    # than the bug it fixes. Of the partners with `supplier_rank > 0`:
    #
    #   * rule 1 refuses **none** — the two companies' contact trees hold a few
    #     warehouse/notification contacts and none is a vendor;
    #   * rule 2 refuses **none** — of the partners that carry a `res.users`
    #     row, only a portal user has any rank at all, and only as a customer.
    #     **Neither company partner carries a user**, and nothing in either
    #     contact tree does, so rule 2 cannot refuse the intercompany
    #     counterparty either;
    #   * on a one-company legacy bench rule 2 refuses only `__system__`,
    #     `admin`, `public` and `portaltemplate`.
    #
    # 🔴 AND THE RULE THE FIRST STEP SEEMED TO ASK FOR IS NOT
    # IMPLEMENTED, DELIBERATELY. `res_partner.company_id` is the only thing that
    # identifies the four contacts core nearly picked on bill A
    # (`company_id = 1`, no parent, no user), so refusing on it would close that
    # hole. It would also refuse **dozens of vendors** on a two-company
    # database, which belong to the second company. Dozens of vendors to catch
    # four contacts is not a trade this guard makes; `company_id` is used for
    # ORDERING below, which is what the requirement actually asked of it. The residual
    # hole is written down in `CHANGELOG.md` rather than closed by guesswork.
    # ------------------------------------------------------------------

    @api.model
    def _ai_our_own_company_partners(self, company=None):
        """The partner of ONE company — the bill's — not of every company.

        🔴 ``company=None`` MEANS ``self.env.company``, AND EVERY CALLER THAT
        HAS A DOCUMENT IN HAND PASSES ITS COMPANY EXPLICITLY. The import runs in
        a queue job whose `env.company` is not guaranteed to be the company the
        bill was filed on, and a guard that silently protects the wrong company
        is worse than no guard: it refuses a legitimate intercompany vendor and
        lets the real one through.

        🔴 `active_test=False`. An ARCHIVED company's partner is still ours, and
        `res.company` carries an `active` field, so the default search silently
        narrows this. The copy of this line that used to live in
        `account.edi.common._import_partner_bank` had the context and the one in
        `goodahead.partner.identifier` did not — the ordinary way two spellings
        of a rule drift. There is now one.
        """
        if company:
            return company.sudo().with_context(active_test=False).partner_id
        return (
            self.env.company.sudo().with_context(active_test=False).partner_id
        )

    # ------------------------------------------------------------------
    # 🔴 THE OWNERSHIP ORDERING — AND CORE ALREADY DOES IT ON ONE BUILD
    #
    #   Look in the current company for a contact first, and then at contacts
    #   that do not belong solely to another company.
    #
    # Three things, in order: this company's contacts, then the shared ones,
    # never another company's.
    #
    # ⚠️ ON THE LEGACY CORE THIS IS ALREADY TRUE AND THERE IS NOTHING TO ADD.
    # `_retrieve_partner` (`account/models/partner.py:983-994` at `ca1f0aea`)
    # loops EVERY step over exactly these two domains in exactly this order —
    # `[*_check_company_domain(company), ('company_id','!=',False)]` then
    # `[('company_id','=',False)]` — and `_check_company_domain` resolves to
    # `company_id in [company.id, False]` (`odoo/orm/models.py:3996`), so ANDing
    # the first with `company_id != False` leaves `company_id = company.id`.
    # Another company's contact is unreachable by construction.
    #
    # 🔴 ON THE SEARCH-PLAN CORE RULES 2 AND 3 HOLD AND THE ORDERING DOES NOT.
    # `_import_retrieve_customer` (`account/models/partner.py:1018-1024` at
    # `189df353`) builds ONE domain, `Domain.OR([this-company, shared])`, and
    # resolves it with `limit=1`. So another company's contact is still
    # unreachable, but between an equally-matching current-company contact and a
    # shared one the database's ordering decides. That is core's behaviour on a
    # build we do not own; what this module can do is make ITS OWN steps follow
    # the required order, which is what `_ai_vendor_search` below is for.
    # ------------------------------------------------------------------

    @api.model
    def _ai_company_ownership_passes(self, company=None):
        """The two passes, in the required order. Never another company."""
        company = company or self.env.company
        return (
            [*self._check_company_domain(company), ("company_id", "!=", False)],
            [("company_id", "=", False)],
        )

    @api.model
    def _ai_refused_vendor_domain(self, company=None):
        """Domain terms that keep refused parties OUT OF THE SEARCH ITSELF.

        🔴 WHY THIS EXISTS AND WHY IT IS NOT MERELY AN OPTIMISATION (PR #336
        review finding 2, and it is the more serious of the two). Dropping
        refused rows AFTER a `limit=20` fetch means twenty refused rows can eat
        the whole window and a legitimate vendor sitting at row twenty-one is
        never seen. The bill then matches NOTHING — the opposite failure from
        the one this branch exists to fix, silent, and caused by the guard.

        ⚠️ EXCLUDED IN THE DOMAIN RATHER THAN PAGED, deliberately. Paging makes
        the number of queries a match costs depend on the data, and this runs on
        every import; a domain term keeps it at exactly one search per ownership
        pass. `test_control_an_eligible_vendor_inside_the_window_costs_the_same_
        searches` holds that.

        🔴 THIS IS A NARROWING, NOT THE GUARANTEE. `_ai_drop_impossible_vendors`
        still runs on whatever comes back and remains the authority — the domain
        cannot express the ancestry walk, and a rule the database can only
        approximate must not become the rule. Two things it deliberately does
        not cover, both of which the post-filter catches:

          * a partner whose ancestry reaches the company partner by a path
            `child_of` does not express;
          * rule 3 (a contact owned solely by another company), which the
            ownership passes already make unreachable, so no such row can be in
            the window at all.

        The user term is an id list rather than ``('user_ids', '=', False)``
        because the one2many form applies `res.users`' own active test and would
        silently let an ARCHIVED login through — an archived user is still one
        of our logins. The list is bounded by the number of logins, not by the
        number of partners: a few dozen at most on a real database.

        ⚠️ IT COSTS TWO SEARCHES PER CALL and is not cached, which is a choice
        rather than an oversight. Measured: `_ai_vendor_search` runs **5**
        `res.partner` searches in total — two ownership passes plus this
        method's `child_of` and login lookups — and `_ai_find_vendor` calls it
        up to three times. An `ormcache` would remove them and would go stale
        the moment somebody creates a contact or a login, which on this rule is
        the wrong way to be wrong. The path it sits on already spends ~60 s in a
        Claude call per document.
        """
        company = company or self.env.company
        refused_ids = set(self._ai_company_partner_tree_ids(company))
        refused_ids.update(
            self.env["res.users"]
            .sudo()
            .with_context(active_test=False)
            .search([])
            .partner_id.ids
        )
        if not refused_ids:
            return []
        return [("id", "not in", list(refused_ids))]

    @api.model
    def _ai_company_partner_tree_ids(self, company=None):
        """The company partner and every contact UNDER it, as ids.

        ⚠️ `child_of`, i.e. DOWNWARD, and that is not in tension with
        `_ai_partner_ancestry` walking upward. The two express the same rule
        from the two ends: a candidate is refused when ITS ancestry reaches the
        company partner, which is the same set as the company partner's own
        descendants. The ancestry walk is what a single partner can be tested
        with; this is what a domain can say.
        """
        company = company or self.env.company
        ours = self._ai_our_own_company_partners(company)
        if not ours:
            return []
        return (
            self.with_context(active_test=False)
            .search([("id", "child_of", ours.ids)])
            .ids
        )

    @api.model
    def _ai_seller_vat_domain(self, seller):
        """Step 1's domain. ONE body — the matcher and the refusal probe both
        use it, so "what the VAT step looks for" cannot be written twice."""
        vat = (seller or {}).get("vat")
        vat = "".join((vat if isinstance(vat, str) else "").split()).upper()
        if not vat or not _VAT_SHAPE.match(vat):
            return []
        return [("vat", "=ilike", like_literal(vat))]

    @api.model
    def _ai_seller_name_domain(self, seller):
        """Step 6's domain. ONE body, same reason as the VAT one.

        `parent_id = False`, never `is_company` — see `_ai_find_vendor` for the
        measurement behind that.
        """
        name = ((seller or {}).get("name") or "").strip()
        if not name:
            return []
        return [("name", "=ilike", like_literal(name)), ("parent_id", "=", False)]

    @api.model
    def _ai_vendor_refusal_for_seller(self, seller, company=None):
        """``(partner, reason)`` when the ONLY thing this seller resolves to is
        a party that can never be a vendor. ``(empty, False)`` otherwise.

        🔴 WHY A SEPARATE PROBE RATHER THAN A FLAG ON THE MATCHER. The matcher
        answers one question — *who is this vendor* — and "nobody" is a complete
        answer to it. The create-a-contact button asks a second question that
        the matcher deliberately does not: *is it empty because nothing matched,
        or because everything that matched was refused?* Those need opposite
        behaviour. Nothing matched → create the contact, which is the button's
        whole purpose. Everything refused → do NOT create, because a document
        whose seller block resolves to our own company would otherwise mint a
        SECOND copy of our own company as a vendor, which is this branch's bug
        wearing a new hat.

        It reuses `_ai_vendor_search` with ``drop=False`` and the same two
        domains the matcher's own steps 1 and 6 use, so there is no second
        spelling of what those steps look for.
        """
        company = company or self.env.company
        for domain in (
            self._ai_seller_vat_domain(seller),
            self._ai_seller_name_domain(seller),
        ):
            if not domain:
                continue
            for partner in self._ai_vendor_search(
                domain, company=company, drop=False, limit=None
            ):
                reason = self._ai_impossible_vendor_reason(partner, company=company)
                if reason:
                    return partner, reason
        return self.browse(), False

    @api.model
    def _ai_vendor_search(self, domain, company=None, order=None, limit=1, drop=True):
        """Search `domain` the required way: this company first, then shared.

        Refused candidates are dropped BEFORE the pass is judged, so a pass
        whose only hit is our own company partner falls through to the next
        pass rather than returning nothing — which is the difference between
        "no vendor" and "the wrong vendor", and on bill A the second
        pass is where the wrong answer came from.

        The generous inner cap exists because dropping happens after the
        search: a `limit=1` search that hits a refused row would report no
        match where a usable one sits behind it.

        ⚠️ ``drop=False`` IS FOR ONE CALLER AND IS NOT A WAY ROUND THE GUARD.
        `_ai_vendor_refusal_for_seller` needs to SEE what was refused in order
        to say why; it never returns the partner as a vendor. Nothing else
        passes it, and a caller that did would be re-opening the defect this
        whole branch exists to close.
        """
        company = company or self.env.company
        # 🔴 THE REFUSED ROWS ARE EXCLUDED BEFORE THE FETCH, NOT AFTER IT.
        # Dropping them afterwards let twenty refused rows eat the whole window
        # and lose a legitimate vendor behind them — see
        # `_ai_refused_vendor_domain`. With `drop=False` the caller is the
        # refusal PROBE and wants to see exactly those rows, so the narrowing
        # is skipped with it.
        narrowing = self._ai_refused_vendor_domain(company) if drop else []
        for extra in self._ai_company_ownership_passes(company):
            found = self.search(domain + extra + narrowing, order=order, limit=20)
            if drop:
                # Still the authority: the domain is an approximation of a rule
                # that walks a parent chain, and an approximation must not
                # become the rule.
                found = self._ai_drop_impossible_vendors(found, company=company)
            if found:
                return found[:limit] if limit else found
        return self.browse()

    @api.model
    def _ai_partner_ancestry(self, partner):
        """The partner, its commercial partner, and every parent UP the chain.

        🔴 UPWARD ONLY. `commercial_partner_id` is nearly always enough — it is
        the topmost `is_company` ancestor — but "nearly" is the whole problem:
        a child contact that itself carries ``is_company = True`` IS its own
        commercial partner, so a grandchild of the company partner resolves to
        the child and the company never appears. Walking `parent_id` is
        structural and does not care how the flags were set, which matters in a
        contact book where `is_company` is unset on a large share of the vendors
        (measured on a migrated test database).

        ⚠️ It must never walk DOWN. A vendor's own contacts are not the vendor's
        ancestry, and refusing a vendor because one of its possible child
        contacts holds a portal login is a bug, not a guard.

        The ``- seen`` is a cycle guard: `parent_id` is not constrained to be
        acyclic at the database level.
        """
        seen = self.browse()
        node = partner | partner.commercial_partner_id
        while node:
            seen |= node
            node = node.parent_id - seen
        return seen

    @api.model
    def _ai_impossible_vendor_reason(self, partner, company=None):
        """Why `partner` can never be the vendor on `company`'s document.

        Returns ``'own_company'``, ``'own_user'``, ``'other_company_contact'``
        or ``False`` — a reason rather than a bool, because the three cases
        deserve different wording and "this matched one of our own companies"
        sends somebody looking for the wrong thing when it was actually a login
        or a multi-company ownership rule.

        ⚠️ ``active_test=False`` on both lookups. An archived company's partner
        is still ours and an archived user is still one of our logins; the
        default search silently narrows both.
        """
        if not partner:
            return False
        company = company or self.env.company
        tree = self._ai_partner_ancestry(partner)
        if tree & self._ai_our_own_company_partners(company):
            return "own_company"
        has_user = (
            self.env["res.users"]
            .sudo()
            .with_context(active_test=False)
            .search_count([("partner_id", "in", tree.ids)], limit=1)
        )
        if has_user:
            return "own_user"
        # Rule 3: never a contact that belongs SOLELY to another
        # company. `company_id = False` is shared and allowed — which is what
        # makes the intercompany counterparty reachable.
        if partner.company_id and partner.company_id != company:
            return "other_company_contact"
        return False

    @api.model
    def _ai_refused_vendor_message(self, partner, reason):
        """The accountant-facing sentence for one refusal reason.

        🔴 ONE SENTENCE PER REASON, AND THAT IS NOT DECORATION. Core's `logs`
        end up in the bill's chatter, and telling somebody the document matched
        *"one of our own companies"* when it actually matched an employee's
        login, or a contact owned by the sister company, sends them looking for
        a problem that is not there. This project has made that exact mistake
        before, in `_ai_enforce_document_bank_side`, and it has its own note.

        Shared by both core seams so the two builds say the same thing.
        """
        tail = _(
            "The bill was left without a vendor; see the note in the chatter "
            "for what was read off the document and which existing vendors "
            "come closest."
        )
        if reason == "own_user":
            return _(
                "The details read off this document matched '%(partner)s', "
                "which is one of our own user accounts — so it was refused "
                "rather than filed against a login. %(tail)s",
                partner=partner.display_name,
                tail=tail,
            )
        if reason == "other_company_contact":
            return _(
                "The details read off this document matched '%(partner)s', a "
                "contact that belongs to another company in this database — so "
                "it was refused rather than filed across companies. %(tail)s",
                partner=partner.display_name,
                tail=tail,
            )
        return _(
            "The details read off this document matched '%(partner)s', which is "
            "this bill's own company — so it was refused rather than filed "
            "against ourselves. %(tail)s",
            partner=partner.display_name,
            tail=tail,
        )

    @api.model
    def _ai_drop_impossible_vendors(self, partners, company=None):
        """Rule 1 of `goodahead.partner.identifier.
        _drop_partners_that_are_never_a_vendor`, ON ITS OWN AND REUSABLE —
        plus the user rule added later.

        🔴 WHY IT IS SPLIT OUT, AND WHY NOT JUST CALLING THE PAIR. That method
        applies TWO rules: ours-is-never-a-vendor, and a partner the business
        has only ever SOLD to. The second is a judgement that belongs where it
        was written — below every exact identity key core has, inside this
        module's own phone and domain steps. Applied to CORE's answer it would
        also discard a core VAT match on a partner with `supplier_rank = 0` and
        `customer_rank > 0`, and a VAT is the strongest key an invoice carries.
        Only the not-a-judgement half travels.

        🔴 AND THE RULE WAS NOT MISSING — IT WAS IN THE ONE PATH CORE BYPASSES.
        `_drop_partners_that_are_never_a_vendor` has said *"We are never a
        vendor on our own bill. This is not a heuristic."* since 19.0.3.0.0, and
        it is measured against both company partners of a two-company database. But it runs
        only inside `goodahead.partner.identifier`, i.e. inside the three steps
        THIS module inserts — and core resolves a partner through its own VAT,
        Peppol-endpoint, bank-account-number, email, phone and name steps, none
        of which pass through it.

        That is how bill A was filed against the buyer company itself:
        the seller printed `+358400000444`, the same number the company partner
        carries, and core's legacy phone step
        (`account/models/partner.py:937` at `ca1f0aea`) returned it on the
        ``company_id = False`` pass — where it is the only row. Our guard never
        saw the answer, because our steps never ran: core had already decided.

        ⚠️ ONE `res.users` SEARCH FOR THE WHOLE RECORDSET, not one per partner.
        This runs on the candidate path with up to 60 partners in hand
        (`_ai_vendor_candidates` searches four name variants at `limit=20`), and
        a per-partner `search_count` there is 60 queries to answer one question.
        """
        if not partners:
            return partners
        company = company or self.env.company
        ours = self._ai_our_own_company_partners(company)
        ancestry = {
            partner.id: self._ai_partner_ancestry(partner) for partner in partners
        }
        reachable = self.browse()
        for tree in ancestry.values():
            reachable |= tree
        with_a_user = (
            self.env["res.users"]
            .sudo()
            .with_context(active_test=False)
            .search([("partner_id", "in", reachable.ids)])
            .partner_id
        )
        refused = ours | with_a_user
        return partners.filtered(
            lambda partner: not (ancestry[partner.id] & refused)
            # Rule 3. `company_id = False` is shared and allowed.
            and not (partner.company_id and partner.company_id != company)
        )

    # ------------------------------------------------------------------
    # 🔴 THE ONE VENDOR MATCHER — the dual-core design
    # ------------------------------------------------------------------

    @api.model
    def _ai_seller_from_context(self):
        """The whole extracted seller dict, as put on the context by
        `account.move._ai_decode_ubl_into_move`.

        🔴 WHY THE WHOLE DICT AND NOT CORE'S DECODED VALUES. This module is both
        the producer and the consumer of the XML: it generates the BIS 3.0 from
        an extraction and then decodes it. So the matcher never has to read
        whatever shape core happened to decode the document into — it can read
        the original seller dict, which is richer (it carries the website and
        the business id, for which Peppol has no slot core reads) and, far more
        to the point, is IDENTICAL on both of the two 19.0 import pipelines.
        That is what lets one matcher serve an adapter on either side.

        Returns `{}` — never None, never a raise — when nothing put a dict
        there. `res.partner._retrieve_partner_with_name` (the legacy adapter's
        seam, the dual-core design) is reachable outside our own import, and an adapter
        that raised on somebody else's document would be a defect introduced by
        a portability feature.

        ⚠️ The three single-key relays (`ai_seller_business_id`,
        `ai_seller_country_code`, `ai_seller_website`) are NOT replaced by this
        one. The search-plan steps below read them, they are what a genuine
        Peppol document arrives without, and changing what they do is not part
        of a hoist.
        """
        seller = self.env.context.get("ai_seller")
        return seller if isinstance(seller, dict) else {}

    @api.model
    def _ai_find_vendor(self, seller, company=None):
        """Find an existing vendor for the extracted seller. 🔴 NEVER CREATE ONE.

        🔴 THIS METHOD HAS NO CORE DEPENDENCY AT ALL, AND THAT IS ITS WHOLE
        POINT (the dual-core design). It reads the `seller` dict and this module's own
        models, calls nothing on `account.edi.*`, and is callable from an
        ordinary `self.env['res.partner']` with no document, no decoder and no
        pipeline. It moved here from `account.move._ai_match_existing_vendor`
        for one reason: on the older 19.0 core (`legacy`,
        ca1f0aea / nightly 20260118) NOTHING ever reaches the search-plan hooks
        this tree uses, so a matcher reachable only through them is dead code
        there — installed, green, never run, which is the exact silent no-op
        the dual-core design exists to prevent.

        The ordered search, strongest key first:

            1. VAT                      exact
            2. business id + country    exact, country-scoped
            3. phone                    E.164                  🆕 19.0.3.0.0
            4. email domain             host, non-public       🆕 19.0.3.0.0
            5. website domain           host, non-public       🆕 19.0.3.0.0
            6. name                     exact (``=ilike``)

        VAT and business id are matched independently — neither is derived from
        the other.

        🔴 WHY 3–5 EXIST, MEASURED. A sample set of seven vendor bills was put
        through this import: 7 of 7 extracted correctly and 6 of 7 created a
        duplicate vendor, because on an ordinary US invoice step 6 is the only
        step that can fire and the printed legal name is almost never the ledger
        name. On a typical US vendor book most suppliers carry a phone and about
        half an email, while almost none carry a bank account or a VAT — so
        phone and domain are the only keys that exist at scale here.

        🔴 A MATCH THAT IS NOT UNIQUE IS NOT A MATCH. Steps 3–5 return nothing
        when the normalised value resolves to more than one commercial partner;
        see `goodahead.partner.identifier._commercial_partner_for`. Falling
        through to the name step, and then to no vendor at all, is the correct
        outcome — an ambiguous match files the bill against the wrong company
        with confidence, and nothing downstream disagrees.

        🆕 19.0.3.6.0 — STEPS 2-4 ARE NOW THE SHARED BODY
        `_ai_vendor_from_seller_keys`, and 1 and 6 are deliberately NOT in it.
        This method is the composition for a caller with no core chain under it;
        the legacy adapter is the composition for core's name slot, where core
        owns VAT and name itself. Read that method's docstring before moving a
        rule between the two.

        ``company`` scopes the searches when it is given, and since 19.0.3.23.0
        steps 1 and 6 use it for the ownership ORDERING as well: this
        company's contacts first, then the shared ones, never another company's
        (see `_ai_company_ownership_passes`). ⚠️ It defaults to
        ``self.env.company`` rather than to "every company", because an
        unscoped search is what let a bill reach a Beta-owned contact
        — on a two-company database a share of the vendors belong to the second
        company.

        It also exists for the legacy adapter (the dual-core design), where core's own
        `_retrieve_partner` runs its steps twice — once company-scoped, once
        with `company_id = False`. ⚠️ The adapter answers that with
        `filtered_domain` on the caller's own `extra_domain` rather than by
        passing `company` down, because core's two passes differ in more than
        the company (the second asks for `company_id = False`, which
        `with_company` does not express).

        ⚠️ STEPS 2-5 ARE STILL NOT COMPANY-SCOPED, and that is unchanged on
        purpose. They resolve through `goodahead.partner.identifier`, which is
        not a `res.partner` search at all; the required ordering was asked of
        "the steps our module controls... where our own steps search
        `res.partner` directly", which is steps 1 and 6. Scoping the identifier
        table is a separate change with its own blast radius.

        Returns a res.partner (possibly empty), the established vendor over a
        recent duplicate.
        """
        seller = seller if isinstance(seller, dict) else {}
        Partner = self.env["res.partner"]
        if company:
            Partner = Partner.with_company(company)
        # Prefer an established supplier, then the oldest (over a recent duplicate).
        order = "supplier_rank desc, create_date asc, id asc"

        # 🔴 THE VAT AND NAME STEPS NEEDED THE OWN-COMPANY RULE TOO, AND THAT
        # IS 19.0.3.23.0. Steps 2-4 resolve through
        # `goodahead.partner.identifier`, which has applied it since
        # 19.0.3.0.0 — so the matcher LOOKED guarded and two of its six steps
        # were not. A document printing our own VAT (a swapped seller/buyer
        # block) or our own name reached straight past it.
        #
        # `_ai_vendor_search` carries BOTH the refusal and the ownership
        # ordering, so these two steps now agree with core's legacy chain
        # instead of being the one place in the module that searched every
        # company at once.
        vat_domain = self._ai_seller_vat_domain(seller)
        if vat_domain:
            match = Partner._ai_vendor_search(
                vat_domain, company=company, order=order
            )
            if match:
                return match

        # Steps 2-4, shared verbatim with the legacy name-slot adapter — see
        # `_ai_vendor_from_seller_keys`.
        match = self._ai_vendor_from_seller_keys(seller, company=company)
        if match:
            return match

        name = (seller.get("name") or "").strip()
        if name:
            # Name is a weak key: restrict to TOP-LEVEL partners so we never
            # assign an individual/child contact as the bill's vendor.
            #
            # 🔴 `is_company` IS NOT THE TEST, AND USING IT COST A LARGE SHARE OF
            # THE VENDOR BOOK. Measured on a migrated test database, of the partners with
            # `supplier_rank > 0` most are `is_company = True` and top-level,
            # and **almost a third are `is_company = False` and top-level** —
            # among them two busy vendors of the measured documents. The flag is
            # simply unset on a large part of a contact book that was imported
            # years ago; it says nothing about whether the row is a company.
            #
            # `parent_id = False` is the condition that was actually meant: a
            # child contact has a parent, a vendor does not. It excludes exactly
            # the rows the comment above was written to exclude, and excludes
            # nothing else. The same mistake was in `_ai_vendor_candidates`,
            # where it returned an EMPTY candidate list for `Northwind` — the one
            # case F27 was designed around — and it was invisible to every test,
            # because a fixture partner is created with `is_company = True`.
            match = Partner._ai_vendor_search(
                self._ai_seller_name_domain(seller),
                company=company,
                order=order,
            )
            if match:
                return match
        return Partner.browse()

    # ------------------------------------------------------------------
    # The matching policy (core/source split — ADR-0010, contracts §1.3)
    # ------------------------------------------------------------------

    @api.model
    def _ai_matching_policy(self, policy=None):
        """``policy``, else the one on the context, else the default one."""
        if policy is not None:
            return policy
        policy = self.env.context.get(MATCHING_POLICY_CONTEXT_KEY)
        return policy if isinstance(policy, MatchingPolicy) else DEFAULT_POLICY

    @api.model
    def _ai_vendors_by_key(self, key, seller, company=None):
        """Every vendor COMPANY one seller identifier points at.

        Contacts collapse into their commercial partner — a vendor company
        with three contacts that all carry its VAT number is one vendor, not
        four. Parties that can never be a vendor (our own
        companies, our logins, another company's contacts) are dropped, as on
        every other path of the matcher. Within a key the ownership
        ordering still holds: this company's contacts first, then shared ones
        (`_ai_vendor_search`)."""
        seller = seller if isinstance(seller, dict) else {}
        company = company or self.env.company
        Partner = self.env["res.partner"].with_company(company)
        Identifier = self.env["goodahead.partner.identifier"]
        found = Partner.browse()
        if key == KEY_VAT:
            domain = self._ai_seller_vat_domain(seller)
            if domain:
                found = Partner._ai_vendor_search(domain, company=company, limit=None)
        elif key == KEY_BUSINESS_ID:
            domain = self._ai_vendor_registry_domain(
                seller.get("business_id"),
                (seller.get("country_code") or "").strip()
                or self._vat_country_code(
                    "".join((seller.get("vat") or "").split()).upper()
                ),
            )
            if domain:
                found = Partner._ai_vendor_search(domain, company=company, limit=None)
        elif key == KEY_PHONE:
            phone = Identifier._normalise_phone(seller.get("phone"))
            if phone:
                found = Identifier.search(
                    [("kind", "=", "phone"), ("value", "=", phone)]
                ).partner_id
        elif key == KEY_DOMAIN:
            domains = [
                Identifier._normalise_domain(seller.get("email")),
                Identifier._normalise_domain(seller.get("website")),
            ]
            domains = [d for d in domains if d]
            if domains:
                found = Identifier.search(
                    [("kind", "=", "domain"), ("value", "in", domains)]
                ).partner_id
        elif key == KEY_NAME:
            domain = self._ai_seller_name_domain(seller)
            if domain:
                found = Partner._ai_vendor_search(domain, company=company, limit=None)
        vendors = found.commercial_partner_id.filtered("active")
        return self._ai_drop_impossible_vendors(vendors, company=company)

    @api.model
    def _ai_vendor_decision(self, seller, company=None, policy=None):
        """Who the vendor is under ``policy``, and who else to offer.

        Returns ``{"vendor", "candidates", "reason", "keys"}``:

        * ``vendor`` — the `res.partner` to set on the bill, or empty;
        * ``candidates`` — the vendor companies an ASSIGNING identifier pointed
          at when it could not decide (all of them, uncapped — AC-05);
        * ``reason`` — False, ``"ambiguous"`` (one identifier, several vendor
          companies) or ``"conflict"`` (VAT number and Business ID, say, name
          different vendor companies — AC-04b);
        * ``keys`` — the assigning identifiers that found anybody.

        🔴 THE DEFAULT POLICY IS `_ai_find_vendor`, CALLED AS IT ALWAYS WAS.
        The AI source passes it and its behaviour must not move:
        the established vendor wins a shared identifier there, and the first
        step that finds anybody decides. Only a non-default policy reaches the
        counting below. Never creates a vendor."""
        policy = self._ai_matching_policy(policy)
        Partner = self.env["res.partner"]
        seller = seller if isinstance(seller, dict) else {}
        decision = {
            "vendor": Partner.browse(),
            "candidates": Partner.browse(),
            "reason": False,
            "keys": [],
        }
        if policy.is_legacy:
            decision["vendor"] = self._ai_find_vendor(seller, company=company)
            return decision

        hits = []
        for key in ALL_KEYS:
            if not policy.assigns(key):
                continue
            vendors = self._ai_vendors_by_key(key, seller, company=company)
            if vendors:
                hits.append((key, vendors))
        decision["keys"] = [key for key, _vendors in hits]
        if not hits:
            return decision

        if policy.conflict == CONFLICT_FIRST_MATCH:
            key, vendors = hits[0]
            if len(vendors) == 1:
                decision["vendor"] = vendors
                return decision
            decision["candidates"] = vendors
            decision["reason"] = "ambiguous"
            decision["keys"] = [key]
            return decision

        every = Partner.browse()
        for _key, vendors in hits:
            every |= vendors
        if len(every) == 1:
            decision["vendor"] = every
            return decision
        decision["candidates"] = every
        # One identifier shared by several vendor companies is "ambiguous";
        # every identifier unique but naming different companies is "conflict".
        ambiguous = any(len(vendors) > 1 for _key, vendors in hits)
        decision["reason"] = "ambiguous" if ambiguous else "conflict"
        return decision

    # ------------------------------------------------------------------
    # The rule bodies. 🔴 ONE BODY PER RULE, AND THAT IS THE POINT.
    #
    # `_ai_find_vendor` above and the three search-plan steps below are two
    # DIFFERENT CALLERS of the same five rules, with two different jobs: the
    # matcher resolves a partner itself, the plan steps hand core a domain and
    # let core's plan runner resolve it with `limit=1`. Until 19.0.3.5.0 each
    # rule was written out twice, once per caller — which is the real
    # portability bug, because a fix applied to one spelling is invisible in the
    # other and nothing anywhere says the two disagree. They now share these
    # helpers, and `tests/test_vendor_matcher.py::TestOneBodyPerRule` stubs each
    # helper and asserts BOTH callers change their answer, so a reintroduced
    # second spelling fails a test instead of drifting quietly.
    #
    # ⚠️ What is deliberately NOT shared is where each caller gets its VALUES.
    # The plan steps read core's decoded `customer_values` first — they are
    # inserted into EVERY customer search plan, including a genuine Peppol
    # document's, where no `ai_seller*` context exists at all — and the matcher
    # reads the extraction. Collapsing that would change behaviour on documents
    # this module did not produce.
    # ------------------------------------------------------------------

    @api.model
    def _ai_vendor_from_seller_keys(self, seller, company=None):
        """The three rules this module ADDS to core's own chain: business id,
        then phone, then email/website domain. Returns a single `res.partner`,
        or an empty recordset. 🔴 NEVER CREATES, never raises on junk.

        🔴 WHY THIS IS A SEPARATE COMPOSITION FROM `_ai_find_vendor`, AND NOT A
        TIDYING-UP. The two have different jobs and therefore different
        contents:

        * `_ai_find_vendor` answers *"who is this vendor"* with no core step
          running at all — the post-decode safety net, and any caller holding
          only an extraction. It owns the VAT rule and the name rule because
          nothing else is going to run them.
        * THIS one answers *"who else might this be, at the point in CORE's
          chain where our keys belong"* — below every exact key core has, above
          core's name match. Core has already run its own VAT step by then (on
          the legacy core, twice, with prefix and zero-padding variants), and
          the name match at that position IS core's.

        Composing the whole matcher at a core seam is the trap the dual-core design walks
        into twice. Putting our `=ilike` VAT rule there retries, case-insensitively,
        a key core deliberately declined; putting our name rule there (exact
        `=ilike`, `parent_id = False`, ordered by `supplier_rank`) resolves an
        ambiguity core would have refused. Both change which vendor a bill is
        filed against, on one build only — which is precisely the divergence
        this whole feature exists to remove.

        ⚠️ So the rule for anyone adding a key: a rule goes in HERE when it
        belongs at core's name slot on both pipelines, and in `_ai_find_vendor`
        when it is ours to run because core will not.

        ``company`` scopes the registry search when given; the phone and domain
        rules resolve through `goodahead.partner.identifier`, which is not
        company-scoped — unchanged from 19.0.3.5.0, deliberately, because this
        is still a hoist.
        """
        seller = seller if isinstance(seller, dict) else {}
        Partner = self.env["res.partner"]
        if company:
            Partner = Partner.with_company(company)
        # Prefer an established supplier, then the oldest (over a recent duplicate).
        order = "supplier_rank desc, create_date asc, id asc"

        registry_domain = self._ai_vendor_registry_domain(
            seller.get("business_id"),
            (seller.get("country_code") or "").strip()
            or Partner._vat_country_code(
                "".join((seller.get("vat") or "").split()).upper()
            ),
        )
        # 🔴 THROUGH `_ai_vendor_search`, NOT A BARE `search` — PR #336 review
        # finding 1, and it was a real hole rather than a style point. This step
        # used to be the ONE path through the matcher that applied no refusal at
        # all: a seller printing a business id that matched the company
        # partner's `company_registry` was returned unfiltered to
        # `_create_partner_from_extraction`, to the create-contact button whose
        # docstring promises it cannot happen, AND to core through the legacy
        # name-slot adapter. The other five steps were covered — steps 1 and 6
        # by `_ai_vendor_search`, the phone and domain steps by
        # `goodahead.partner.identifier` — which is exactly why nothing noticed.
        if registry_domain:
            match = Partner._ai_vendor_search(
                registry_domain, company=company, order=order
            )
            if match:
                return match

        match = self._ai_vendor_by_phone(seller.get("phone"))
        if match:
            return self._ai_drop_impossible_vendors(match, company=company)

        # Email first, then the website — in practice the email is the one the
        # vendor prints on the invoice itself, and the website is the one that
        # caught Woodgrove and Contoso where the email did not (measured).
        match = self._ai_vendor_by_domains([seller.get("email"), seller.get("website")])
        if match:
            # ⚠️ FILTERED HERE TOO, THOUGH THE IDENTIFIER HELPERS ALREADY DROP
            # OUR OWN COMPANY PARTNERS. They do it against `self.env.company`,
            # because `goodahead.partner.identifier` is not company-scoped —
            # deliberately, and unchanged. The import runs in a queue job whose
            # `env.company` need not be the bill's, so without this the phone
            # and domain steps would be guarded against the WRONG company. One
            # extra filtered recordset of at most one row.
            return self._ai_drop_impossible_vendors(match, company=company)

        return Partner.browse()

    # ------------------------------------------------------------------
    # 🔴 THE LEGACY ADAPTER — the dual-core design. Live on one core, inert on the other,
    # by CORE's doing rather than by a flag of ours.
    # ------------------------------------------------------------------

    @api.model
    def _retrieve_partner_with_name(self, name, extra_domain):
        """Give our three keys a chance at the last slot of core's legacy chain.

        🔴 WHY THIS METHOD, AND NOT `_retrieve_partner`. On the legacy core
        (`ca1f0aea`, nightly deb 20260118) `_retrieve_partner`
        (`account/models/partner.py:958`) runs
        ``search_with_vat -> search_with_domain -> search_with_phone_mail ->
        search_with_name``, each twice — once company-scoped, once with
        ``company_id = False`` — and `:976` calls this method as the LAST live
        step. Hooking HERE puts our keys exactly where the search-plan adapter
        puts them on the other core: below every exact key core has, above
        core's name match. Hooking `_retrieve_partner` instead, as the source
        project's fork does, runs our keys AFTER `super()` has already settled
        on a name match — so there a printed name beats a business id, which is
        the duplicate-vendor defect the ordering exists to prevent.

        🔴 WHY IT IS INERT ON THE OTHER BUILD, AND WHY THAT IS NOT A PROFILE
        CHECK. On the search-plan core the same method is a deprecated stub with
        zero core callers, so this override cannot change anything there —
        `tests/test_legacy_name_slot.py` asserts that by MEASUREMENT, counting
        entries during a full import rather than reading the core for callers.
        A `core_profile` test here would add a second way to be wrong without
        adding a guarantee: the profile is already enforced UPSTREAM, at
        `account.move._ai_decode_ubl_into_move` and
        `ai.invoice.import._process_import`, which refuse outright on an
        unrecognised core — so `ai_invoice_import` can never be on the context
        of a build the probe did not recognise. And a profile check that is
        wrong fails CLOSED: the adapter would silently not run, which is exactly
        the silent no-op the dual-core design exists to prevent. A naturally-inert override
        cannot be wrong that way.

        ⚠️ SCOPED TO OUR OWN DOCUMENTS by the `ai_invoice_import` context key.
        This method is reachable from a genuine Peppol document, a Factur-X
        bill, or another module; a model's reading of a PDF is a different thing
        from a registered e-invoice, and core's behaviour there is not ours to
        change.

        🔴 NEVER WORSE THAN CORE, and that is three separate guarantees:
          1. We only ever RETURN EARLY on a match. Every other path is
             `super()`, unchanged, including the empty-name case where core
             returns None rather than a recordset.
          2. A match is filtered through the caller's own ``extra_domain``, so
             the company-scoped pass cannot be answered with an out-of-scope
             partner.
          3. `_ai_vendor_from_seller_keys` resolves to ONE partner or to
             nothing; it never hands back an ambiguous set for the caller's
             ``len(partner) == 1`` test to resolve by accident.

        ⚠️ It is called twice per import, once per ``extra_domain``. The rules
        are reads, so that is a cost rather than a correctness problem.

        🆕 19.0.3.8.0 — AND THEN THE NAME STEP ITSELF, because the two cores do
        not spell it the same way. See `_ai_retrieve_partner_with_exact_name`.
        ⚠️ The three guarantees above are about the THREE ADDED KEYS and are
        unchanged by it — but "never worse than core" does not carry over to the
        name step unaltered, because that step narrows core's own query and can
        therefore decide where the legacy caller refused. Both directions are
        set out there; do not read this heading as covering it.
        """
        if self.env.context.get("ai_invoice_import"):
            seller = self._ai_seller_from_context()
            if seller:
                partner = self._ai_vendor_from_seller_keys(seller)
                if partner and partner.filtered_domain(extra_domain or []):
                    return partner
            if name:
                return self._ai_retrieve_partner_with_exact_name(name, extra_domain)
        return super()._retrieve_partner_with_name(name, extra_domain)

    @api.model
    def _ai_retrieve_partner_with_exact_name(self, name, extra_domain):
        """Core's name step, spelled the way the OTHER core spells it.

        🔴 THE GAP THIS CLOSES, AND IT HAD NO STEP IN the dual-core design. *(measured
        on both benches)* core's name step differs between the two
        19.0 builds this module targets:

          * search-plan — `_import_retrieve_customer_from_name` searches
            ``[('name', '=ilike', name)]``: an EXACT, case-insensitive match
            (`account/models/partner.py:1013` at `189df353`).
          * legacy — `_retrieve_partner_with_name` searches
            ``[('name', 'ilike', name)]``: a SUBSTRING match
            (`account/models/partner.py:956` at `ca1f0aea`), which
            `_retrieve_partner` accepts whenever exactly one row comes back.

        So on the legacy build a printed name that is merely a SUBSTRING of one
        existing partner's name decides the bill. Nothing is invented — core
        MATCHED it — and that is why the dual-core design cannot reach it: suppressing a
        create cannot suppress a match. 🔴 And because this module's whole
        recovery path (the banner, the candidate cards, the chatter note, the
        learning) is gated on an EMPTY ``partner_id``, a wrong match does not
        add a row, it SILENCES ALL OF IT. F27's promise — *"no vendor is
        invented, and an unrecognised one is visible"* — is simply not kept on
        half the installed base, under the binding rule that a vendor is never
        invented.

        🔴 WHY THE ANSWER IS "NARROW IT" AND NOT "REJECT IT AFTERWARDS".
        Rejecting after ``super()`` cannot work: core searches with ``limit=2``
        and `_retrieve_partner` accepts only ``len(partner) == 1``, so by the
        time a result comes back the information needed to tell an exact match
        from a substring one has already been thrown away — and a genuine exact
        match can be crowded out of those two rows by substring matches it
        never competed with. The decision has to be narrowed where it is MADE.

        🔴 NOTHING IS LOST, AND THAT IS CHECKABLE. Every partner core's
        substring step could have matched is already in the set
        `account.move._ai_vendor_candidates` offers under *"similar name"* —
        it searches the same ``('name', 'ilike', name)``. So this does not
        discard the resemblance, it moves it from a SILENT DECISION to a
        VISIBLE CANDIDATE, which is the vocabulary F27 already put in this
        module. `tests/test_legacy_name_slot.TestASubstringNameIsNotAVendor`
        asserts both halves, ungated, with the same expected outcome on both
        benches.

        ⚠️ AND IT WAS A SUPERSET CLAIM THAT DID NOT HOLD UNTIL 19.0.3.17.0 (the dual-core design
        review, finding W6). The candidate search carried
        ``("parent_id", "=", False)`` and core's legacy step does not, so a
        CHILD CONTACT whose name merely contains the printed name failed both
        halves at once: ``=ilike`` stopped matching it and the candidate list
        excluded it. The quantifier said *every* and meant *every top-level
        one*. `_ai_vendor_candidates` now runs a second, separately-capped
        search for child contacts — see the note there for why a second search
        and not a wider one, and for why being OFFERED is not being CHOSEN.

        🔴 WHAT THIS CHANGES, IN BOTH DIRECTIONS — AND THE PARAGRAPH THAT STOOD
        HERE ONLY GAVE ONE OF THEM (found in review). It said:
        *"``=ilike`` matches a strict SUBSET of the rows ``ilike`` matches, so
        this can never return a partner core would not also have found. What it
        can do is DECLINE where core would have matched."* The set statement is
        right. The behavioural conclusion was half an answer, and it was
        load-bearing in the argument for shipping this unguarded.

          1. **It declines where the legacy core matched.** The Northwind case: a
             printed name that merely OCCURS INSIDE one ledger partner's name no
             longer decides the bill. This is the direction that paragraph gave.
          2. **It can also DECIDE where the legacy core refused.** The caller
             accepts only ``len(partner) == 1`` (`:992` at `ca1f0aea`), so
             shrinking the result set turns *two rows → declined* into *one row
             → accepted*. *(measured)* with `ZZ Zeta Test Oy` and
             `ZZ Zeta Test Oy Branch` in the ledger and `ZZ Zeta Test Oy` on the
             bill, the legacy core's substring step returns BOTH and declines;
             this returns ONE and decides.

        🔴 AND (2) IS CORRECT, WHICH IS THE WHOLE POINT OF THE FEATURE. The
        question at a shared seam is never *does this differ from core* — it is
        *do the two builds now answer the same*. *(measured on the
        search-plan bench)* that core's `_import_retrieve_customer_from_name` is
        ``('name', '=ilike', name)`` and `_import_retrieve_customer` searches
        ``limit=1`` under ``order='is_company DESC, supplier_rank DESC,
        company_id, parent_id DESC, id DESC'`` (`:1006`, `:1046-1053` at
        `189df353`). It does not refuse an ambiguity at all; it picks one, and
        on that fixture it picks `ZZ Zeta Test Oy`. So this does not resolve
        something *core* refused — it resolves something the LEGACY core refused
        and the SEARCH-PLAN core has always decided the same way, and making it
        decline instead would have put a NEW divergence in the one place this
        module exists to remove one.

        ⚠️ ONE DISAGREEMENT REMAINS, DELIBERATELY (plan §12, U11). With TWO rows
        matching the printed name EXACTLY this hands both back and the caller
        declines, while the search-plan core picks the higher `supplier_rank`.
        That is the safe direction — declining lands the bill in F27's candidate
        cards, where a person chooses and the choice teaches the matcher —
        and closing it would mean composing an ORDER here, which is exactly what
        the note below forbids. `test_legacy_name_slot.
        test_two_partners_with_the_SAME_exact_name_are_still_refused` pins it.

        ⚠️ WHAT IS DELIBERATELY NOT COMPOSED HERE, for the fourth time in this
        plan (§4.3(a), §4.3(b), §6, and now this). This is CORE's name step,
        not ours. `_ai_find_vendor`'s name rule also carries
        ``parent_id = False`` and a ``supplier_rank``-first order; putting
        either of those here would resolve, on one build only, an ambiguity the
        other build's core refuses — which is the divergence this feature
        exists to remove. The ONLY thing that changes is the operator.
        ``limit=2`` and the ``extra_domain`` are core's and stay core's,
        because `_retrieve_partner`'s ``len(partner) == 1`` test reads them.

        ⚠️ SCOPED TO OUR OWN DOCUMENTS, by the caller's ``ai_invoice_import``
        guard. A registered e-invoice arriving over Peppol is not a model's
        reading of a PDF, and core's name semantics for those are not ours to
        change.

        🔴 ``%`` and ``_`` in the printed name are taken LITERALLY (security
        fix). The search-plan core's own name step treats them as wildcards;
        here that would let a document printing ``%`` match every partner.
        A vendor name that really contains either character still matches
        itself exactly.
        """
        return self.env["res.partner"].search(
            [("name", "=ilike", like_literal(name))] + list(extra_domain or []), limit=2
        )

    @api.model
    def _ai_vendor_registry_domain(self, business_id, country_code):
        """The domain matching a vendor on its national business id, or None.

        The country scope is not optional. A national business id is only
        unique within its country, so without one this would be a free-text
        match on a short numeric string across every partner in the database.
        No country, no match.
        """
        business_id = (business_id or "").strip()
        country_code = (country_code or "").strip()
        if not business_id or not country_code.isascii() or not country_code.isalpha():
            return None
        return [
            ("company_registry", "in", self._registry_variants(business_id)),
            ("country_id.code", "=ilike", country_code),
        ]

    @api.model
    def _ai_vendor_by_phone(self, raw_phone):
        """The ONE commercial partner a printed phone number resolves to.

        The number is reduced to its last ten digits through
        `goodahead.partner.identifier._normalise_phone`, and compared against
        the identifier rows derived the same way from every partner's own
        `phone` field — because core's own phone step is exact string equality,
        so `212.555.0111` does not match the record holding `+1 212-555-0111`,
        although it is the same telephone. Measured: that is one of the six
        duplicates in the sample set.

        ⚠️ IT TOOK A ``country_code`` UNTIL 19.0.3.22.0 and does not any more:
        the country-aware E.164 key was withdrawn with its migration. See
        `_normalise_phone`.

        Returns an EMPTY recordset when the number resolves to more than one
        commercial partner. The uniqueness is resolved here rather than in a
        domain because core runs a plan step's domain with ``limit=1``, which
        would pick one of several ambiguous vendors and never say so.
        """
        Identifier = self.env["goodahead.partner.identifier"]
        return Identifier._commercial_partner_for(
            "phone", Identifier._normalise_phone(raw_phone)
        )

    @api.model
    def _ai_vendor_by_domains(self, raw_values):
        """The ONE commercial partner the first usable web domain resolves to.

        `raw_values` is tried in order and may hold an email address, a URL or a
        bare host; each is reduced to its domain and the first that resolves
        uniquely wins. Core's own email step is exact string equality too, so
        `purchasing@vendor.com` does not find the record holding
        `sales@vendor.com` — the same company, which is why the match is on
        the email domain.

        A free-provider domain is never a match — `_normalise_domain` returns
        None for it — and an ambiguous one is never a match either.
        """
        Identifier = self.env["goodahead.partner.identifier"]
        seen = []
        for raw in raw_values:
            domain = Identifier._normalise_domain(raw)
            if not domain or domain in seen:
                continue
            seen.append(domain)
            partner = Identifier._commercial_partner_for("domain", domain)
            if partner:
                return partner
        return self.browse()

    # 🔴 `_retrieve_partner` IS NOT OVERRIDDEN HERE, ON EITHER CORE.
    #
    # The source project overrides it to add a business-id fallback after
    # `super()`. That is the wrong seam twice over. On the core this tree runs
    # (`odoo/odoo` at the deployed revision `189df353`) it is never
    # reached during a UBL import at all: it has become a thin legacy wrapper
    # that itself delegates to `_import_retrieve_customer`, and the import
    # pipeline calls `_import_retrieve_customer` directly with a search plan —
    # so an override there installs cleanly, passes every gate and never runs
    # once. And on the LEGACY core, where it is the live implementation, running
    # after `super()` means a printed name has already won.
    #
    # So the same three keys are expressed twice, at the same RELATIVE position
    # on each build and over one shared body (`_ai_vendor_from_seller_keys`):
    # as plan steps below, inserted by
    # `account.edi.ubl._import_ubl_retrieve_customer_search_plan`, and as the
    # `_retrieve_partner_with_name` override above.

    @api.model
    def _import_retrieve_customer_from_ai_business_id(self, customer_values):
        """Search-plan step: match a vendor on its national business id.

        An invoice usually carries BOTH a VAT number and a national
        registration number, and the two are independent — neither is derived
        from the other. A vendor stored with the business id in
        ``company_registry`` but a different or absent VAT is matched by
        nothing in core's plan except the ``ilike`` name step, so every bill
        from them mints a duplicate vendor.

        The business id is read from the context (``ai_seller_business_id``),
        not from ``customer_values``. That is not an oversight:
        `adapters/ubl_generator.py` does not emit the id into the XML at all —
        Peppol's place for it is ``PartyLegalEntity/CompanyID``, and core reads
        the first ``.//CompanyID`` under the party as the VAT, so emitting it
        there risks the VAT being read as the registration number on documents
        where ``PartyTaxScheme`` is absent. `account.move._ai_decode_ubl_into_move`
        therefore puts the extracted value on the context before the decoder is
        resolved, and the decoder inherits that env.

        The country scope is not optional. A national business id is only
        unique within its country, so without one this would be a free-text
        match on a short numeric string across every partner in the database.
        No country, no match.

        Returns core's plan-step shape: None, or ``{'criteria': [...]}``.

        🆕 19.0.3.5.0 — the RULE now lives in `_ai_vendor_registry_domain`, so
        this step and `_ai_find_vendor` cannot drift apart. Where the two VALUES
        come from is still different on purpose: the business id can only reach
        this step on the context, and the country falls back to the one implied
        by core's decoded VAT.
        """
        ctx = self.env.context
        domain = self._ai_vendor_registry_domain(
            ctx.get("ai_seller_business_id"),
            (ctx.get("ai_seller_country_code") or "").strip()
            or self._vat_country_code(customer_values.get("vat")),
        )
        if not domain:
            return None
        return {"criteria": [{"domain": domain}]}

    @api.model
    def _import_retrieve_customer_from_ai_phone(self, customer_values):
        """Search-plan step: match a vendor on a NORMALISED phone number.

        🔴 CORE'S PHONE STEP IS EXACT STRING EQUALITY AND THAT IS WHY THIS
        EXISTS. `account/models/partner.py:982-992` is
        ``{'domain': [('phone', '=', phone)]}`` — so the Bluebird invoice
        printing `212.555.0111` does not match the record holding
        `+1 212-555-0111`, although it is the same telephone. Measured: that is
        one of the six duplicates in the sample set.

        The number is compared on its last ten digits, through
        `goodahead.partner.identifier._normalise_phone`, against the identifier
        rows seeded from every partner's own `phone` field, which are derived by
        that same function. ⚠️ It was E.164 between 19.0.3.11.0 and 19.0.3.22.0,
        and this sentence is deliberately the one that says which shape is
        live: a review finding was this very docstring left asserting
        the old rule after the sibling `_ai_vendor_by_phone` had been updated,
        which is this module's own standing note — a wrong claim in a docstring
        outlives the person who wrote it.

        It returns NOTHING when the number resolves to more than one commercial
        partner — see `_commercial_partner_for`. The uniqueness is resolved here
        rather than in the domain because core runs a plan step's domain with
        ``limit=1``, which would pick one of several ambiguous vendors and never
        say so.

        🆕 19.0.3.5.0 — the RULE now lives in `_ai_vendor_by_phone`, shared with
        `_ai_find_vendor`. The VALUE still comes from core's decoded
        `customer_values` first: this step is inserted into every customer
        search plan, including a genuine Peppol document's, where there is no
        extraction on the context to read.
        """
        partner = self._ai_vendor_by_phone(
            customer_values.get("phone") or self.env.context.get("ai_seller_phone")
        )
        if not partner:
            return None
        return {"criteria": [{"domain": [("id", "=", partner.id)]}]}

    @api.model
    def _import_retrieve_customer_from_ai_domain(self, customer_values):
        """Search-plan step: match a vendor on the DOMAIN of its email or website.

        🔴 CORE'S EMAIL STEP IS EXACT STRING EQUALITY TOO
        (`account/models/partner.py:994-1004`:
        ``{'domain': [('email', '=', email)]}``), so `purchasing@vendor.com` on
        the invoice does not find the record holding `sales@vendor.com` — the
        same company, which is why the match is on the email domain.

        The website is read off the CONTEXT (`ai_seller_website`) and not off
        `customer_values`, because UBL has no slot for a party's website that
        core reads, and inventing one would mean emitting an element nothing
        decodes. `account.move._ai_decode_ubl_into_move` puts it on the context
        before the decoder is resolved, exactly as it already does for the
        business id. Email arrives normally, through `cac:Contact` /
        `cbc:ElectronicMail`, which core maps at
        `account_edi_ubl_cii/models/account_edi_ubl.py:2659`.

        A free-provider domain is never a match — `_normalise_domain` returns
        None for it — and an ambiguous one is never a match either.

        🆕 19.0.3.5.0 — the RULE now lives in `_ai_vendor_by_domains`, shared
        with `_ai_find_vendor`, email before website in both.
        """
        partner = self._ai_vendor_by_domains(
            [
                customer_values.get("email"),
                self.env.context.get("ai_seller_website"),
            ]
        )
        if not partner:
            return None
        return {"criteria": [{"domain": [("id", "=", partner.id)]}]}

    @api.model
    def _registry_variants(self, business_id):
        """Format variants of the SAME business id so it matches whether the stored
        ``company_registry`` keeps separators or not (e.g. ``0000007-8`` vs
        ``00000078``, ``HRB 12345`` vs ``HRB12345``). Purely formatting — it never
        converts between a VAT and a business id."""
        business_id = (business_id or "").strip()
        if not business_id:
            return []
        variants = {business_id, business_id.replace(" ", "").replace(".", "")}
        alnum = "".join(ch for ch in business_id if ch.isalnum())
        if alnum:
            variants.add(alnum)
        return list(variants)

    @api.model
    def _vat_country_code(self, vat):
        """ISO country code from a prefixed VAT (``DE811234567`` -> ``DE``,
        ``ATU12345678`` -> ``AT``), or None when there is no 2-letter country
        prefix followed by an alphanumeric body containing at least one digit."""
        vat = (vat or "").strip().upper().replace(" ", "")
        if len(vat) < 3 or not vat[:2].isalpha():
            return None
        body = vat[2:]
        if body.isalnum() and any(ch.isdigit() for ch in body):
            return vat[:2]
        return None
