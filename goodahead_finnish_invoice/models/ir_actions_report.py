# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import models
from odoo.tools.pdf import merge_pdf


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    INVOICE_REPORTS = (
        "account.report_invoice",
        "account.report_invoice_with_payments",
    )

    def _render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        """Flag a run that is printing Finnish customer invoices.

        The page geometry cannot hang off the report action:
        ``account.account_invoices`` prints vendor bills as well, and those
        still use the stock layout, which needs the company's own format. The
        flag is read back in get_paperformat below, so only the documents this
        module lays out get its format.

        One wkhtmltopdf call carries one paper format, so a selection holding
        both kinds of document is split into runs of the same kind, rendered a
        run at a time and merged. Splitting by run rather than by kind keeps the
        pages in the order they were asked for. A selection that is all one kind
        — which is what printing from either list view gives — is still a single
        call.
        """
        report = self._get_report(report_ref)
        if report.report_name in self.INVOICE_REPORTS and res_ids:
            # exists(): a stale id in the selection would otherwise raise
            # MissingError on the first field read.
            moves = self.env["account.move"].browse(res_ids).exists()
            runs = self._fi_split_by_layout(moves)
            if len(runs) > 1:
                streams = [
                    self._render_qweb_pdf(report_ref, res_ids=ids, data=data)[0]
                    for _is_finnish, ids in runs
                ]
                return merge_pdf(streams), "pdf"
            if runs and runs[0][0]:
                # The flag has to travel on the report record: _get_report hands
                # a record reference straight back, so a context set only on
                # self never reaches get_paperformat below.
                report_ref = report.with_context(goodahead_fi_invoice=True)
                self = self.with_context(goodahead_fi_invoice=True)
        return super(IrActionsReport, self)._render_qweb_pdf(
            report_ref, res_ids=res_ids, data=data
        )

    @staticmethod
    def _fi_split_by_layout(moves):
        """Group consecutive moves that share a layout: [(is_finnish, ids), ...]."""
        runs = []
        for move in moves:
            is_finnish = move._fi_uses_finnish_invoice_report()
            if runs and runs[-1][0] == is_finnish:
                runs[-1][1].append(move.id)
            else:
                runs.append((is_finnish, [move.id]))
        return runs

    def get_paperformat(self):
        if self.env.context.get("goodahead_fi_invoice"):
            paperformat = self.env.ref(
                "goodahead_finnish_invoice.paperformat_finnish_invoice",
                raise_if_not_found=False,
            )
            if paperformat:
                return paperformat
        return super().get_paperformat()
