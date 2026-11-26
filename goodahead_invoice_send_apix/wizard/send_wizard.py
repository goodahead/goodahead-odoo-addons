# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The mass sending wizard.

Opened on a selection of the invoice list ("Send e-invoices…"). On open every
selected invoice the user may read becomes one line, classified from stored
data only per company and transmit method:

    ready · not_ready · already_sent · in_progress · sending_off · skipped ·
    not_posted

Only ready lines are included by default; already-sent ones only after the
warning. "Send now" and "Send in the background" queue the included lines
(one job per invoice, in its own company, ``_apix_queue``) — the background
is recommended above the lowest background threshold of the selection's
companies, and both are always offered. Closing the wizard changes nothing:
queued sends continue.
"""

import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_datetime

from ..models.account_move import (
    ALREADY_SENT,
    NOT_CUSTOMER_DOCUMENT,
    NOT_POSTED,
    NOT_READY,
    SEND_IN_PROGRESS,
    SENDING_OFF,
)
from ..models.account_move_queue import JOB_CHANNELS, PRIORITY
from ..models.goodahead_invoice_send_attempt import (
    ATTENTION_STATES,
    IN_PROGRESS,
    split_reason,
)

WIZARD = "goodahead.invoice.send.wizard"
CATEGORIES = [
    ("ready", "Ready"),
    ("not_ready", "Not ready"),
    ("already_sent", "Already sent"),
    ("in_progress", "In progress"),
    ("sending_off", "Not sendable — sending off"),
    ("skipped", "Skipped"),
    ("not_posted", "Draft or cancelled"),
]
#: The first refusal code found decides the category, in this order.
CATEGORY_BY_CODE = [
    (NOT_CUSTOMER_DOCUMENT, "not_posted"),
    (NOT_POSTED, "not_posted"),
    (SEND_IN_PROGRESS, "in_progress"),
    (SENDING_OFF, "sending_off"),
    (NOT_READY, "not_ready"),
    (ALREADY_SENT, "already_sent"),
]
DEFAULT_THRESHOLD = 20
#: No send of the run started for this long while some are queued: the
#: background worker is probably down (SAD §11).
WORKER_WAIT = timedelta(seconds=30)


def _refusal_text(refusal):
    """A refusal of ``_apix_check_sendable`` with its items, one line."""
    items = " ".join(item["message"] for item in refusal.get("items") or [] if "message" in item)
    return f"{refusal['message']} {items}".strip()


class GoodaheadInvoiceSendWizard(models.TransientModel):
    _name = WIZARD
    _description = "Send invoices through APIX"

    move_ids = fields.Many2many("account.move", string="Selected invoices")
    line_ids = fields.One2many("goodahead.invoice.send.wizard.line", "wizard_id")
    state = fields.Selection(
        [("review", "Review"), ("sending", "Sending"), ("queued", "Queued")],
        default="review",
        required=True,
    )
    run_uuid = fields.Char(readonly=True)
    included_count = fields.Integer(compute="_compute_counts")
    can_send = fields.Boolean(compute="_compute_counts")
    has_already_sent = fields.Boolean(compute="_compute_counts")
    background_threshold = fields.Integer(compute="_compute_background")
    recommend_background = fields.Boolean(compute="_compute_background")
    copy_warning = fields.Text(compute="_compute_copy_warning")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        context = self.env.context
        if "move_ids" in fields_list and context.get("active_model") == "account.move":
            # only what the user may read: other companies never reach the
            # wizard (record rules, allowed companies; AC-54)
            readable = self.env["account.move"].search([("id", "in", context.get("active_ids") or [])])
            values["move_ids"] = [(6, 0, readable.ids)]
        return values

    @api.model_create_multi
    def create(self, vals_list):
        wizards = super().create(vals_list)
        for wizard in wizards:
            wizard._apix_classify()
        return wizards

    @api.depends("line_ids.include", "line_ids.category")
    def _compute_counts(self):
        for wizard in self:
            wizard.included_count = len(wizard.line_ids.filtered("include"))
            wizard.can_send = bool(wizard.included_count)
            wizard.has_already_sent = any(
                line.category == "already_sent" for line in wizard.line_ids
            )

    @api.depends("line_ids.company_id", "included_count")
    def _compute_background(self):
        Connection = self.env["goodahead.apix.connection"].sudo()
        for wizard in self:
            # read with sudo: the field is hidden from Accountants
            thresholds = [
                Connection._apix_for_company(company).background_threshold
                for company in wizard.line_ids.company_id
            ]
            threshold = min([t for t in thresholds if t] or [DEFAULT_THRESHOLD])
            wizard.background_threshold = threshold
            wizard.recommend_background = wizard.included_count > threshold

    @api.depends("line_ids.company_id")
    def _compute_copy_warning(self):
        Connection = self.env["goodahead.apix.connection"]
        for wizard in self:
            warnings = []
            for company in wizard.line_ids.company_id:
                connection = Connection._apix_for_company(company)
                warning = connection._apix_copy_warning() if connection else False
                if warning:
                    warnings.append(f"{company.name}: {warning}")
            wizard.copy_warning = "\n".join(warnings) or False

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _apix_classify(self):
        """One line per selected invoice the user may read (record rules
        and allowed companies: other companies never reach the wizard)."""
        self.ensure_one()
        moves = self.env["account.move"].search(
            [("id", "in", self.move_ids.ids)], order="company_id, name, id"
        )
        Line = self.env["goodahead.invoice.send.wizard.line"]
        self.line_ids.unlink()
        Line.create([dict(Line._apix_classify_values(move), wizard_id=self.id) for move in moves])

    def _apix_after_fix(self, move):
        """The fix-it wizard returns here (Flow G): re-classify its line."""
        self.ensure_one()
        for line in self.line_ids.filtered(lambda line: line.move_id == move):
            line.write(line._apix_classify_values(move))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_include_already_sent(self, line_ids=None):
        """Include already-sent lines (all, or ``line_ids``) after the
        warning the button shows."""
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: line.category == "already_sent")
        if line_ids:
            lines = lines.filtered(lambda line: line.id in line_ids)
        lines.write({"include": True})
        return self._reopen()

    def action_send_now(self):
        """Queue the included invoices, send-now priority; the wizard stays
        open."""
        self._apix_queue_included("mass_now")
        self.state = "sending"
        return self._reopen()

    def action_send_background(self):
        """Queue the included invoices in the background; tell how many and
        close."""
        count = self._apix_queue_included("mass_background")
        self.state = "queued"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _(
                    "%s invoices queued. Outcomes appear on each invoice's Delivery tab; "
                    "failed ones in Failed sends.", count,
                ),
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _apix_queue_included(self, origin):
        """Re-check and queue the included lines per company under one new
        run; the number queued."""
        self.ensure_one()
        if not self.can_send:
            raise UserError(_("No invoice in this selection can be sent now."))
        self.run_uuid = str(uuid.uuid4())
        lines = self.line_ids.filtered("include")._apix_recheck()
        queued = 0
        for company, company_lines in lines.grouped("company_id").items():
            moves = company_lines.move_id.with_company(company)
            result = moves._apix_queue(self.run_uuid, origin, PRIORITY[origin])
            queued += len(result["queued"])
            company_lines._apix_after_queue(result)
        return queued

    # ------------------------------------------------------------------
    # Run status, polled every second by the widget
    # ------------------------------------------------------------------

    @api.readonly
    def get_run_status(self):
        """The live status of this wizard's run: one row per invoice (its
        latest attempt of the run, read fresh), plus the invoices another
        send held when the run was queued. Only what the user may read."""
        self.ensure_one()
        return self._apix_run_status(fields.Datetime.now())

    def _apix_run_status(self, now):
        status = {"run_uuid": self.run_uuid, "total": 0, "done": 0,
                  "waiting_for_worker": False, "lines": []}
        if not self.run_uuid:
            return status
        attempts = self.env["goodahead.invoice.send.attempt"]._ledger_run_status(self.run_uuid)
        readable = set(self.env["account.move"].search(
            [("id", "in", list({attempt["move_id"] for attempt in attempts}))]
        ).ids)
        attempts = [attempt for attempt in attempts if attempt["move_id"] in readable]
        latest = {attempt["move_id"]: attempt for attempt in attempts}  # ordered by id
        lines = {line.move_id.id: line for line in self.line_ids}
        rows = [self._apix_status_row(lines.get(move_id), attempt)
                for move_id, attempt in latest.items()]
        rows += [
            self._apix_status_row(line, None)
            for line in self.line_ids
            if line.in_run and line.move_id.id not in latest
        ]
        order = {move_id: index for index, move_id in enumerate(lines)}
        rows.sort(key=lambda row: order.get(row["move_id"], len(order)))
        status.update(
            total=len(rows),
            done=len([row for row in rows if row["state"] not in IN_PROGRESS]),
            waiting_for_worker=self._apix_waiting_for_worker(attempts, latest, now),
            lines=rows,
        )
        return status

    def _apix_status_row(self, line, attempt):
        """One row of the status; ``attempt`` None: the invoice another send
        held when the run was queued (SCR-06 in progress conflict)."""
        if not attempt:
            return {
                "line_id": line.id, "move_id": line.move_id.id, "name": line.move_id.display_name,
                "company": line.company_id.name, "channel": line.channel, "state": "in_progress",
                "code": "send.in_progress", "message": line.missing or "", "batch_id": "",
                "warnings": "", "updated": False, "can_retry": False,
            }
        code, message = split_reason(attempt["reason"])
        updated = attempt["end_date"] or attempt["start_date"] or attempt["queued_date"]
        return {
            "line_id": line.id if line else False,
            "move_id": attempt["move_id"],
            "name": attempt["move_name"],
            "company": attempt["company_name"],
            "channel": attempt["channel"],
            "state": attempt["state"],
            "code": code,
            "message": message,
            "batch_id": attempt["batch_id"],
            "warnings": attempt["warnings"],
            "updated": fields.Datetime.to_string(updated) if updated else False,
            "can_retry": bool(line) and self.state == "sending"
            and attempt["state"] in ATTENTION_STATES,
        }

    @api.model
    def _apix_waiting_for_worker(self, attempts, latest, now):
        """No attempt of the run left ``queued`` for 30 s while some are
        queued (SAD §11): the job runner is probably down."""
        if not any(attempt["state"] == "queued" for attempt in latest.values()):
            return False
        moments = [attempt["start_date"] or attempt["queued_date"] for attempt in attempts]
        moments = [moment for moment in moments if moment]
        return bool(moments) and now - max(moments) >= WORKER_WAIT

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Send e-invoices"),
            "res_model": WIZARD,
            "res_id": self.id,
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
        }


class GoodaheadInvoiceSendWizardLine(models.TransientModel):
    _name = "goodahead.invoice.send.wizard.line"
    _description = "Invoice in the APIX sending wizard"
    _order = "company_id, id"

    wizard_id = fields.Many2one(WIZARD, required=True, ondelete="cascade")
    move_id = fields.Many2one("account.move", string="Invoice", required=True, readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    partner_id = fields.Many2one(related="move_id.partner_id", string="Customer")
    amount_total = fields.Monetary(related="move_id.amount_total")
    currency_id = fields.Many2one(related="move_id.currency_id")
    channel = fields.Selection(
        [("einvoice", "E-invoice"), ("letter", "Printed letter"), ("email", "Email")],
        readonly=True,
    )
    category = fields.Selection(CATEGORIES, readonly=True)
    missing = fields.Text(string="Reason", readonly=True)
    sent_info = fields.Char(string="Sent", readonly=True)
    include = fields.Boolean()
    attempt_id = fields.Many2one("goodahead.invoice.send.attempt", readonly=True)
    #: Queued with the run (or found held by another send at that moment).
    in_run = fields.Boolean(readonly=True)

    @api.model
    def _apix_classify_values(self, move):
        """Line values of ``move`` (stored data only, no APIX call)."""
        channel = move._apix_channel()
        values = {
            "move_id": move.id,
            "company_id": move.company_id.id,
            "channel": channel if channel in JOB_CHANNELS else False,
            "missing": False,
            "sent_info": False,
        }
        if channel not in JOB_CHANNELS:
            values.update(category="skipped", include=False, missing=_(
                "This transmit method cannot be sent from here."
            ))
            return values
        refusals = {refusal["code"]: refusal for refusal in move._apix_check_sendable(channel)}
        category = next(
            (category for code, category in CATEGORY_BY_CODE if code in refusals), "ready"
        )
        values.update(category=category, include=category == "ready")
        refusal = next((refusals[code] for code, cat in CATEGORY_BY_CODE
                        if cat == category and code in refusals), None)
        if category == "already_sent":
            values["sent_info"] = self._apix_sent_info(refusal["items"])
        elif category == "not_ready":
            values["missing"] = " ".join(item["message"] for item in refusal["items"])
        elif refusal:
            values["missing"] = refusal["message"]
        return values

    @api.model
    def _apix_sent_info(self, info):
        if info["before_install"]:
            return _("Sent before the add-on was installed (channel unknown)")
        channels = dict(self._fields["channel"]._description_selection(self.env))
        channels.update(download=_("Download"), unknown=_("Unknown"))
        return _(
            "%(channel)s, %(when)s by %(by)s",
            channel=channels.get(info["channel"], info["channel"]),
            when=format_datetime(self.env, info["when"]) if info["when"] else "",
            by=info["by"] or "",
        )

    def _apix_recheck(self):
        """The included lines still sendable now; the others are
        re-classified and left out."""
        sendable = self.browse()
        for line in self:
            refusals = line.move_id._apix_check_sendable(
                line.channel, resend_confirmed=line.category == "already_sent"
            )
            if refusals:
                values = line._apix_classify_values(line.move_id)
                # held by a send started meanwhile: shown in the run's status
                values.update(include=False, in_run=values["category"] == "in_progress")
                line.write(values)
                continue
            sendable |= line
        return sendable

    def _apix_after_queue(self, result):
        attempts = {attempt.move_id.id: attempt for attempt in result["queued"]}
        self.in_run = True
        for line in self:
            holder = result["in_progress"].get(line.move_id.id)
            if holder:
                line.write({"category": "in_progress", "include": False,
                            "missing": holder.message(self.env)})
                continue
            line.attempt_id = attempts.get(line.move_id.id)

    def action_open_fix(self):
        """SCR-07 for this line's invoice; saving returns to the wizard,
        which re-classifies the line (Flow G)."""
        self.ensure_one()
        action = self.move_id.action_apix_open_fix()
        action["context"] = dict(
            action["context"],
            default_return_to="mass_wizard",
            default_return_res_model=WIZARD,
            default_return_res_id=self.wizard_id.id,
        )
        return action

    def action_retry(self):
        """Queue this refused or failed invoice again under the wizard's run,
        send-now priority, after checking it again."""
        self.ensure_one()
        attempt = self.attempt_id
        attempt.invalidate_recordset(["state"])
        if self.wizard_id.state != "sending" or attempt.state not in ATTENTION_STATES:
            raise UserError(_("Only a refused or failed invoice of this sending can be retried."))
        move = self.move_id.with_company(self.company_id)
        refusals = move._apix_check_sendable(
            self.channel, resend_confirmed=self.category == "already_sent"
        )
        if refusals:
            raise UserError("\n".join(_refusal_text(refusal) for refusal in refusals))
        self._apix_after_queue(
            move._apix_queue(self.wizard_id.run_uuid, "mass_now", PRIORITY["mass_now"])
        )
        return True

    def action_include(self):
        """Include this already-sent line after the warning."""
        self.ensure_one()
        return self.wizard_id.action_include_already_sent(line_ids=self.ids)
