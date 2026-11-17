# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Review dialog comparing invoice details with the vendor record.

The wizard holds no rules of its own: each line wraps a
`goodahead.vendor.data.suggestion` and Apply calls that row's `action_apply`.
Sensitive rows (bank account, tax id) always start unticked, so applying one
requires a deliberate tick.
"""

from odoo import Command, _, api, fields, models


class GoodaheadVendorDataReview(models.TransientModel):
    _name = "goodahead.vendor.data.review"
    _description = "Review the invoice details that differ from a vendor"

    partner_id = fields.Many2one("res.partner", string="Vendor", readonly=True)
    move_id = fields.Many2one(
        "account.move",
        string="Bill",
        readonly=True,
        help="Set when the dialog was opened from a bill. Empty when it was "
        "opened from the queue, where the rows can come from several bills — "
        "which is also why “Not now” only defers a bill it was opened from.",
    )
    line_ids = fields.One2many(
        "goodahead.vendor.data.review.line",
        "review_id",
        string="Details",
    )
    heading = fields.Char(compute="_compute_heading")
    has_sensitive = fields.Boolean(compute="_compute_has_sensitive")

    @api.depends("line_ids.is_sensitive")
    def _compute_has_sensitive(self):
        """Show a full-width warning banner, which unlike a column cannot be
        truncated."""
        for wizard in self:
            wizard.has_sensitive = any(wizard.line_ids.mapped("is_sensitive"))

    @api.depends("partner_id", "move_id", "line_ids")
    def _compute_heading(self):
        """The vendor and the bill, shown once at the top."""
        for wizard in self:
            vendor = wizard.partner_id.display_name or _("this vendor")
            if wizard.move_id:
                wizard.heading = _(
                    "%(count)s detail(s) on %(bill)s differ from %(vendor)s",
                    count=len(wizard.line_ids),
                    bill=wizard.move_id.display_name,
                    vendor=vendor,
                )
                continue
            wizard.heading = _(
                "%(count)s detail(s) waiting on %(vendor)s",
                count=len(wizard.line_ids),
                vendor=vendor,
            )

    # ------------------------------------------------------------------
    # Opening it
    # ------------------------------------------------------------------

    @api.model
    def _open_for(self, suggestions, move=None):
        """Build the dialog over ``suggestions`` and return the action.

        ``suggestions`` must belong to a single vendor; the vendor is read
        from the rows.
        """
        pending = suggestions.filtered(lambda row: row.state == "pending")
        if not pending:
            # Another user may have handled the rows meanwhile; never open an
            # empty dialog.
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "type": "info",
                    "sticky": False,
                    "message": _("Nothing is waiting on this vendor any more."),
                },
            }
        wizard = self.create(
            {
                "partner_id": pending[0].partner_id.id,
                "move_id": move.id if move else False,
                "line_ids": [
                    Command.create(
                        {
                            "suggestion_id": row.id,
                            # Sensitive rows are never pre-ticked.
                            "selected": not row.is_sensitive,
                        }
                    )
                    for row in pending
                ],
            }
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Details that differ from this vendor"),
            "res_model": "goodahead.vendor.data.review",
            "res_id": wizard.id,
            "view_mode": "form",
            "views": [
                (
                    self.env.ref(
                        "goodahead_invoice_ai_import."
                        "view_vendor_data_review_form"
                    ).id,
                    "form",
                )
            ],
            "target": "new",
            # The web client reads `dialog_size` from the action context, not
            # from the form arch.
            "context": {"dialog_size": "extra-large"},
        }

    # ------------------------------------------------------------------
    # The three buttons
    # ------------------------------------------------------------------

    def action_apply_selected(self):
        """Apply the ticked rows through each row's own `action_apply`.

        Ticked sensitive rows are applied too: they start unticked, so the tick
        is a deliberate, per-row decision.
        """
        self.ensure_one()
        self.line_ids.filtered("selected").suggestion_id.action_apply()
        return {"type": "ir.actions.act_window_close"}

    def action_not_now(self):
        """Close and leave the rows `pending`, so they stay in the queue."""
        self.ensure_one()
        if self.move_id:
            self.move_id.action_ai_defer_vendor_suggestions()
        return {"type": "ir.actions.act_window_close"}


class GoodaheadVendorDataReviewLine(models.TransientModel):
    _name = "goodahead.vendor.data.review.line"
    _description = "One invoice detail offered in the review dialog"
    # No `_order`: ordering by the non-stored related fields would fail in SQL.
    # Lines are created in the suggestion model's order, so `id` order matches.

    review_id = fields.Many2one(
        "goodahead.vendor.data.review",
        required=True,
        ondelete="cascade",
        index=True,
    )
    suggestion_id = fields.Many2one(
        "goodahead.vendor.data.suggestion",
        required=True,
        ondelete="cascade",
        help="The real row. Everything shown on this line is a related field "
        "of it and the apply is its own method — this line is a tick box and "
        "nothing else.",
    )
    selected = fields.Boolean(
        string="Apply",
        help="Pre-ticked for everything except a bank account and a tax id.",
    )
    field_label = fields.Char(
        related="suggestion_id.field_label", string="Detail", readonly=True
    )
    invoice_value = fields.Char(
        related="suggestion_id.invoice_value",
        string="On this invoice",
        readonly=True,
    )
    record_value = fields.Char(
        related="suggestion_id.record_value",
        string="On the vendor",
        readonly=True,
    )
    is_sensitive = fields.Boolean(
        related="suggestion_id.is_sensitive", string="Money", readonly=True
    )
    field_name = fields.Char(related="suggestion_id.field_name", readonly=True)
    note = fields.Char(
        related="suggestion_id.note", string="Full note", readonly=True
    )
    why = fields.Char(compute="_compute_why", string="Why")

    @api.depends("suggestion_id")
    def _compute_why(self):
        """A short phrase saying what applying the row does to the vendor.

        Kept short so it does not squeeze the value columns; the full note is
        available as an optional column.
        """
        for line in self:
            if line.is_sensitive:
                line.why = _("Confirm it first")
                continue
            if line.field_name in ("phone", "email", "website"):
                line.why = _("Added, not replaced")
                continue
            line.why = _("Replaces the record")
