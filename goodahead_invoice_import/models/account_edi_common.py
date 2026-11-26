# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import logging

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import clean_context

from odoo.addons.base.models.res_bank import sanitize_account_number

_logger = logging.getLogger(__name__)


class AccountEdiCommon(models.AbstractModel):
    """The OLD UBL pipeline's bank hook. Dormant in this tree — keep it anyway.

    🔴 READ THIS BEFORE DELETING ANYTHING IN THIS FILE.

    This override does NOT fire for a Peppol BIS 3.0 import on the US deployment, and
    that is not a bug. The core here — `odoo/odoo` at
    `189df353` (2026-06-23), installed over the Debian package so that none of
    the deb's Python runs — routes `account.edi.xml.ubl_bis3`
    through `account.edi.ubl._ubl_import_invoice`. That pipeline resolves bank
    details through `_import_ubl_retrieve_partner_bank`, which is overridden
    next door in `account_edi_ubl.py`. THAT is the live one here.

    This one is the twin for the core used **before** that rework. An older
    deployment runs an older nightly and is still on it, so there the
    live/dormant roles are exactly reversed.

    It is kept for three reasons, none of them sentimental:

    1. The module is generic and shared; the other project needs this half.
    2. `account.edi.common` is still reached on THIS core by other formats —
       Factur-X and plain UBL 2.0 documents still call `_import_partner_bank`
       (`account_edi_xml_cii_facturx.py:306`, `account_edi_xml_ubl_20.py:1154`).
       So it is dormant for *our generated BIS 3.0*, not dead in general.
    3. A core upgrade or a format change can move the live path back here
       without warning, and the failure mode is silent: a supplier's IBAN
       quietly recorded against the company.

    ⚠️ Its tests are the ones named `test_older_hook_*` in
    `tests/test_credit_memo_bank.py`, which call this method DIRECTLY so they
    pass on either core. The tests that exercise the live path here go through
    `_process_import` and the real decoder.
    """

    _inherit = "account.edi.common"

    # ------------------------------------------------------------------
    # Never invent a vendor — the older core's half of it
    # ------------------------------------------------------------------

    @api.model
    def _ai_core_peppol_domain(self, kwargs):
        """Core's own Peppol-identity domain, from core's own two kwargs.

        🔴 THIS IS A SECOND SPELLING OF FOUR LINES OF CORE
        (`account_edi_common.py:530-534` at `ca1f0aea`) AND IT IS KEPT HONEST BY
        A TEST, NOT BY CARE. `test_legacy_no_invented_vendor.
        test_a_vendor_reachable_only_by_its_peppol_pair_still_matches` builds a
        vendor that NOTHING ELSE in core's chain can find — different name,
        different VAT, no phone, no email — so if this domain stops agreeing
        with core's, that vendor stops matching and the test says so. A copied
        rule with no tripwire is how two spellings drift; this one has the
        tripwire.
        """
        eas = kwargs.get("peppol_eas")
        endpoint = kwargs.get("peppol_endpoint")
        if eas and endpoint:
            return [("peppol_eas", "=", eas), ("peppol_endpoint", "=", endpoint)]
        return False

    def _import_partner(self, company_id, name, phone, email, vat, **kwargs):
        """🔴 THE OLDER CORE INVENTS A VENDOR HERE, AND NOTHING COULD SEE IT.

        The rule: a vendor is never invented. It is the POLICY and not a
        setting — there is no per-company flag here and no degraded mode, for
        the same reason `models/core_profile.py` refuses an unrecognised build
        rather than guessing: a second code path through the highest-severity
        behaviour in this module is an invitation to take it.

        The sibling of `account.edi.ubl._import_ubl_create_missing_customer`,
        which is where the SEARCH-PLAN core invents and which this module has
        suppressed since 19.0.3.0.0. The legacy core has no such method; it
        creates inside this one, guarded by ``if not partner and name and vat``
        (`account_edi_common.py:545` at `ca1f0aea`) and calling
        ``res.partner.create`` four lines later at `:549`.

        ⚠️ SCOPED TO OUR OWN DECODES by the `ai_invoice_import` context key, for
        the same reason as the other half: `account.edi.common` is shared, and a
        genuine structured e-invoice arriving over Peppol with a registered VAT
        is not a model's reading of a PDF.

        🔴 WHERE THIS DEPARTS FROM the dual-core design — AND 19.0.3.23.0 MOVED THE LINE.
        §6 proposed pre-retrieving on EVERY call through this method. Until
        19.0.3.23.0 that was declined: core's create cannot fire unless
        ``name and vat``, so behind that guard there was nothing to suppress and
        the right behaviour was core's, untouched, at zero cost.

        The pre-retrieval is now unconditional **within our own decodes**, and
        the reason is a second defect at the same seam that the create guard
        cannot see. Core also MATCHES here, and its match has no
        ``name and vat`` condition: bill A resolved its vendor to the buyer's
        own company partner on the PHONE step, with core creating nothing at
        all. So there are two things to check and only one of them is gated on a
        printed tax id. §6 was right about the shape and wrong about nothing;
        the method simply turned out to have a second job.

        ⚠️ IT STILL COSTS NOTHING OUTSIDE OUR OWN IMPORTS — the context test is
        the first line of the method and is unchanged. Inside them it is one
        extra read per document, on a path that already accepted a second
        retrieval on the match path for the reason below.

        ⚠️ WHAT §6 GOT RIGHT AND IS KEPT. The retrieval has to happen before
        ``super()``, because core creates inside the same call and a create
        cannot be taken back. On a MATCH we call ``super()`` anyway rather than
        returning the partner we just found: core fills an empty address from
        the document after a match, that is not create, and re-implementing it
        here would be a third spelling of something core already does. The
        retrieval therefore runs twice on the match path. It is a read, it is
        deterministic, and the alternative is worse.

        🔴 THE TEMPTING WRONG ANSWER, WRITTEN DOWN SO NOBODY TRIES IT (§6's own
        warning, and it survives contact): passing ``vat=None`` to ``super()``
        would satisfy core's guard and skip the create — and would also disable
        core's VAT retrieval, which is step 1 of the match and the strongest key
        an invoice carries. Nulling ``name`` fails the same way. There is no
        argument-level way to disarm the create that does not also disarm the
        match, which is why the wrapper exists at all.

        ⚠️ THE RETURN SHAPE IS THE TRAP. `_import_fill_invoice` does
        ``invoice.partner_id = partner.id`` on the next line
        (`account_edi_xml_ubl_20.py:1116`), so the early return must be a
        ``(recordset, logs)`` 2-tuple holding an EMPTY ``res.partner`` — not
        ``False``, not ``None``.
        """
        if not self.env.context.get("ai_invoice_import"):
            return super()._import_partner(
                company_id, name, phone, email, vat, **kwargs
            )

        Partner = self.env["res.partner"]

        # 🔴 A SOURCE'S MATCHING POLICY OVERRULES CORE'S CHAIN (core/source split —
        # ADR-0010). Core's legacy chain matches on phone, email and name,
        # which a structured source may only PROPOSE by, and creates on a
        # miss. Under a non-default policy neither runs: the vendor is the
        # policy's decision or nobody. The default policy (the AI source)
        # takes the path below, unchanged.
        policy = Partner._ai_matching_policy()
        if not policy.is_legacy:
            decision = Partner._ai_vendor_decision(
                Partner._ai_seller_from_context(),
                company=company_id,
                policy=policy,
            )
            return decision["vendor"], []

        partner = Partner.with_company(company_id)._retrieve_partner(
            name=name,
            phone=phone,
            email=email,
            vat=vat,
            domain=self._ai_core_peppol_domain(kwargs),
        )

        # 🔴 A DOCUMENT'S VENDOR IS NEVER ONE OF OUR OWN COMPANIES OR ONE OF OUR
        # OWN LOGINS — AND THIS TEST IS NOT GATED ON ``name and vat``, UNLIKE
        # THE CREATE SUPPRESSION BELOW IT.
        #
        # Core cannot CREATE without both, which is why the pre-retrieval used
        # to sit behind that guard. Core's MATCH has no such condition: the
        # phone and email step runs on any document that prints either. Bill A
        # printed a tax id and still would have reached
        # here with none, because what matched was the phone.
        #
        # What happened, and it is the whole reason this block exists: core's
        # legacy chain (`account/models/partner.py:958` at `ca1f0aea`) runs each
        # step TWICE — once company-scoped, once with ``company_id = False`` —
        # and accepts a step whose answer is exactly one row. The seller printed
        # `+358400000444`; on the company-scoped pass that matched four related
        # contacts and ``len(partner) == 1`` declined, and on the
        # ``company_id = False`` pass it matched exactly one row: the company's
        # OWN partner. Core then set `invoice.partner_id` to it, and
        # `_import_partner_bank` hung the seller's IBAN there eight lines later.
        #
        # ⚠️ SCOPED TO OUR OWN DECODES, and here that scope is load-bearing
        # rather than conventional. The US deployment is a TWO-COMPANY database
        # (`Alpha LLC` / `Beta Inc.`, with real intercompany orders), so
        # on a genuine structured document a counterparty that IS another
        # company's partner is CORRECT. An unscoped refusal would break the
        # intercompany flow. A model's reading of a PDF is a different thing.
        #
        # Half of the rule is not new and is not ours to invent: it is rule 1 of
        # `goodahead.partner.identifier._drop_partners_that_are_never_a_vendor`
        # — *"We are never a vendor on our own bill. This is not a heuristic."*
        # It has been in this module since 19.0.3.0.0 and could never fire here,
        # because core had already decided before our steps ran. The USER half
        # and the ownership half are the agreed rules. All
        # three bodies live in one place, `res.partner.
        # _ai_impossible_vendor_reason`, and all three are scoped to THIS
        # company — another company's partner is allowed, because that is the
        # intercompany case.
        #
        # 🔴 `company_id`, THE ARGUMENT, NOT `self.env.company`. Core hands this
        # method the document's company and the import runs in a queue job
        # whose `env.company` need not be it. A guard that protects the wrong
        # company would refuse a legitimate intercompany vendor and pass the
        # real defect through.
        reason = Partner._ai_impossible_vendor_reason(partner, company=company_id)
        if reason:
            _logger.warning(
                "AI invoice import: the seller on this document resolved to "
                "%s (refused: %s); the bill was left without a vendor.",
                partner.display_name,
                reason,
            )
            return Partner, [Partner._ai_refused_vendor_message(partner, reason)]

        # Core's own create guard. Where it cannot fire, there is nothing left
        # here to suppress and core's behaviour is the right behaviour.
        if not (name and vat):
            return super()._import_partner(
                company_id, name, phone, email, vat, **kwargs
            )

        if partner:
            return super()._import_partner(
                company_id, name, phone, email, vat, **kwargs
            )

        # 🔴 CORE'S `logs` ARE USER-VISIBLE AND A SILENT SUPPRESSION IS THE
        # FAILURE MODE THIS PROJECT KEEPS FINDING. Core's own no-match line
        # ("Could not retrieve partner with details: …") would be a lie by
        # omission: core did not fail to find one, it was stopped from making
        # one up. The wording is the SAME sentence the search-plan half appends
        # (`account_edi_ubl.py:_import_ubl_create_missing_customer`), so a
        # person reading a bill gets one message whichever core they are on.
        _logger.info(
            "AI invoice import: no existing vendor matched %r; core's create "
            "was suppressed and the bill left without a vendor.",
            name,
        )
        return self.env["res.partner"], [
            _(
                "No existing vendor matched '%s'. The bill was left without a "
                "vendor rather than creating one — see the note in the chatter "
                "for what was read off the document and which existing vendors "
                "come closest.",
                name,
            )
        ]

    def _import_partner_bank(self, invoice, bank_details):
        """Never read a received credit note's bank account as one of ours.

        The older decoder's twin of
        ``account.edi.ubl._import_ubl_retrieve_partner_bank``. Core before
        the UBL rework — which the source project's older nightly still runs,
        and which THIS tree does not — never calls that hook; it resolves the
        bank here, taking the company partner for every inbound move. On a
        received credit note that hangs the supplier's IBAN on our own partner:
        a new number is created there, and a number someone already archived off
        us is quietly un-archived (the credit-note bank fix; seen again on a later
        received credit note).

        So, as in the newer hook, the number is recorded against the vendor and
        ``partner_bank_id`` is left to ``_compute_partner_bank_id``. Written
        without ``_find_or_create_bank_account``, which the older core lacks.
        Only ``in_refund`` is DIVERTED.

        🔴 WHAT WAS WIDENED IN 19.0.3.23.0, AND WHAT DELIBERATELY WAS NOT.
        The two GUARDS below — no vendor, and a vendor that is one of our own
        companies — now run on every PURCHASE document. The DIVERT still runs
        on ``in_refund`` alone, and "widen it to every inbound move type" would
        be a regression rather than a fix. Core picks the side from
        ``invoice.is_inbound()``, and ``get_inbound_types`` is
        ``['out_invoice', 'in_refund', 'out_receipt']``
        (`account/models/account_move.py:6303` at `ca1f0aea`):

          * on ``in_refund`` core takes ``self.env.company.partner_id`` — the
            credit-note bank bug, and the only reason the divert exists;
          * on ``in_invoice`` core ALREADY takes ``invoice.partner_id``, which
            is the correct side, and it also fills ``invoice.partner_bank_id``,
            which an accountant uses. Diverting there would gain nothing and
            lose that;
          * on ``out_invoice`` / ``out_receipt`` core takes the company partner
            and is RIGHT — that is our account, for the customer to pay into.

        🔴 THE NO-VENDOR GUARD IS DEFENCE IN DEPTH, AND THE FIRST VERSION OF
        THIS NOTE CLAIMED IT WAS NECESSARY. It said the root fix alone would
        turn a wrong vendor into a FAILED IMPORT, because core reaches
        ``create({'acc_number': …, 'partner_id': partner.id})`` with
        ``partner.id`` False and ``res.partner.bank.partner_id`` is
        ``required``. **That was read, not run, and running it says no.**
        `test_no_bank_row_and_no_crash_when_the_bill_has_no_vendor` PASSES
        against 19.0.3.22.0: core never reaches the create, because
        ``partner_bank.partner_id == partner`` compares two EMPTY recordsets,
        which is True, so it assigns the empty recordset and returns
        (`account_edi_common.py:581-586` at `ca1f0aea`). The guard is kept as a
        lock on that outcome, not as a repair.

        ⚠️ On the SEARCH-PLAN core the same situation reads like it WOULD raise
        — `_find_or_create_bank_account` has no empty-partner branch and lands
        on the same ``required`` column — but that is a reading of the source
        and has not been run on that bench. Stated as a reading.

        🔴 WHAT *IS* MEASURED, AND IT IS THE REASON THIS DEFECT HAS NEVER BEEN
        SEEN ON THE US DEPLOYMENT. The newer core refuses the whole thing one level
        down: `res.partner.bank._find_or_create_bank_account` raises
        *"Please add your own bank account manually"* when ``partner.id`` is in
        ``res.company._get_company_partner_ids()``
        (`base/models/res_bank.py:209-214` at `189df353`), unless
        ``allow_company_account_creation``. The LEGACY core has neither that
        protection nor `_get_company_partner_ids` — it does a bare
        ``create()``. So the `res_partner_bank` row R could not have been
        created on the US-deployment build, and the company-partner guard below is
        this module supplying, on the older core, a protection the newer core
        has built in.

        ⚠️ ``in_receipt`` is in the list though this module never produces one:
        the condition states which documents the rules are about, and a purchase
        receipt is one of them. `_finalize_import` only ever builds
        ``in_invoice`` / ``in_refund``.
        """
        if invoice.move_type not in ("in_invoice", "in_refund", "in_receipt"):
            return super()._import_partner_bank(invoice, bank_details)

        partner = invoice.partner_id.commercial_partner_id
        if not partner:
            return
        # A document whose sender resolved to this bill's own company, to one of
        # our logins, or to another company's contact must not hang its IBAN
        # there — whether the vendor was mis-resolved (bill A) or the
        # extraction swapped seller and buyer. Same body as `_import_partner`'s
        # guard, and scoped to the same company: `invoice.company_id`, not
        # `self.env.company`.
        if self.env["res.partner"]._ai_impossible_vendor_reason(
            partner, company=invoice.company_id
        ):
            return

        if invoice.move_type != "in_refund":
            # Core's own behaviour, which is correct on a bill we pay: the
            # account goes to `invoice.partner_id` and `partner_bank_id` is
            # filled. Only the two guards above were added for this side.
            return super()._import_partner_bank(invoice, bank_details)

        # A strict import never creates a bank account (AC-06, AC-07; see
        # `res.partner.bank._find_or_create_bank_account`). On a credit note
        # the vendor's account was only ever recorded, never attached, so
        # there is nothing left to do.
        if self.env["res.partner.bank"]._ai_bank_lookup_only():
            return

        # Same reason as core: a default from the context must not leak into
        # a bank account created during import.
        Bank = self.env["res.partner.bank"].with_env(
            self.env(context=clean_context(self.env.context))
        )
        numbers = {sanitize_account_number(number) for number in bank_details}
        numbers.discard(False)
        for number in numbers:
            # Archived ones count: the vendor already has it, archived or not.
            known = Bank.with_context(active_test=False).search_count(
                [
                    ("sanitized_acc_number", "=", number),
                    ("partner_id", "child_of", partner.id),
                ],
                limit=1,
            )
            if known:
                continue
            try:
                Bank.create({"acc_number": number, "partner_id": partner.id})
            except UserError as e:
                invoice._message_log(
                    body=_("The bank account couldn't be fetched: %s", str(e))
                )

    # ------------------------------------------------------------------
    # The vendor-aware product match — the legacy half (the dual-core design)
    # ------------------------------------------------------------------

    def _import_lines(
        self,
        record,
        tree,
        xpath,
        document_type=False,
        tax_type=False,
        qty_factor=1,
    ):
        """Say who the vendor is, so the product plan can be narrowed to them.

        🔴 THIS OVERRIDE DOES NOTHING BUT CARRY ONE ID. The decision it enables
        lives in `product.product._get_product_domain_search_order`, and the
        reason it cannot live there alone is a plain API fact: core resolves a
        line's product through `_import_product(**product_vals)` ->
        `product.product._retrieve_product(**product_vals)`, and neither of
        those is given the document or the partner. The context is the only
        channel between the record and the plan.

        🔴 WHY THE TOMBSTONE THAT STOOD HERE WAS RIGHT UNTIL IT WAS NOT. The
        two methods this replaces (`_import_lines` / `_import_product`, from
        the source project's fork) were deleted in 19.0.1.2.0 with a good
        reason: on THIS tree's core they are never called, and real, reviewed,
        tested-looking code that silently never runs is the failure this
        project keeps finding. That reason stops holding the moment the module
        claims to run on two cores — which is the dual-core design. What comes back is the
        BEHAVIOUR, not the code: see the note on the product-side method for
        why only one of the two methods returned, and why it is not the one
        the dual-core design names.

        ⚠️ PURCHASE DOCUMENTS ONLY. On a sale document `record.partner_id` is
        the CUSTOMER, and scoping product matching to a customer's
        `product.supplierinfo` rows would match nothing at all. ``tax_type`` is
        ``'sale'`` / ``'purchase'`` in all four of this core's callers
        (`account_edi_xml_ubl_20`, `account_edi_xml_cii_facturx`,
        `purchase_edi_ubl_bis3`, `sale_edi_ubl`) and is the same discriminator
        the search-plan half reads as ``collected_values['odoo_document_type']``
        — so the two builds narrow on the same condition. 🔴 ``record`` is NOT
        always an `account.move` here: two of those four callers pass a
        `purchase.order` / `sale.order`, which have no `journal_id`. That is
        why the test is on the argument and not on the record.

        ⚠️ The COMMERCIAL partner, because that is what core's own
        supplierinfo step is handed on the other build
        (`account_edi_ubl.py:3251` at `189df353`:
        ``partner.commercial_partner_id.id``). A bill filed against a child
        contact would otherwise find none of the parent's supplierinfo rows.
        The source project's fork uses ``record.partner_id.id`` and has that
        bug; it is not copied.

        🔴 SCOPED TO OUR OWN DECODES, AND IT WAS NOT (found in
        review). This was the one override in the module with no `ai_invoice_import`
        guard, while the two either side of it — `_import_partner` above and
        `res.partner._retrieve_partner_with_name` — both have one. On the legacy
        core `_import_lines` has three live callers at `ca1f0aea`
        (`account_edi_xml_ubl_20.py:1160`, `account_edi_xml_cii_facturx.py:330`,
        and `purchase_edi_ubl_bis3/models/purchase_edi_xml_ubl_bis3.py:355`,
        whose module is `installed` in the source project's database), and for
        every one of them the narrowing dropped core's `default_code`, `name`
        and `name ilike` steps. A line on a GENUINE supplier UBL whose vendor
        has no `product.supplierinfo` row then silently got no product.

        The module's brief in this step is not to change behaviour on documents
        it did not produce. A structured e-invoice arriving over Peppol is not a
        model's reading of a PDF, and core's product semantics for those are not
        ours to change — the same sentence as the other two overrides, now true
        of all three.

        ⚠️ IT COSTS THE AI PATH NOTHING, and that is a measurement rather than a
        reading: `ai_invoice_import` is set by `account.move._ai_decode_ubl_into_
        move` on the model the decoder is fetched from, so it is on the context
        for the whole decode, and
        `test_product_matching.test_an_unmatched_vendor_code_does_not_fall_
        through_to_our_default_code` drives a real `_decode` end to end on both
        benches and still finds no product for an unmapped vendor code.

        🔴 AND IT CARRIES THE SUPPRESSION'S OWN OUTCOME, WHICH IS A REVIEW FINDING
        FINDING W3. On a document that reached `_import_partner` above, found no
        vendor and had core's create suppressed, `record.partner_id` is False —
        core's caller does `invoice.partner_id = partner.id` with our empty
        recordset. The old single key then said nothing, the product plan fell
        through to core's whole list, and a vendor's article code was matched
        against OUR internal references on exactly the bills this module
        deliberately hands to a human. Two steps that were each right on their
        own; only a test of the PAIR could hold it, and there was none —
        `test_product_matching.
        test_a_suppressed_vendor_does_not_re_arm_cores_default_code_step` is it.

        ⚠️ IT IS INERT ON THE SEARCH-PLAN BUILD. This method IS called there,
        by Factur-X and UBL 2.0 documents — but the context key it sets is read
        only by `_get_product_domain_search_order`, which that core defines and
        never calls. So a US-deployment Factur-X import gets one extra, unread
        context key and nothing else. `tests/test_product_matching` asserts
        that by measurement rather than by this paragraph.
        """
        vendor = record.partner_id.commercial_partner_id
        if self.env.context.get("ai_invoice_import") and tax_type == "purchase":
            # 🔴 TWO FACTS, NOT ONE (found in review). "No vendor id on
            # the context" and "this is our purchase decode and there IS no
            # vendor" are different states, and only the second one can narrow
            # the plan. Carrying one key conflated them, so the document the
            # create suppression had just handed to a human — the one bill where
            # the vendor is unknown — fell through to core's FULL plan,
            # `default_code` included, which on this core is the vendor's own
            # `SellersItemIdentification`.
            self = self.with_context(
                ai_import_vendor_partner_id=vendor.id if vendor else False,
                ai_import_vendor_unknown=not vendor,
            )
        return super()._import_lines(
            record,
            tree,
            xpath,
            document_type=document_type,
            tax_type=tax_type,
            qty_factor=qty_factor,
        )
