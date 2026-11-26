// Copyright 2026 Goodahead Oy
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const WIZARD = "goodahead.invoice.send.wizard";
const LINE = "goodahead.invoice.send.wizard.line";
/** Spec §6 Mass send now: an outcome shows ≤ 2 s after APIX answered. */
export const POLL_INTERVAL_MS = 1000;
/** Consecutive failed polls before "Status unavailable". */
export const MAX_FAILURES = 3;
const ENDED = ["accepted", "sent", "refused", "failed", "cancelled", "downloaded", "in_progress"];

/**
 * Live status of the mass wizard's "Send now" (SCR-06 sending, contract §3.4).
 *
 * Calls `get_run_status` on the wizard through the ORM service once when it
 * opens and then every second while the dialog is open (no custom HTTP route);
 * stops when every row ended or the dialog closes. A failed call is retried at
 * the next tick; three in a row show "Status unavailable" — the invoices keep
 * sending in the background either way. Refused and failed rows offer Fix
 * (SCR-07, which returns to the wizard) and Retry (queued again in the run).
 */
export class ApixSendRunStatus extends Component {
    static template = "goodahead_invoice_send_apix.ApixSendRunStatus";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ status: null, failures: 0 });
        this.timer = null;
        this.polling = false;
        this.alive = true;
        onWillStart(() => this.poll());
        onMounted(() => {
            if (!this.finished) {
                this.start();
            }
        });
        onWillUnmount(() => {
            this.alive = false;
            this.stop();
        });
    }

    get finished() {
        const status = this.state.status;
        return Boolean(status && status.total && status.done >= status.total);
    }

    get unavailable() {
        return this.state.failures >= MAX_FAILURES;
    }

    get summary() {
        const lines = this.state.status?.lines || [];
        const count = (states) => lines.filter((line) => states.includes(line.state)).length;
        return _t("%(sent)s sent · %(refused)s refused · %(failed)s failed", {
            sent: count(["accepted", "sent"]),
            refused: count(["refused"]),
            failed: count(["failed", "cancelled"]),
        });
    }

    start() {
        if (this.timer || !this.alive) {
            return;
        }
        this.timer = browser.setInterval(() => this.poll(), POLL_INTERVAL_MS);
    }

    stop() {
        if (this.timer) {
            browser.clearInterval(this.timer);
        }
        this.timer = null;
    }

    async poll() {
        if (this.polling || !this.alive) {
            return;
        }
        this.polling = true;
        let status;
        try {
            status = await this.orm.silent.call(WIZARD, "get_run_status", [
                [this.props.record.resId],
            ]);
        } catch {
            this.state.failures++;
            return;
        } finally {
            this.polling = false;
        }
        if (!this.alive) {
            return;
        }
        this.state.failures = 0;
        this.state.status = status;
        if (this.finished) {
            this.stop();
        }
    }

    stateText(line) {
        const reason = line.message || line.code;
        switch (line.state) {
            case "queued":
                return _t("Queued");
            case "running":
                return _t("Sending…");
            case "accepted": {
                const text = line.batch_id
                    ? _t("Accepted by APIX · batch %s", line.batch_id)
                    : _t("Accepted by APIX");
                return line.warnings ? `${text} · ${_t("warning: %s", line.warnings)}` : text;
            }
            case "sent":
                return line.channel === "email" ? _t("Sent by email") : _t("Sent");
            case "refused":
                return _t("Refused: %s", reason);
            case "failed":
                return _t("Failed: %s", reason);
            case "cancelled":
                return _t("Cancelled");
            case "in_progress":
                return _t("In progress — %s", line.message);
            default:
                return line.state;
        }
    }

    stateClass(line) {
        if (["accepted", "sent"].includes(line.state)) {
            return "text-success";
        }
        if (["refused", "failed"].includes(line.state)) {
            return "text-danger";
        }
        return ENDED.includes(line.state) ? "text-warning" : "text-muted";
    }

    async retry(line) {
        await this.orm.call(LINE, "action_retry", [[line.line_id]]);
        await this.poll();
        if (!this.finished) {
            this.start();
        }
    }

    async fix(line) {
        const action = await this.orm.call(LINE, "action_open_fix", [[line.line_id]]);
        await this.action.doAction(action);
    }
}

export const apixSendRunStatus = {
    component: ApixSendRunStatus,
    displayName: _t("APIX sending status"),
    supportedTypes: ["char"],
};

registry.category("fields").add("apix_send_run_status", apixSendRunStatus);
