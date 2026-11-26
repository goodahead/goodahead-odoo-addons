# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The existing vendors offered on a bill whose vendor was not recognised.

Candidates are records shown inline under the Vendor field, each with a button,
so the choice is acted on rather than read in the chatter. Picking one runs
``account.move._ai_apply_vendor_data``, which stores the invoice's phone and
domain as identifiers on that vendor, so the next bill matches by itself.
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
    # The bill's company, stored so record rules can scope the rows: a user
    # of another company must not list it. Filled by the ORM for existing
    # rows on upgrade.
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
        "from that vendor. A real count, not `supplier_rank`: that number "
        "is incremented by other things and never decremented, so it is not "
        "bills and must not be labelled bills.",
    )
    draft_bill_count = fields.Integer(
        string="Draft bills",
        help="How many DRAFT vendor bills and refunds this company already has "
        "from that vendor. Reported beside the posted number and never added "
        "into it.",
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
        help="The vendor's town, shown under the name, because several "
        "candidates can carry almost the same name and the town tells them "
        "apart. Computed, never stored — it is a "
        "display of the partner and must not become a second copy of it.",
    )

    @api.depends("partner_id.city", "partner_id.state_id")
    def _compute_partner_location(self):
        for row in self:
            parts = [row.partner_id.city, row.partner_id.state_id.code]
            row.partner_location = ", ".join(part for part in parts if part)

    def action_use_this(self):
        """File the bill against this vendor and learn from the invoice.

        Empty vendor fields are filled, phone and domain become identifiers,
        contradictions are queued as suggestions, and the candidate rows are
        removed.
        """
        self.ensure_one()
        # Under the policy the bill's import used: learning from a strict
        # bill never writes a bank account (AC-06, AC-07).
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

    @api.model
    def _build_for_move(self, move, candidates):
        """Replace the bill's candidates with ``candidates``.

        Written with ``sudo()``: the import runs as whoever uploaded the PDF
        and must not fail on an ACL for this table.
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
