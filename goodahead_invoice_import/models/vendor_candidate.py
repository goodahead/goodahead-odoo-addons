# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The existing vendors offered on a bill whose vendor was not recognised.

🔴 WHY THESE ARE ROWS AND NOT A CHATTER LIST. Part 1 already posts the
candidates to the chatter, and that is where they died: a chatter note is read,
not acted on. The first design (a dedicated "Vendor Data" tab) was rejected
for the same reason: buried in the last tab it gets lost, the accountant just
searches for a vendor and the data never improves. The same objection applies
to a note. So the candidates are RECORDS, shown inline under the Vendor field
the accountant is already clicking, each with a button.

🔴 AND THE BUTTON IS THE WHOLE FEATURE, NOT A CONVENIENCE. Picking a candidate
runs `account.move._ai_apply_vendor_data`, which stores the invoice's phone and
web domain as `goodahead.partner.identifier` rows on that vendor. That is how
the matcher LEARNS: the Northwind bill comes from the manufacturer and the ledger
holds `NORTHWIND - Smith reps`, the rep agency that sells for them — different
company, different phone, different domain, and no rule derives one from the
other. A person says so ONCE and the next bill matches by itself.

⚠️ NO OWL. The list is an ordinary inline one2many with an object button. Two
traps in this tree make a custom widget the wrong bet: 19.0's `evalPartialContext`
SILENTLY DROPS a field-`context` key whose expression names a record field (so
the move's id cannot be passed into a Many2one's context to rank its dropdown),
and a duplicate OWL template name aborts the WHOLE `web.assets_web` bundle
silently — a duplicate-template check exists because that already happened here.
"""

from odoo import _, api, fields, models


class GoodaheadVendorCandidate(models.Model):
    _name = "goodahead.vendor.candidate"
    _description = "Suggested vendor for an unrecognised bill"
    _order = "sequence, id"

    move_id = fields.Many2one(
        "account.move",
        required=True,
        ondelete="cascade",
        index=True,
        string="Bill",
    )
    # 🔴 STORED, FOR THE COMPANY RULE (security.xml). A candidate row names a
    # vendor and its bill counts for one company's bill; a user of another
    # company must not list it. The ORM fills it for existing rows when the
    # add-on is upgraded.
    company_id = fields.Many2one(
        related="move_id.company_id",
        store=True,
        index=True,
        readonly=True,
    )
    partner_id = fields.Many2one(
        "res.partner",
        required=True,
        ondelete="cascade",
        string="Vendor",
    )
    reason = fields.Char(
        help="Why this vendor is being offered — the same phone, the same "
        "domain, or a name that contains what the invoice printed.",
    )
    bill_count = fields.Integer(
        string="Posted bills",
        help="How many POSTED vendor bills and refunds this company has had "
        "from that vendor. 🔴 A real count, not `supplier_rank`: that number "
        "is incremented by other things and never decremented, so it is not "
        "bills and must not be labelled bills.",
    )
    draft_bill_count = fields.Integer(
        string="Draft bills",
        help="How many DRAFT vendor bills and refunds this company already has "
        "from that vendor. Reported beside the posted number and never added "
        "into it, so a vendor with no posted bills but several drafts is not "
        "shown as a vendor nobody has billed.",
    )
    partner_archived = fields.Boolean(
        string="Archived",
        help="The vendor is archived. It is still offered, because an archived "
        "record may well be the right answer and hiding it sends somebody off "
        "to create the duplicate by hand.",
    )
    sequence = fields.Integer(default=10)
    partner_location = fields.Char(
        string="Where",
        compute="_compute_partner_location",
        help="The vendor's town, shown under the name. 🔴 It is here because "
        "the three candidates on a Northwind bill are called NORTHWIND, "
        "`NORTHWIND - Smith reps` and `Northwind - Smith Reps`: the NAME does not "
        "tell them apart and the town does. Computed, never stored — it is a "
        "display of the partner and must not become a second copy of it.",
    )

    @api.depends("partner_id.city", "partner_id.state_id")
    def _compute_partner_location(self):
        for row in self:
            parts = [row.partner_id.city, row.partner_id.state_id.code]
            row.partner_location = ", ".join(part for part in parts if part)

    # ------------------------------------------------------------------
    # The one button
    # ------------------------------------------------------------------

    def action_use_this(self):
        """File the bill against this vendor, and LEARN from it.

        Three things happen, in this order:

        1. the bill gets the vendor;
        2. `_ai_apply_vendor_data` fills the vendor's EMPTY fields from the
           invoice, adds the invoice's phone and domain as identifier rows, and
           queues every CONTRADICTION as a `goodahead.vendor.data.suggestion`
           rather than overwriting anything;
        3. the candidate rows for this bill are deleted — the question has been
           answered and a stale list of alternatives beside a filed bill is
           noise.

        The chatter note comes out of step 2, with a first line naming who
        picked. One note rather than two: the person who picked and what was
        learned are the same event, and splitting them means the second half is
        the one nobody reads.
        """
        self.ensure_one()
        # Under the policy the bill's import used (core/source split): learning
        # from a strict bill never writes a bank account.
        move = self.move_id._ai_with_import_policy()
        move.partner_id = self.partner_id

        extracted = move._ai_stored_extraction()
        origin = _(
            "%(user)s picked this vendor from the suggested candidates.",
            user=self.env.user.display_name,
        )
        move._ai_apply_vendor_data(
            self.partner_id, extracted, origin=origin
        )
        move.ai_vendor_candidate_ids.unlink()
        return True

    # ------------------------------------------------------------------
    # Building the list
    # ------------------------------------------------------------------

    @api.model
    def _build_for_move(self, move, candidates):
        """Store ``candidates`` (the dicts `_ai_vendor_candidates` returns).

        Replaces whatever was there: a re-import of the same bill must not
        stack two sets of alternatives.

        `sudo()` on the write. The rows are produced by the import, which runs
        in a queue job whose user is whoever uploaded the PDF — and an import
        must never fail because of an ACL on a suggestion table.
        """
        rows = self.sudo().search([("move_id", "=", move.id)])
        if rows:
            rows.unlink()
        if not candidates:
            return self.browse()
        return self.sudo().create(
            [
                {
                    "move_id": move.id,
                    "partner_id": candidate["partner"].id,
                    "reason": candidate["reason"],
                    "bill_count": candidate["bill_count"],
                    "draft_bill_count": candidate.get("draft_bill_count", 0),
                    "partner_archived": candidate["archived"],
                    "sequence": (index + 1) * 10,
                }
                for index, candidate in enumerate(candidates)
            ]
        )
