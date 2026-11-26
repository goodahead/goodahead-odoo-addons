# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Sending one invoice now (contract §1.1 – §1.3, §10.4; ADR-0004, Flow A).

``account.move._apix_send_now(origin, channel=None, resend_confirmed=False,
pdf=None) -> SendResult`` — the answer comes back in the same action and is
recorded before anyone sees it:

    checks (nothing written) → claim → render the PDF → build the values →
    write the message → schema gate → package → SendInvoiceZIP → outcome

Every step after the claim ends in the ledger (own short transactions,
ADR-0007), so the outcome survives whatever the caller's transaction does;
this code never commits the caller's transaction. Everything goes through the
invoice's own company: its connection, its sender details.
"""

import base64
import logging
from dataclasses import dataclass, field

from odoo import _, models
from odoo.exceptions import AccessError

from odoo.addons.goodahead_apix.lib.apix_client import (
    ApixError,
    ApixNoAnswer,
    ApixUnexpected,
    ApixUnreachable,
)
from odoo.addons.goodahead_einvoice_base.lib import finvoice

from ..lib.masking import mask_exchange
from ..lib.package import build_package, safe_file_name
from .account_move import ALREADY_SENT, NOT_READY, SEND_IN_PROGRESS
from .apix_connection import ADMIN_GROUP
from .goodahead_invoice_send_attempt import DEVELOPER_GROUP
from .ledger import Holder

_logger = logging.getLogger(__name__)

SCHEMA_INVALID = "send.schema_invalid"
REFUSED = "send.refused"
CREDENTIALS_REFUSED = "send.credentials_refused"
NO_CONTRACT = "send.no_contract"
NO_CREDIT = "send.no_credit"
UNREACHABLE = "send.unreachable"
NO_ANSWER = "send.no_answer"
UNEXPECTED = "send.unexpected"
CHANNEL_UNSUPPORTED = "send.channel_unsupported"
#: Codes that are about the company's connection, not the invoice: they
#: pause the company's sending.
CONNECTION_CODES = (CREDENTIALS_REFUSED, NO_CONTRACT, NO_CREDIT, UNREACHABLE)
#: transmit method -> the APIX channel it sends on.
METHOD_CHANNEL = {"einvoice": "einvoice", "letter": "letter"}
#: channel -> the writer's receiver (the print service for letters, AC-56).
CHANNEL_RECEIVER = {"einvoice": "einvoice", "letter": "print"}


@dataclass
class SendResult:
    """What a send answers."""

    outcome: str
    #: the ``ledger.AttemptView`` of the recorded attempt (plain values read
    #: on the ledger's cursor), never a record to read in this transaction
    attempt: object = None
    code: str = ""
    message: str = ""
    refusals: list = field(default_factory=list)
    holder: dict = None
    batch_id: str = ""
    document_id: str = ""
    credits: float = 0.0
    warnings: list = field(default_factory=list)


@dataclass
class _Verdict:
    """The outcome of one send before it is recorded."""

    state: str
    code: str = ""
    message: str = ""
    values: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


class AccountMove(models.Model):
    _inherit = "account.move"

    def _apix_send_now(self, origin, channel=None, resend_confirmed=False, pdf=None):
        """Send this invoice through APIX now; see the module docstring."""
        self.ensure_one()
        channel = channel or METHOD_CHANNEL.get(self.transmit_method)
        if channel not in CHANNEL_RECEIVER:
            return SendResult(
                outcome="not_sent",
                code=CHANNEL_UNSUPPORTED,
                message=_(
                    "This invoice goes by email: send it with Send & Print, or choose "
                    "e-invoice or printed letter as its transmit method."
                ),
            )
        refused = self._apix_refused(channel, resend_confirmed)
        if refused:
            return refused
        Attempt = self.env["goodahead.invoice.send.attempt"]
        claim = Attempt._ledger_claim(self, channel, origin, self.env.user)
        if isinstance(claim, Holder):
            return _in_progress(claim, self.env)
        try:
            verdict = self._apix_send_claimed(claim, channel, pdf)
        except Exception as exc:  # recorded, never raised past the claim
            _logger.exception("APIX send of invoice %s failed unexpectedly", self.id)
            verdict = _Verdict("failed", UNEXPECTED, _(
                "The send failed unexpectedly (%s); nothing was sent.", type(exc).__name__
            ))
        return self._apix_record_verdict(claim, verdict)

    # ------------------------------------------------------------------
    # The "Send via APIX" button
    # ------------------------------------------------------------------

    def action_apix_send(self):
        """Send this invoice now and answer with a sticky notification and a
        reload of the form; the already-sent confirmation first when
        it was sent before; the fix-it wizard after a not-ready refusal.
        Context ``apix_resend_confirmed`` = SCR-03 was confirmed."""
        self.ensure_one()
        resend = bool(self.env.context.get("apix_resend_confirmed"))
        result = self._apix_send_now("button", resend_confirmed=resend)
        codes = [refusal["code"] for refusal in result.refusals]
        if codes == [ALREADY_SENT]:
            return self._apix_already_sent_action()
        return _notification(*self._apix_outcome_notice(result), self._apix_next_action(result))

    def _apix_already_sent_action(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Already sent"),
            "res_model": "goodahead.invoice.send.already.sent",
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
            "context": {"default_move_id": self.id},
        }

    def _apix_next_action(self, result):
        """The fix-it wizard after a refusal it can fix, else a reload."""
        fixable = any(
            item.get("fix_at") == "fix_it"
            for refusal in result.refusals if refusal["code"] == NOT_READY
            for item in refusal["items"] or []
        )
        if fixable:
            return self.action_apix_open_fix()
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def _apix_outcome_notice(self, result):
        """``(type, message)`` of the SCR-01 notification for ``result``."""
        if result.outcome == "accepted":
            return self._apix_accepted_notice(result)
        if result.outcome == "in_progress":
            return "warning", result.message
        if result.outcome == "not_sent":
            kind = "warning" if result.code in (CHANNEL_UNSUPPORTED,) else "danger"
            return kind, result.message
        if result.code in CONNECTION_CODES and not self.env.user.has_group(ADMIN_GROUP):
            return "danger", _(
                "Not sent — the company's APIX connection is not working. An Administrator "
                "has been notified."
            )
        return "danger", _("Not sent — %s", result.message)

    def _apix_accepted_notice(self, result):
        values = {"batch": result.batch_id, "credits": result.credits}
        if result.attempt.channel == "letter":
            message = _("Sent to the APIX print service — batch %(batch)s, %(credits)s credits",
                        **values)
        else:
            message = _("Sent to APIX — batch %(batch)s, %(credits)s credits", **values)
        if not result.warnings:
            return "success", message
        return "warning", _("%(message)s. Warning: %(warnings)s", message=message,
                            warnings=" ".join(result.warnings))

    def _apix_refused(self, channel, resend_confirmed):
        """A ``SendResult`` for the pre-send refusals, or None."""
        refusals = self._apix_check_sendable(channel, resend_confirmed)
        if not refusals:
            return None
        if [r["code"] for r in refusals] == [SEND_IN_PROGRESS]:
            holder = refusals[0]["items"]
            return SendResult(
                outcome="in_progress", code=SEND_IN_PROGRESS, message=refusals[0]["message"],
                holder={"user": holder["user"], "since": holder["since"]},
            )
        return SendResult(
            outcome="not_sent",
            code=refusals[0]["code"],
            message="\n".join(refusal["message"] for refusal in refusals),
            refusals=refusals,
        )

    # ------------------------------------------------------------------
    # Regenerate and check (developer view, SCR-13; AC-35, AC-36)
    # ------------------------------------------------------------------

    def _apix_regenerate_check(self):
        """The Finvoice message built from the current data and its schema
        check: ``{message, schema_result, problems, units_without_code}``.

        🔴 Writes no attempt, attachment or job and calls no one:
        the same build and schema gate as a send, nothing after it."""
        self.ensure_one()
        if not self.env.user.has_group(DEVELOPER_GROUP):
            raise AccessError(_("Regenerate and check is part of the developer view "
                                "(developer mode)."))
        channel = METHOD_CHANNEL.get(self.transmit_method) or "einvoice"
        values = self._apix_finvoice_values()
        result = {
            "message": b"",
            "schema_result": "invalid",
            "problems": [],
            "units_without_code": list(values.get("units_without_code") or []),
        }
        try:
            message = finvoice.write(values, receiver=CHANNEL_RECEIVER[channel])
        except finvoice.FinvoiceError as exc:
            result["problems"] = [exc.message]
            return result
        problems = finvoice.validate_schema(message)
        result.update(
            message=message,
            schema_result="invalid" if problems else "valid",
            problems=[f"{finvoice.explain(problem)} ({problem.get('message')})"
                      for problem in problems],
        )
        return result

    def action_apix_regenerate_check(self):
        """SCR-13: the result in a dialog with the message as a download."""
        self.ensure_one()
        result = self._apix_regenerate_check()
        dialog = self.env["goodahead.invoice.send.regenerate"].create({
            "move_id": self.id,
            "schema_result": result["schema_result"],
            "problems": "\n".join(result["problems"]) or False,
            "units_without_code": ", ".join(result["units_without_code"]) or False,
            "message_file": base64.b64encode(result["message"]) if result["message"] else False,
            "message_filename": f"{safe_file_name(self.name)}.xml",
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Regenerate and check"),
            "res_model": dialog._name,
            "res_id": dialog.id,
            "views": [[False, "form"]],
            "target": "new",
        }

    # ------------------------------------------------------------------
    # After the claim
    # ------------------------------------------------------------------

    def _apix_send_claimed(self, attempt, channel, pdf):
        """Build, check, pack and send; the ``_Verdict``. Nothing is written
        here: the caller records the verdict through the ledger."""
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company_id)
        # the package always names the image invoice.pdf
        _filename, pdf_bytes = pdf or self._apix_render_pdf()
        values = self._apix_finvoice_values(attempt_id=attempt.id)
        units = ", ".join(values.get("units_without_code") or [])
        base = {"units_without_code": units or False}
        try:
            message = finvoice.write(values, receiver=CHANNEL_RECEIVER[channel])
        except finvoice.FinvoiceError as exc:
            return _Verdict("failed", SCHEMA_INVALID, _(
                "The e-invoice could not be built: %s", exc.message
            ), dict(base, schema_result="invalid", schema_errors=exc.message))
        xml_name = f"{safe_file_name(self.name)}.xml"
        files = {"message_attachment_id": (xml_name, message, "application/xml")}
        problems = finvoice.validate_schema(message)
        if problems:
            details = "; ".join(
                f"{finvoice.explain(problem)} ({problem.get('message')})" for problem in problems
            )
            return _Verdict("failed", SCHEMA_INVALID, _(
                "The e-invoice could not be built: %s", details
            ), dict(base, schema_result="invalid", schema_errors="\n".join(
                f"{problem.get('path')}: {problem.get('message')}" for problem in problems
            )), files)
        package = build_package(message, pdf_bytes, self.name)
        files["package_attachment_id"] = (f"{safe_file_name(self.name)}.zip", package,
                                          "application/zip")
        base["schema_result"] = "valid"
        verdict = self._apix_call(connection, package)
        verdict.values = dict(base, **verdict.values)
        verdict.files = files
        return verdict

    def _apix_call(self, connection, package):
        """SendInvoiceZIP with the invoice company's connection, mapped by
        contract §10.4."""
        soft, ver = connection.interface_name, connection.interface_version
        request = mask_exchange(
            "PUT", f"/invoices?soft={soft}&ver={ver}&TraID={connection.transfer_id}&t=&d=",
            None, package,
        )
        try:
            answer = connection._client().send_invoice_zip(
                connection.transfer_id, connection.transfer_key, soft, ver, package
            )
        except ApixUnreachable as exc:
            return _Verdict("failed", UNREACHABLE, _(
                "APIX could not be reached; nothing was sent. (%s)", exc.message
            ), {"exchange_log": request})
        except ApixNoAnswer:
            return _Verdict("failed", NO_ANSWER, _(
                "APIX did not answer — the invoice may already have reached APIX. "
                "You can send it again."
            ), {"exchange_log": request})
        except (ApixUnexpected, ApixError) as exc:
            return _Verdict("failed", UNEXPECTED, _(
                "Unexpected answer from APIX: %s", exc.message
            ), {"exchange_log": request})
        exchange = "\n\n".join([request, mask_exchange("ANSWER", "", answer.status, answer.raw)])
        verdict = self._apix_answer_verdict(answer)
        verdict.values.update(exchange_log=exchange, apix_status_code=answer.status_code or False)
        if answer.saldo is not None:
            connection._apix_record_credit_balance(answer.saldo)
        return verdict

    def _apix_answer_verdict(self, answer):
        if answer.status == "OK":
            return self._apix_ok_verdict(answer)
        code = answer.status_code or ""
        ending = code[-2:]
        reason = answer.free_text or ""
        if ending == "04":
            return _Verdict("failed", CREDENTIALS_REFUSED, _(
                "APIX refused the company's transfer credentials (code %(code)s). %(text)s",
                code=code, text=reason,
            ))
        if ending == "03":
            return _Verdict("failed", NO_CONTRACT, _(
                "There is no APIX sending contract for this company (code %(code)s). %(text)s",
                code=code, text=reason,
            ))
        if ending in ("02", "11"):
            return _Verdict("failed", NO_CREDIT, _(
                "The company's APIX credit balance is used up (code %(code)s). %(text)s",
                code=code, text=reason,
            ))
        if ending in ("01", "12") or code.startswith("4"):
            return _Verdict("refused", REFUSED, _(
                "APIX refused the invoice (code %(code)s). %(text)s", code=code, text=reason,
            ))
        return _Verdict("failed", UNEXPECTED, _(
            "Unexpected answer from APIX (code %(code)s). %(text)s", code=code, text=reason,
        ))

    def _apix_ok_verdict(self, answer):
        rejected = [doc for doc in answer.documents if not doc["accepted"]]
        accepted = [doc for doc in answer.documents if doc["accepted"]]
        if rejected or (answer.rejected_count and not answer.accepted_count):
            texts = [self._apix_answer_text(doc) for doc in rejected]
            return _Verdict("refused", REFUSED, _(
                "APIX refused the invoice: %s", " ".join(t for t in texts if t) or _("no reason given")
            ))
        if not accepted and not answer.accepted_count:
            # OK without an accepted document is no proof it arrived (review R2)
            return _Verdict("failed", UNEXPECTED, _(
                "APIX answered OK but accepted no document (batch %s) — the invoice may "
                "already have reached APIX. Check before sending it again.",
                answer.batch_id or _("unknown"),
            ))
        warnings =[text for text in (self._apix_answer_text(doc) for doc in accepted) if text]
        document_id = accepted[0]["document_id"] if accepted else False
        return _Verdict("accepted", values={
            "apix_batch_id": answer.batch_id or False,
            "apix_document_id": document_id,
            "apix_credits": answer.cost_in_credits or 0.0,
            "warning_text": "\n".join(warnings) or False,
        }, warnings=warnings)

    def _apix_answer_text(self, document):
        """APIX's ValidateText in Finnish for a Finnish user, else English."""
        texts = document.get("texts") or {}
        lang = (self.env.user.lang or "")[:2]
        return texts.get(lang) or texts.get("en") or texts.get("fi") or ""

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _apix_record_verdict(self, attempt, verdict):
        values = dict(verdict.values)
        if verdict.code:
            values["reason"] = f"[{verdict.code}] {verdict.message}"
        Attempt = self.env["goodahead.invoice.send.attempt"]
        # an AttemptView: the row is not visible in this transaction (§1.4)
        recorded = Attempt._ledger_finish(attempt, verdict.state, values, verdict.files)
        self._apix_connection_after(verdict)
        result = SendResult(
            outcome=recorded.state, attempt=recorded, code=verdict.code,
            message=verdict.message, warnings=verdict.warnings,
        )
        if recorded.state == "accepted":
            result.batch_id = recorded.apix_batch_id
            result.document_id = recorded.apix_document_id
            result.credits = recorded.apix_credits
        return result

    def _apix_connection_after(self, verdict):
        """A connection-level code pauses the company's sending; an accepted
        send ends a pause early."""
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company_id)
        if not connection:
            return
        if verdict.code in CONNECTION_CODES:
            connection._apix_open_send_circuit(verdict.code, verdict.message)
            return
        if verdict.state == "accepted":
            connection._apix_close_send_circuit()


def _notification(kind, message, next_action):
    return {
        "type": "ir.actions.client",
        "tag": "display_notification",
        "params": {"type": kind, "message": message, "sticky": True, "next": next_action},
    }


def _in_progress(holder, env):
    return SendResult(
        outcome="in_progress",
        code=SEND_IN_PROGRESS,
        message=holder.message(env),
        holder={"user": holder.user_name, "since": holder.since},
    )
