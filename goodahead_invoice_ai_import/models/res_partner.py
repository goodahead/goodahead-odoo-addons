# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import _, api, fields, models

# The fields an identifier can be derived from. `res.partner.write` is a hot
# path, so the override below does nothing unless one of these keys is in vals.
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
        """Add the identifiers derivable from phone/email/website.

        Rows are only ever added, never replaced or deleted: a row records that
        the partner was once reachable that way. A stale row costs at most an
        ambiguous lookup (which declines); a deleted one costs a duplicate vendor.
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
    # Parties that can never be the vendor on a bill, scoped to the bill's
    # company: (1) the company partner and its contacts, (2) any partner with
    # an internal user, (3) a contact owned solely by another company. Another
    # company's shared partner is allowed, which keeps intercompany billing
    # working.
    # Rules 1 and 2 walk the ancestry upward only: walking down would refuse a
    # vendor because one of its contacts has a portal login.
    # ------------------------------------------------------------------

    @api.model
    def _ai_our_own_company_partners(self, company=None):
        """The partner of one company (the bill's), archived or not.

        Callers with a document should pass its company: the import runs in a
        queue job whose ``env.company`` may differ.
        """
        if company:
            return company.sudo().with_context(active_test=False).partner_id
        return (
            self.env.company.sudo().with_context(active_test=False).partner_id
        )

    @api.model
    def _ai_company_ownership_passes(self, company=None):
        """Search passes: this company's contacts first, then shared ones."""
        company = company or self.env.company
        return (
            [*self._check_company_domain(company), ("company_id", "!=", False)],
            [("company_id", "=", False)],
        )

    @api.model
    def _ai_refused_vendor_domain(self, company=None):
        """Domain terms that keep refused parties out of the search itself.

        Filtering only after a limited fetch would let refused rows fill the
        window and hide a legitimate vendor behind them. This is a narrowing;
        ``_ai_drop_impossible_vendors`` stays the authority. User partners are
        an id list because ``user_ids`` would skip archived users.
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
        """Ids of the company partner and every contact under it."""
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
        """Domain of the VAT step, shared by the matcher and the refusal probe."""
        vat = "".join(((seller or {}).get("vat") or "").split()).upper()
        return [("vat", "=ilike", vat)] if vat else []

    @api.model
    def _ai_seller_name_domain(self, seller):
        """Domain of the name step, top-level partners only.

        ``parent_id = False`` rather than ``is_company``, which is often unset
        on imported vendors.
        """
        name = ((seller or {}).get("name") or "").strip()
        if not name:
            return []
        return [("name", "=ilike", name), ("parent_id", "=", False)]

    @api.model
    def _ai_vendor_refusal_for_seller(self, seller, company=None):
        """Return ``(partner, reason)`` when the seller only resolves to a refused party.

        Distinguishes "nothing matched" (creating a contact is fine) from
        "everything that matched was refused" (creating one would duplicate our
        own company as a vendor). Returns ``(empty, False)`` otherwise.
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
        """Search ``domain`` in this company's contacts first, then shared ones.

        Refused candidates are dropped before a pass is judged, so a pass whose
        only hit is refused falls through to the next one. ``drop=False`` is
        only for the refusal probe, which never returns the partner as a vendor.
        """
        company = company or self.env.company
        narrowing = self._ai_refused_vendor_domain(company) if drop else []
        for extra in self._ai_company_ownership_passes(company):
            found = self.search(domain + extra + narrowing, order=order, limit=20)
            if drop:
                # The domain only approximates the ancestry rule; this is exact.
                found = self._ai_drop_impossible_vendors(found, company=company)
            if found:
                return found[:limit] if limit else found
        return self.browse()

    @api.model
    def _ai_partner_ancestry(self, partner):
        """The partner, its commercial partner, and every parent up the chain.

        ``commercial_partner_id`` is not enough when a child contact itself has
        ``is_company`` set. ``- seen`` guards against parent cycles.
        """
        seen = self.browse()
        node = partner | partner.commercial_partner_id
        while node:
            seen |= node
            node = node.parent_id - seen
        return seen

    @api.model
    def _ai_impossible_vendor_reason(self, partner, company=None):
        """Why ``partner`` can never be the vendor on ``company``'s document.

        Returns ``'own_company'``, ``'own_user'``, ``'other_company_contact'``
        or ``False``; each reason gets its own user-facing message.
        """
        if not partner:
            return False
        company = company or self.env.company
        tree = self._ai_partner_ancestry(partner)
        if tree & self._ai_our_own_company_partners(company):
            return "own_company"
        # Internal users only: a vendor with a portal login (purchase portal)
        # is still a vendor.
        has_user = (
            self.env["res.users"]
            .sudo()
            .with_context(active_test=False)
            .search_count(
                [("partner_id", "in", tree.ids), ("share", "=", False)], limit=1
            )
        )
        if has_user:
            return "own_user"
        # A shared contact (no company) is allowed; another company's own is not.
        if partner.company_id and partner.company_id != company:
            return "other_company_contact"
        return False

    @api.model
    def _ai_refused_vendor_message(self, partner, reason):
        """The accountant-facing message for one refusal reason."""
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
        """Drop partners that can never be a vendor on ``company``'s documents.

        Unlike ``goodahead.partner.identifier._drop_partners_that_are_never_a_vendor``
        this does not drop customer-only partners, so it is safe to apply to
        core's own matches (e.g. a VAT match). Uses a single ``res.users``
        search for the whole recordset.
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
            # A shared contact (no company) is allowed.
            and not (partner.company_id and partner.company_id != company)
        )

    @api.model
    def _ai_seller_from_context(self):
        """The extracted seller dict put on the context by the decode, or ``{}``.

        Reading the extraction rather than core's decoded values keeps the
        matcher identical on both import pipelines. Never raises, as callers
        are reachable outside our own imports.
        """
        seller = self.env.context.get("ai_seller")
        return seller if isinstance(seller, dict) else {}

    @api.model
    def _ai_find_vendor(self, seller, company=None):
        """Find an existing vendor for the extracted seller; never create one.

        Independent of the EDI pipeline, so it works on either core import
        pipeline. Steps, strongest key first: VAT, business id + country, normalised
        phone, email domain, website domain, exact name. A key that resolves
        to more than one commercial partner is not a match. VAT and name
        searches use this company's contacts first, then shared ones.

        Returns a res.partner (possibly empty), preferring an established
        vendor over a recent duplicate.
        """
        seller = seller if isinstance(seller, dict) else {}
        Partner = self.env["res.partner"]
        if company:
            Partner = Partner.with_company(company)
        # Prefer an established supplier, then the oldest (over a recent duplicate).
        order = "supplier_rank desc, create_date asc, id asc"

        vat_domain = self._ai_seller_vat_domain(seller)
        if vat_domain:
            match = Partner._ai_vendor_search(
                vat_domain, company=company, order=order
            )
            if match:
                return match

        match = self._ai_vendor_from_seller_keys(seller, company=company)
        if match:
            return match

        name = (seller.get("name") or "").strip()
        if name:
            # Name is a weak key: top-level partners only, so a child contact
            # is never assigned as the bill's vendor.
            match = Partner._ai_vendor_search(
                self._ai_seller_name_domain(seller),
                company=company,
                order=order,
            )
            if match:
                return match
        return Partner.browse()

    # ------------------------------------------------------------------
    # Rule bodies shared by the matcher above and the search-plan steps
    # below, so a fix cannot land in one spelling only. The plan steps read
    # core's decoded values first because they also run on genuine Peppol
    # documents, where no extraction is on the context.
    # ------------------------------------------------------------------

    @api.model
    def _ai_vendor_from_seller_keys(self, seller, company=None):
        """Match on the keys this module adds: business id, phone, email/web domain.

        These belong at core's name slot on both pipelines. VAT and name are
        not here because core already runs its own versions at that point.
        Returns one partner or an empty recordset; never creates.
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
        if registry_domain:
            match = Partner._ai_vendor_search(
                registry_domain, company=company, order=order
            )
            if match:
                return match

        match = self._ai_vendor_by_phone(seller.get("phone"))
        if match:
            return self._ai_drop_impossible_vendors(match, company=company)

        # Email first, then the website.
        match = self._ai_vendor_by_domains([seller.get("email"), seller.get("website")])
        if match:
            # The identifier helpers filter against env.company; filter again
            # against the bill's company, which may differ in a queue job.
            return self._ai_drop_impossible_vendors(match, company=company)

        return Partner.browse()

    @api.model
    def _retrieve_partner_with_name(self, name, extra_domain):
        """Give our keys a chance at the last slot of core's chain.

        Core calls this as the last step of ``_retrieve_partner``, once
        company-scoped and once for shared partners, so our keys land below
        core's exact keys (VAT, Peppol pair, phone/email) and above its name
        match. Only for AI decodes; a match is returned only if it satisfies
        the caller's ``extra_domain``.
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
        """Core's name step with ``=ilike`` instead of ``ilike``.

        Core matches names as a substring and accepts a single hit, so a
        printed name contained in one partner's name would decide the bill
        silently. AI decodes use an exact case-insensitive match instead;
        substring look-alikes are still offered as candidates on the bill.
        ``limit=2`` and ``extra_domain`` stay as core's, since the caller
        accepts only a single result.
        """
        return self.env["res.partner"].search(
            [("name", "=ilike", name)] + list(extra_domain or []), limit=2
        )

    @api.model
    def _ai_vendor_registry_domain(self, business_id, country_code):
        """The domain matching a vendor on its national business id, or None.

        A business id is only unique within its country, so no country means
        no match.
        """
        business_id = (business_id or "").strip()
        country_code = (country_code or "").strip()
        if not business_id or not country_code:
            return None
        return [
            ("company_registry", "in", self._registry_variants(business_id)),
            ("country_id.code", "=ilike", country_code),
        ]

    @api.model
    def _ai_vendor_by_phone(self, raw_phone):
        """The single commercial partner a printed phone number resolves to.

        Core's phone step is exact string equality; this compares normalised
        numbers through the identifier table. Returns an empty recordset when
        the number is ambiguous.
        """
        Identifier = self.env["goodahead.partner.identifier"]
        return Identifier._commercial_partner_for(
            "phone", Identifier._normalise_phone(raw_phone)
        )

    @api.model
    def _ai_vendor_by_domains(self, raw_values):
        """The single commercial partner the first usable web domain resolves to.

        ``raw_values`` may hold emails, URLs or bare hosts, tried in order.
        Free-provider and ambiguous domains never match.
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

    # `_retrieve_partner` is deliberately not overridden: running after
    # `super()` would let a name match win. The keys are hooked through
    # `_retrieve_partner_with_name` above (and as search-plan steps below,
    # which only a 19.0 core calls).

    @api.model
    def _import_retrieve_customer_from_ai_business_id(self, customer_values):
        """Search-plan step: match a vendor on its national business id.

        The id is read from the context (``ai_seller_business_id``) because the
        generated UBL does not carry it: core would read ``CompanyID`` as the
        VAT. Returns core's plan-step shape: None or ``{'criteria': [...]}``.
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
        """Search-plan step: match a vendor on a normalised phone number.

        Resolved to a single partner here because core runs a step's domain
        with ``limit=1`` and would silently pick one of several.
        """
        partner = self._ai_vendor_by_phone(
            customer_values.get("phone") or self.env.context.get("ai_seller_phone")
        )
        if not partner:
            return None
        return {"criteria": [{"domain": [("id", "=", partner.id)]}]}

    @api.model
    def _import_retrieve_customer_from_ai_domain(self, customer_values):
        """Search-plan step: match a vendor on the domain of its email or website.

        The website comes from the context (``ai_seller_website``) because UBL
        has no party website element that core reads.
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
        ``company_registry`` keeps separators or not (e.g. ``1234567-8`` vs
        ``12345678``, ``HRB 12345`` vs ``HRB12345``). Purely formatting — it never
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
