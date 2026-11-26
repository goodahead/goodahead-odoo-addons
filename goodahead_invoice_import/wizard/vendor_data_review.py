# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The Review dialog — one screen that says what the invoice claims, what the
vendor record holds, and which of the two is about to win.

🔴 IT EXISTS BECAUSE THE RAW LIST IN A DIALOG WAS NOT FOR A HUMAN. "Review" used
to open `goodahead.vendor.data.suggestion` in its own list view, inside a
`target: new` dialog, and that was not readable for a human and not like
the design, which called for a wizard. Measured on the same screen: NINE columns — Vendor, Detail, On this invoice, On
the vendor record, Money detail, Bill, Note, State and two buttons — in a dialog
narrower than the queue page the view was drawn for. The vendor repeated on
every row although every row had the same vendor, and the two columns the screen
exists for ("On this invoice", "On the vendor record") were squeezed to
`ar@northwind….` and `(212) 555-0…` by a free-text note column.

So: a wizard with the vendor said ONCE in the heading, five pinned columns, and
three buttons that each mean something different.

🔴 WHAT THIS DOES NOT DO IS THE POINT. It holds no rules of its own. Every line
points at an existing `goodahead.vendor.data.suggestion` row and Apply calls
that row's own `action_apply`, so the add-beside-never-replace rule for phone,
email and website, the state lookup that refuses rather than invents, and the
bank-account path all stay in ONE place. A second implementation of "apply a
suggestion" is how the two drift apart and the dangerous one is the one nobody
re-read.

🔴 NOTHING SENSITIVE IS PRE-TICKED — a bank account and a tax id arrive
unticked, every time, and `_open_for` is where that is decided. That is the
whole protection in this dialog: F27 asks for a DELIBERATE ACT on a money
detail, and ticking a box that was not ticked for you is that act. ⚠️ Note the
difference from the queue's own "Apply ticked" button, which REFUSES sensitive
rows outright: there the ticks are the list's record-selection checkboxes and
select-all is one click, so a bank account could ride along in a batch of nine
address corrections without anyone looking at it. Here each row's box is a value
on the row, and it starts false.
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
        """Drives one red line above the table.

        🔴 THE WARNING DOES NOT LIVE IN A COLUMN, AND THAT IS MEASURED. A list
        Char column gets whatever the pinned columns leave, and in this dialog
        that was ~130 px — the per-row reason read `Added beside — the store…`.
        A truncated warning about invoice fraud is worse than none, because it
        looks like it was said. So the per-row text is a short phrase and the
        sentence that matters is a full-width banner that cannot be clipped.
        """
        for wizard in self:
            wizard.has_sensitive = any(wizard.line_ids.mapped("is_sensitive"))

    @api.depends("partner_id", "move_id", "line_ids")
    def _compute_heading(self):
        """The vendor and the bill, said ONCE, at the top.

        The list view repeated both on every row; with one vendor per dialog
        that is two columns of the same word, and they were the two columns
        squeezing the ones a person is actually comparing.
        """
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

        ``suggestions`` is scoped to ONE vendor by both callers — the bill form
        passes that bill's rows, the queue passes a vendor's rows. The heading
        and the comparison both assume it, so this reads the vendor off the rows
        rather than taking it as an argument: one source, and it cannot disagree
        with what is displayed.
        """
        pending = suggestions.filtered(lambda row: row.state == "pending")
        if not pending:
            # 🔴 NEVER AN EMPTY DIALOG. Somebody else can have dealt with these
            # between the page loading and the click — the queue is explicitly a
            # shared worklist — and an empty modal reads like a broken button.
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
                            # 🔴 THE ONE LINE THE SAFETY RULE LIVES ON.
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
                        "goodahead_invoice_import."
                        "view_vendor_data_review_form"
                    ).id,
                    "form",
                )
            ],
            "target": "new",
            # ⚠️ `dialog_size` is read from the action's CONTEXT, not from the
            # form arch (`web/.../action_service.js:1072`). Without it the
            # default dialog is `lg`, and five columns of real addresses and
            # phone numbers in it is the squeeze this dialog was rewritten to
            # stop.
            "context": {"dialog_size": "extra-large"},
        }

    # ------------------------------------------------------------------
    # The three buttons
    # ------------------------------------------------------------------

    def action_apply_selected(self):
        """Apply the ticked rows, through each row's own `action_apply`.

        A ticked SENSITIVE row IS applied here, and that is the design rather
        than an oversight: it arrived unticked, so a person ticked it one row at
        a time with the invoice value and the stored value side by side. That is
        the deliberate act F27 asks for. (The queue's bulk button is stricter
        for the opposite reason — see this module's docstring.)
        """
        self.ensure_one()
        self.line_ids.filtered("selected").suggestion_id.action_apply()
        return {"type": "ir.actions.act_window_close"}

    def action_not_now(self):
        """Close and hand the rows on. 🔴 THEY STAY `pending` — that is what
        puts them in the queue. Dismissing here would answer the question on
        behalf of whoever owns vendor data, and the person who opened this
        dialog is usually the one person who should not have to."""
        self.ensure_one()
        if self.move_id:
            self.move_id.action_ai_defer_vendor_suggestions()
        return {"type": "ir.actions.act_window_close"}


class GoodaheadVendorDataReviewLine(models.TransientModel):
    _name = "goodahead.vendor.data.review.line"
    _description = "One invoice detail offered in the review dialog"
    # 🔴 NO `_order` OF ITS OWN, AND THAT IS DELIBERATE RATHER THAN LAZY. The
    # obvious one — `is_sensitive desc, field_label, id`, matching the queue —
    # would order by RELATED fields, which are not stored and therefore have no
    # column: the `ORDER BY` is SQL and it would fail at read time, in the
    # dialog, with everything green up to the click. Default `id` ordering gives
    # the same result for free, because `_open_for` creates the lines in the
    # order `goodahead.vendor.data.suggestion._order` already returned them —
    # money first.

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
        """A SHORT phrase per row, because a long one costs the two columns the
        screen exists for.

        🔴 MEASURED, IN THIS DIALOG, AFTER THE FIRST ATTEMPT. The obvious thing
        was to show `goodahead.vendor.data.suggestion.note` — but that is a full
        sentence ("Applying this records the value as another way the vendor is
        reached; it does not replace what is on the contact."), a list Char
        column sizes to its content, and in a 1140 px modal it pushed "On this
        invoice" down to `ar@krown…` and `(973) 305…`. That is exactly the
        failure this stage was opened to remove, reproduced one layer in. The
        long note is still a click away under the optional-columns toggle.

        The phrases are short on purpose and say the CONSEQUENCE, not the
        mechanism: what happens to the vendor record if this row is ticked.
        """
        for line in self:
            if line.is_sensitive:
                line.why = _("Confirm it first")
                continue
            if line.field_name in ("phone", "email", "website"):
                line.why = _("Added, not replaced")
                continue
            line.why = _("Replaces the record")
