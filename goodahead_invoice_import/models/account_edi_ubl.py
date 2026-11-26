# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: Set by `goodahead.invoice.import._import_decode_context` for a structured
#: reader: its lines read their units through the shared unit codes.
EINVOICE_UNIT_CODES_CONTEXT_KEY = "einvoice_unit_codes"


class AccountEdiUbl(models.AbstractModel):
    """The hooks of the NEW UBL import pipeline.

    🔴 WHICH PIPELINE THIS TREE RUNS, and how to check it yourself.

    19.0 core reworked UBL import in the spring of 2026. Before that,
    `account.edi.xml.ubl_bis3` decoded through `account.edi.common`
    (`_import_partner_bank`, `_import_lines`, `_import_product`) and matched
    partners through `res.partner._retrieve_partner`. After it,
    `_import_invoice_ubl_cii` routes to `account.edi.ubl._ubl_import_invoice`,
    which calls none of those — it drives a sequence of
    `_import_ubl_*(collected_values)` steps and resolves partners and products
    through SEARCH PLANS instead.

    The US deployment runs the new one.

    🔴 THE ONLY CORE THAT MATTERS IS THE DEPLOYED REVISION, and it is a pin
    anyone can read: `189df3538aa07d22f683008522b9143421f411a2` (2026-06-23). The image
    `COPY`s a checkout of `odoo/odoo` at that revision over the Debian package
    it installed, after deleting the package's own `odoo` Python tree and its
    bundled addons — so **none of the deb's Python is what runs**; it supplies dependencies only. At that revision,
    `account_edi_xml_ubl_bis3._import_invoice_ubl_cii` ends
    `return self._ubl_import_invoice(...)`.

    ⚠️ An earlier revision of this file dated the core from the deb's version
    string and concluded the deployed core differed from the development one.
    That was wrong in both directions: the deb's code is deleted during the
    build, and a development configuration overlay is at 2026-08-06 and is
    **identical to the deployed revision** for everything this module touches —
    both search plans, and the bank hook. Check the deployed revision; do not
    infer a core from a package version.

    An older deployment runs an older nightly and is on the OLD pipeline —
    the two projects are mirror images here. That is why `account_edi_common.py`
    next door still exists and is still correct, and why everything in THIS file
    is the half that fires for us.
    """

    _inherit = "account.edi.ubl"

    def _import_ubl_retrieve_partner_bank(self, collected_values):
        """Never read a received credit note's bank account as one of ours.

        Core decides whose account the file's IBAN is from ``move_type`` alone,
        and puts ``in_refund`` with ``out_invoice`` — the two documents on which
        we are the payee, so the account must be ours. That holds for a credit
        note we issue. It is wrong for every one we receive: a supplier's credit
        note carries the supplier's account, copied from the invoice it
        reverses.

        Left alone, the importer goes looking for the supplier's IBAN under our
        own partner. Either it finds nothing and logs a refusal, or — where an
        earlier import already left that IBAN on the company partner — it finds
        it and writes it to ``partner_bank_id``, so the bill states a vendor's
        account where the company's belongs.

        So the account is recorded against the vendor, where it belongs and is
        worth having, and ``partner_bank_id`` is left to
        ``_compute_partner_bank_id``: on an ``in_refund`` ``bank_partner_id`` is
        our own partner, which is exactly the account the money comes back to.

        Only ``in_refund`` is DIVERTED. Everything else — including an
        ``out_invoice`` we genuinely issue — keeps core's behaviour.

        🔴 THE TWO GUARDS BELOW ARE WIDER THAN THE DIVERT (19.0.3.23.0). No
        vendor, and a vendor that is one of our own companies, now stop this
        hook on every PURCHASE document; the divert is still ``in_refund``
        alone. The long version of why "every inbound move type" would be a
        regression is in `account.edi.common._import_partner_bank` next door —
        ``out_invoice`` and ``out_receipt`` are inbound and core is RIGHT about
        them. 🔴 On this pipeline there is a second reason to hold the line
        here: core's own customer search plan contains
        `_import_retrieve_customer_from_bank_account_number`, so a stray IBAN
        left on the company partner by an earlier import is read back as a
        VENDOR MATCH on the next bill. The two halves feed each other, which is
        why neither guard is left to the other.
        """
        invoice = collected_values["invoice"]
        if invoice.move_type not in ("in_invoice", "in_refund", "in_receipt"):
            return super()._import_ubl_retrieve_partner_bank(collected_values)

        # ⚠️ The key is part of the contract with the rest of the pipeline, so
        # every path out of this method has to leave it set — including the two
        # new early returns below, which do not call `super()`.
        bank_values = collected_values["partner_bank_values"]
        resolved = (collected_values.get("customer_values") or {}).get("customer")
        if not resolved:
            resolved = invoice.partner_id
        if not resolved:
            # No account holder: nothing can be created, and core would try. See
            # the `required` note in `account.edi.common._import_partner_bank`.
            bank_values["partner_banks"] = self.env["res.partner.bank"]
            return
        # Scoped to the DOCUMENT's company, which is what `collected_values`
        # carries — not `self.env.company`, which in a queue job need not be it.
        if self.env["res.partner"]._ai_impossible_vendor_reason(
            resolved, company=collected_values["company"]
        ):
            bank_values["partner_banks"] = self.env["res.partner.bank"]
            return

        if invoice.move_type != "in_refund":
            return super()._import_ubl_retrieve_partner_bank(collected_values)

        partner_bank_values = collected_values["partner_bank_values"]
        # The key is part of the contract with the rest of the pipeline, so it
        # is always set. Empty: this document yields no account for
        # ``partner_bank_id``, and anything reading it to fill that field would
        # be making the very mistake above.
        partner_bank_values["partner_banks"] = self.env["res.partner.bank"]

        # Off ``collected_values``, not ``invoice.partner_id``: the partner is
        # resolved earlier in the run but only staged in ``to_write``, so the
        # field on the record is still empty at this point.
        #
        # This is the SAME spelling core uses at the deployed revision
        # (`189df353`, `account_edi_ubl.py`: `collected_values.get(
        # 'customer_values', {}).get('customer')` on the `in_invoice` branch),
        # so it is simply the pipeline's own contract, not a workaround.
        #
        # ⚠️ An earlier revision of this comment claimed core read it "one level
        # up", at `collected_values['customer']`, and that this override's
        # spelling was the only working one. That was wrong — it described
        # neither the deployed core nor the development one. Corrected 2026-10-08.
        #
        # The `invoice.partner_id` fallback below stays: it costs nothing and
        # turns a future key change from silent damage into a degraded but
        # correct result, with `test_credit_note_records_the_iban_on_the_vendor`
        # and `test_new_hook_reads_the_partner_from_customer_values` as the net.
        partner = (collected_values.get("customer_values") or {}).get("customer")
        if not partner:
            partner = invoice.partner_id
        if not partner:
            return

        company = collected_values["company"]
        logs = collected_values["logs"]
        for account_number in partner_bank_values["account_numbers"]:
            try:
                self.env["res.partner.bank"]._find_or_create_bank_account(
                    account_number=account_number,
                    partner=partner,
                    company=company,
                )
            except UserError as e:
                logs.append(_("The bank account couldn't be fetched: %s", str(e)))

    # ------------------------------------------------------------------
    # Vendor matching — the business-id fallback, re-hooked
    # ------------------------------------------------------------------

    def _import_ubl_retrieve_customer(self, collected_values):
        """🔴 A DOCUMENT'S VENDOR IS NEVER ONE OF OUR OWN COMPANIES.

        The search-plan twin of the guard in
        `account.edi.common._import_partner`. Read that one for the measured
        case (bill A, matched on a phone the company partner
        shares) and for why the rule is scoped to our own decodes.

        🔴 WHY THIS SEAM AND NOT ONE OF THE THREE WE ALREADY OWN. The three
        steps this module inserts into the plan resolve through
        `goodahead.partner.identifier`, which has refused our own company
        partners since 19.0.3.0.0 — so the rule was already there and could
        never fire, because core decides first. CORE's steps are
        ``vat -> eas_endpoint -> bank_account_number -> email -> phone ->
        name``, and six of them can land on a company partner:

          * `bank_account_number` is the ugly one — it reads back exactly the
            stray `res.partner.bank` rows this bug creates, so one mis-filed
            bill teaches the next one to be mis-filed too;
          * `email` and `phone` are the common ones, because both of our own
            company partners carry both (measured — see
            `_drop_partners_that_are_never_a_vendor`).

        This method is where core writes the answer down —
        ``customer_values['customer']`` and ``to_write['partner_id']``
        (`account_edi_ubl.py:2695-2706` at `189df353`) — and it runs BEFORE
        `_import_ubl_create_missing_customer` and before
        `_import_ubl_retrieve_partner_bank` (`:4017`, `:4018`, `:4029`). So one
        override covers every step of the plan, ours included, and lands early
        enough that no IBAN has been written yet.

        ⚠️ BOTH KEYS, NOT ONE. ``to_write['partner_id']`` is what reaches the
        move; ``customer_values['customer']`` is what the bank hook and
        `_import_ubl_create_missing_customer` read. Clearing only one of them
        leaves the two halves of the pipeline disagreeing about who the vendor
        is, which is worse than either answer.

        ⚠️ INERT ON THE LEGACY CORE, BY CORE'S DOING. `_import_ubl_retrieve_
        customer` does not exist there at all (measured: `hasattr` is False on
        `ca1f0aea`), so nothing calls this and the ``super()`` below is never
        reached — the same arrangement as `_import_ubl_retrieve_partner_bank`
        above. The legacy half of this rule is the `_import_partner` guard.
        """
        result = super()._import_ubl_retrieve_customer(collected_values)
        if not self.env.context.get("ai_invoice_import"):
            return result
        # Purchase only, like `_import_ubl_create_missing_customer`. On a sale
        # document the counterparty is the CUSTOMER, and in a two-company estate
        # a customer that is the other company's partner is the intercompany
        # flow working correctly.
        if collected_values.get("odoo_document_type") != "purchase":
            return result

        customer_values = collected_values.get("customer_values") or {}
        Partner = self.env["res.partner"]

        # 🔴 A SOURCE'S MATCHING POLICY OVERRULES THE PLAN (core/source split —
        # ADR-0010). Core's plan matches on email, phone, bank account and
        # name, any of which a structured source may only PROPOSE by. So under
        # a non-default policy the plan's answer is replaced by the policy's
        # own decision, read off the seller the import put on the context —
        # the same decision post-processing reaches, so the two halves agree.
        # The default policy (the AI source) never gets here.
        policy = Partner._ai_matching_policy()
        if not policy.is_legacy:
            decision = Partner._ai_vendor_decision(
                Partner._ai_seller_from_context(),
                company=collected_values["company"],
                policy=policy,
            )
            vendor = decision["vendor"]
            customer_values["customer"] = vendor
            if vendor:
                collected_values.setdefault("to_write", {})["partner_id"] = vendor.id
            else:
                collected_values.get("to_write", {}).pop("partner_id", None)
            return result

        customer = customer_values.get("customer")
        if not customer:
            return result
        # Scoped to the DOCUMENT's company. Another company's partner is NOT
        # refused — that is the intercompany case, and the agreed rule.
        reason = Partner._ai_impossible_vendor_reason(
            customer, company=collected_values["company"]
        )
        if not reason:
            return result

        _logger.warning(
            "AI invoice import: the seller on this document resolved to %s "
            "(refused: %s); the bill was left without a vendor.",
            customer.display_name,
            reason,
        )
        customer_values["customer"] = Partner
        collected_values.get("to_write", {}).pop("partner_id", None)
        collected_values["logs"].append(
            Partner._ai_refused_vendor_message(customer, reason)
        )
        return result

    def _import_ubl_retrieve_customer_search_plan(self, collected_values):
        """Add our own steps to core's plan: business id, phone, email/web domain.

        🔴 RE-HOOKED FOR THIS TREE. On the source project this logic lives in
        `res.partner._retrieve_partner`, which it overrides to run a fallback
        after `super()` finds nothing. The core this tree runs never calls
        `_retrieve_partner` during a UBL import — `_retrieve_partner` is now a
        thin legacy wrapper that itself delegates to `_import_retrieve_customer`,
        and the pipeline calls `_import_retrieve_customer` directly with the
        plan this method returns. The override was therefore dead code here and
        is expressed as a plan step instead.

        WHERE IN THE PLAN, and why — core's plan at the deployed revision
        (`189df353`) is, in order:

            1. vat                   exact, normalised, country-aware
            2. eas_endpoint          exact (Peppol participant id)
            3. bank_account_number   exact (the IBAN on the document)
            4. email                 exact
            5. phone                 exact
            6. name                  ``('name', '=ilike', name)``

        The business-id step goes **immediately before name**, wherever that
        happens to be. (An earlier revision of this docstring listed five steps,
        omitting `bank_account_number`; the insertion has always been positional
        rather than indexed, so the code was right and only the note was wrong.)

        * It must not come earlier. VAT, the Peppol endpoint, the bank account
          number, email and phone are all exact identity keys, and a vendor
          matched on VAT is matched
          on the strongest thing an invoice carries. A business id placed above
          them could beat a VAT match, which is a regression, not a feature.
        * It must not come after `name`. Whatever core's name step matches on,
          a printed company name is the weakest key an invoice carries — a
          sample set measured it: 6 of 7 bills minted a duplicate because the name on
          the paper is not the name in the ledger. Letting it beat an exact
          national registration number, scoped to the seller's country and so
          unique by construction, is how you get the wrong vendor on a bill.

          ⚠️ A CLAIM THAT WAS IN THIS DOCSTRING AND IS WITHDRAWN (2026-10-09).
          It said core matches names with "**ilike**, i.e. a substring", so
          "ABC Oy" would match "ABC Oy Holding". IT DOES NOT. At the deployed
          revision the step is
          ``{'domain': [('name', '=ilike', name)]}``
          (`account/models/partner.py:1006-1014`) — `=ilike` with no wildcards
          is an EXACT, case-insensitive comparison, which is precisely the
          sample set's finding and the reason this whole feature exists. The placement above
          is unchanged and was never wrong; the reason given for it was.

        ⚠️ This is therefore a deliberate behaviour change from the source, not
        a transcription: the ORDER relative to `name` is different, and it is
        different because the old hook's shape made the source's order an
        artifact rather than a decision. Everything above `name` is unchanged.

        🆕 19.0.3.0.0 — TWO MORE STEPS JOIN IT, IN THE SAME SLOT AND FOR THE
        SAME REASON. Core's own email and phone steps (slots 4 and 5 above) are
        EXACT STRING equality, which fails on `212.555.0111` against
        `+1 212-555-0111` and on `purchasing@vendor.com` against
        `sales@vendor.com`. Ours compare a normalised phone (E.164 —
        `+<country code><national number>`, since 19.0.3.11.0; it read "the last
        ten digits" here until a review finding, which is the third
        copy of the same stale sentence that sweep missed) and a domain, through
        `goodahead.partner.identifier`.

        They go BELOW core's exact steps and ABOVE `name`, for the reason that
        already governs the business id: an exact key is stronger than a
        normalised one, and any identity key at all is stronger than core's
        substring name match. Among the three, business id first (it is exact
        and country-scoped), then phone, then domain — the order F27 §Part 1
        states.

        🔴 None of the three can return an AMBIGUOUS answer: each resolves to a
        single partner id before it hands core a domain, and declines when there
        is more than one. Core runs a step's domain with ``limit=1``, so a
        two-vendor domain left to core would be silently resolved to whichever
        row sorted first.
        """
        plan = super()._import_ubl_retrieve_customer_search_plan(collected_values)
        ResPartner = self.env["res.partner"]
        steps = [
            ResPartner._import_retrieve_customer_from_ai_business_id,
            ResPartner._import_retrieve_customer_from_ai_phone,
            ResPartner._import_retrieve_customer_from_ai_domain,
        ]

        # Insert before the name step rather than at a fixed index, so a core
        # change that adds another exact-match step above `name` keeps these
        # below it instead of silently floating to the wrong place.
        name_step = ResPartner._import_retrieve_customer_from_name
        plan = list(plan)
        for index, entry in enumerate(plan):
            if getattr(entry, "__name__", None) == name_step.__name__:
                return plan[:index] + steps + plan[index:]

        # No name step (a core we have not seen): appending is the conservative
        # placement — they can only run when everything else has already failed.
        _logger.info(
            "AI import: core's customer search plan has no name step; "
            "appending the business-id, phone and domain fallbacks at the end "
            "instead."
        )
        return plan + steps

    def _import_ubl_create_missing_customer(self, collected_values):
        """🔴 CORE MINTS A VENDOR TOO, AND NOTHING IN THIS MODULE COULD SEE IT.

        `_create_partner_from_extraction` stopped creating vendors in
        19.0.3.0.0, and on its own that would have been a HALF MEASURE. Core has
        a step of its own — `account_edi_ubl_cii/models/account_edi_ubl.py:2752`
        at the deployed revision, run from `_ubl_import_invoice` immediately
        after the search plan — which does exactly the same thing:

            if not name or not vat:
                return
            ...
            customer = self.env['res.partner'].create(partner_create_values)

        It is guarded on a VAT being present, which is why the sample set showed it on only
        one of the seven documents — six printed no EIN at all. The seventh was
        Woodgrove, which prints one. So without this override an invoice that happens
        to carry a tax id would still mint `Woodgrove Bank (tailwind fleet)` beside
        `Tailwind Fleet - Woodgrove Bank`, and the chatter note promising nobody was
        invented would be wrong on the bill where it mattered most.

        🔴 AND IT CAN UNDO OUR OWN MATCH. Core's `vat_mismatch` branch creates a
        SECOND partner when a customer WAS found but its stored VAT differs from
        the printed one, overwriting `to_write['partner_id']`. Our new phone and
        domain steps make that case commoner, not rarer: a vendor matched on its
        phone number whose record carries an old or absent EIN.

        ⚠️ SCOPED TO OUR OWN IMPORTS, by the `ai_invoice_import` context key
        `account.move._ai_decode_ubl_into_move` sets. `account.edi.ubl` is a
        shared abstract model: a genuine structured e-invoice arriving over
        Peppol with a real registered VAT is a different thing from a model's
        reading of a PDF, and core's behaviour there is not F27's to change.

        ⚠️ NO VAT IS WRITTEN EITHER. Core's other branch fills an empty `vat` on
        a matched partner from the document. F27 states the rule that forbids it
        here in as many words: *"Two fields are NEVER automatic, empty or not:
        bank account and tax ID"* — invoice fraud is precisely a changed bank
        account or a changed tax identity, and vendor invoices do carry printed
        warnings about exactly that scam. A tax id waits for a
        person.
        """
        if not self.env.context.get("ai_invoice_import"):
            return super()._import_ubl_create_missing_customer(collected_values)
        if collected_values.get("odoo_document_type") != "purchase":
            return super()._import_ubl_create_missing_customer(collected_values)

        if (collected_values.get("customer_values") or {}).get("customer"):
            return
        name = (collected_values.get("customer_values") or {}).get("name")
        if name:
            collected_values["logs"].append(
                _(
                    "No existing vendor matched '%s'. The bill was left without "
                    "a vendor rather than creating one — see the note in the "
                    "chatter for what was read off the document and which "
                    "existing vendors come closest.",
                    name,
                )
            )

    # ------------------------------------------------------------------
    # Product matching — the vendor-aware supplierinfo match, re-hooked
    # ------------------------------------------------------------------

    def _import_ubl_retrieve_products_search_plan(self, collected_values):
        """Stop a vendor's article code from matching OUR internal reference.

        🔴 WHAT THIS DOES AND DOES NOT DO. It adds no matching of its own. Core
        already has a correct vendor-scoped supplierinfo match
        (`product.product._import_retrieve_product_from_supplierinfo`), it is
        already slot 5, and `_import_ubl_invoice_line_add_product_values` already
        hands it `vendor_partner_id` plus all three code fields
        (`sellers_item_id`, `buyers_item_id`, `standard_item_id`). This method's
        entire job is to decide WHICH OF CORE'S STEPS RUN.

        THE PROBLEM IS THE FALL-THROUGH, NOT THE MATCH. Core's plan continues
        past supplierinfo to `default_code` (15), `name` (20) and a predictive
        step. `product_values['default_code']` is `sellers_item_id or
        buyers_item_id` — the VENDOR's code — and slot 15 matches it against OUR
        `product.default_code`. Those are different namespaces that happen to
        share a string space. When a vendor's code has no supplierinfo row,
        core keeps going and can land on whichever of our products happens to
        carry that string as its internal reference: the wrong product on a
        vendor bill, priced from the wrong line, with nothing anywhere
        complaining. `name` and the predictive step are the same class of guess.

        So when the document is a purchase and the vendor is known, the plan is
        narrowed to:

            1. core's supplierinfo step   scoped to this vendor, all three codes
            2. core's barcode step        a GTIN/EAN is global, so no vendor scope

        and nothing else. That is the source project's behaviour expressed on
        this API: its `_import_product` returned an empty recordset rather than
        falling through, precisely so a cross-vendor code could not match.
        Finding nothing is the correct answer here — a human adds the
        supplierinfo row once and every later bill from that vendor matches.

        WHEN SUPPLIERINFO AND BARCODE DISAGREE, supplierinfo wins — it is first,
        as it is in core. A `product.supplierinfo` row is a mapping somebody in
        this business created deliberately; a barcode on an invoice line is
        whatever the vendor printed.

        ⚠️ The barcode step is inert for documents this module generates:
        `adapters/ubl_generator.py` emits `SellersItemIdentification` but no
        `StandardItemIdentification`, so `product_values['barcode']` is always
        None. Kept because this hook is on the shared model and will also see
        UBL from elsewhere — an inert step costs nothing, a missing one is a
        silent gap.

        Outside that case — a sale document, or a purchase whose vendor could
        not be resolved — core's plan is returned untouched. With no vendor
        there is no vendor scope to apply, and narrowing the plan would only
        make matching worse.

        🔴 A PREVIOUS REVISION OF THIS METHOD SHIPPED ITS OWN SUPPLIERINFO
        MATCH, AND IT WAS WRONG. It spelled the domain as two independent dotted
        leaves — `seller_ids.partner_id` and `seller_ids.product_code` — which
        over an x2many become two separate subqueries ANDed on the PRODUCT, not
        on the same `product.supplierinfo` row. A product carrying rows
        (VendorA, "111") and (VendorB, "XXX") therefore matched VendorA quoting
        "XXX": exactly the cross-vendor collision this method exists to prevent.
        Core avoids it with `('product_tmpl_id.seller_ids', 'any', [...])`.
        **Do not reintroduce a local match.** If core's needs extending, extend
        core's step; the one thing that must never be rewritten by hand is the
        row-scoping.
        """
        plan = super()._import_ubl_retrieve_products_search_plan(collected_values)

        if collected_values.get("odoo_document_type") != "purchase":
            return plan

        # The vendor is resolved by `_import_ubl_retrieve_customer`, which runs
        # earlier in `_ubl_import_invoice` than product retrieval does, so it is
        # populated by the time we are called. Core reads it from the same place
        # (`_import_ubl_invoice_line_add_product_values`) to build
        # `vendor_partner_id`, so if this is empty core's supplierinfo step
        # would decline anyway and narrowing the plan would leave nothing.
        vendor = (collected_values.get("customer_values") or {}).get("customer")
        if not vendor:
            return plan

        ProductProduct = self.env["product.product"]
        return [
            ProductProduct._import_retrieve_product_from_supplierinfo,
            ProductProduct._import_retrieve_product_from_barcode,
        ]

    # ------------------------------------------------------------------
    # Units through the shared unit codes
    # ------------------------------------------------------------------

    def _import_ubl_invoice_retrieve_product_uoms(self, collected_values):
        """Read a structured source's line units through ``uom.uom.rec20_code``.

        The ERP's own lookup knows a fixed table of standard units only; the
        code an Administrator set on a unit (Unit codes, the same code sending
        writes) would be ignored, and a code nobody mapped would leave the
        line without a unit and without a word. Here an incoming code is read
        as the unit that carries it; a code no unit carries gives the
        company's default unit for unmapped codes, or no unit, and a note on
        the bill (``logs`` become the import's chatter message).

        🔴 OUR STRUCTURED DECODES ONLY (``einvoice_unit_codes`` on the
        context): an AI reading of a PDF and every document the ERP imports
        by itself keep the ERP's lookup unchanged.
        """
        if not self.env.context.get(EINVOICE_UNIT_CODES_CONTEXT_KEY):
            return super()._import_ubl_invoice_retrieve_product_uoms(collected_values)
        Uom = self.env["uom.uom"]
        company = collected_values["invoice"].company_id
        logs = collected_values["logs"]
        for line_values in collected_values["lines_collected_values"]:
            uom_values = line_values["product_uom_values"]
            to_write = line_values["to_write"]
            to_write["product_uom_id"] = False
            code = uom_values.get("uom_code")
            if not code:
                continue
            uom, note = Uom._einvoice_unit_for_code(code, company)
            if note:
                logs.append(note)
            if not uom:
                uom_values["force_empty"] = True
                continue
            product = line_values["product_values"].get("product")
            product_uom = product.product_tmpl_id.uom_id if product else Uom
            if product and not uom._has_common_reference(product_uom):
                logs.append(_(
                    "The unit %(uom)s (unit code %(code)s) does not fit the unit %(product_uom)s "
                    "of the product %(product)s; the line was left without a unit.",
                    uom=uom.name,
                    code=code,
                    product_uom=product_uom.name,
                    product=product.display_name,
                ))
                uom_values["force_empty"] = True
                continue
            to_write["product_uom_id"] = uom.id
            uom_values["uom"] = uom
