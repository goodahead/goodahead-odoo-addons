// Copyright 2026 Goodahead Oy
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import { describe, expect, test } from "@odoo/hoot";
import { advanceTime, animationFrame, freezeTime } from "@odoo/hoot-mock";
import {
    contains,
    defineModels,
    fields,
    models,
    mountView,
    onRpc,
} from "@web/../tests/web_test_helpers";
// `account` brings `mail`, whose services start in every mock environment.
import { defineMailModels } from "@mail/../tests/mail_test_helpers";

class SendWizard extends models.Model {
    _name = "goodahead.invoice.send.wizard";

    state = fields.Selection({
        selection: [
            ["review", "Review"],
            ["sending", "Sending"],
        ],
    });
    run_uuid = fields.Char();

    _records = [{ id: 1, state: "sending", run_uuid: "zz-run-0001" }];
}

const ARCH = `<form>
    <field name="state" invisible="1"/>
    <field name="run_uuid" widget="apix_send_run_status" nolabel="1"/>
</form>`;

function row(lineId, state, extra = {}) {
    return {
        line_id: lineId,
        move_id: 100 + lineId,
        name: `INV/2026/000${lineId}`,
        company: "Example Oy",
        channel: "einvoice",
        state,
        code: "",
        message: "",
        batch_id: "",
        warnings: "",
        updated: "2026-11-25 10:00:00",
        can_retry: state === "refused" || state === "failed",
        ...extra,
    };
}

function status(lines, extra = {}) {
    const ended = lines.filter((line) => !["queued", "running"].includes(line.state));
    return {
        run_uuid: "zz-run-0001",
        total: lines.length,
        done: ended.length,
        waiting_for_worker: false,
        lines,
        ...extra,
    };
}

async function mountWizard() {
    // only advanceTime moves the clock: a slow test run must not add polls
    freezeTime();
    const view = await mountView({
        resModel: "goodahead.invoice.send.wizard",
        resId: 1,
        type: "form",
        arch: ARCH,
    });
    await animationFrame();
    return view;
}

async function tick() {
    await advanceTime(1000);
    await animationFrame();
}

describe.current.tags("desktop");
defineMailModels();
defineModels([SendWizard]);

describe("ApixSendRunStatus", () => {
    test("sending — loading: rows and progress, polled every second", async () => {
        const answers = [
            status([row(1, "queued"), row(2, "queued")]),
            status([row(1, "running"), row(2, "queued")]),
            status([row(1, "accepted", { batch_id: "ZZBATCH-1" }), row(2, "queued")]),
        ];
        let calls = 0;
        onRpc("goodahead.invoice.send.wizard", "get_run_status", () => answers[calls++]);
        await mountWizard();
        expect(calls).toBe(1);
        expect(".o-apix-run-row").toHaveCount(2);
        expect(".o-apix-run-progress").toHaveText("0 / 2 done");
        await tick();
        expect(calls).toBe(2);
        expect(".o-apix-run-row:eq(0) .o-apix-run-state").toHaveText("Sending…");
        await tick();
        expect(calls).toBe(3);
        expect(".o-apix-run-row:eq(0) .o-apix-run-state").toHaveText(
            "Accepted by APIX · batch ZZBATCH-1"
        );
        expect(".o-apix-run-progress").toHaveText("1 / 2 done");
    });

    test("success — done: polling stops and the summary shows", async () => {
        let calls = 0;
        onRpc("goodahead.invoice.send.wizard", "get_run_status", () => {
            calls++;
            if (calls < 2) {
                return status([row(1, "queued"), row(2, "queued")]);
            }
            return status([
                row(1, "accepted"),
                row(2, "refused", { code: "send.refused", message: "ZZ invalid address" }),
            ]);
        });
        await mountWizard();
        await tick();
        expect(calls).toBe(2);
        expect(".o-apix-run-done").toHaveText("1 sent · 1 refused · 0 failed");
        await tick();
        await tick();
        await tick();
        expect(calls).toBe(2, { message: "no poll once every row ended" });
    });

    test("sending — row error: Retry queues again and polling resumes", async () => {
        let calls = 0;
        const retried = [];
        onRpc("goodahead.invoice.send.wizard", "get_run_status", () => {
            calls++;
            if (retried.length) {
                return status([row(1, "queued")]);
            }
            return status([row(1, "failed", { code: "send.unreachable", message: "ZZ down" })]);
        });
        onRpc("goodahead.invoice.send.wizard.line", "action_retry", ({ args }) => {
            retried.push(args[0]);
            return true;
        });
        await mountWizard();
        expect(".o-apix-run-row:eq(0) .o-apix-run-state").toHaveText("Failed: ZZ down");
        expect(".o-apix-run-fix").toHaveCount(1);
        await contains(".o-apix-run-retry").click();
        await animationFrame();
        expect(retried).toEqual([[1]]);
        expect(".o-apix-run-row:eq(0) .o-apix-run-state").toHaveText("Queued");
        const before = calls;
        await tick();
        expect(calls).toBe(before + 1, { message: "polling again after the retry" });
    });

    test("sending — status unavailable after three failed polls; sending continues", async () => {
        let calls = 0;
        let fail = true;
        onRpc("goodahead.invoice.send.wizard", "get_run_status", () => {
            calls++;
            if (fail) {
                throw new Error("ZZ poll failed");
            }
            return status([row(1, "queued")]);
        });
        await mountWizard();
        await tick();
        expect(".o-apix-run-unavailable").toHaveCount(0);
        await tick();
        expect(calls).toBe(3);
        expect(".o-apix-run-unavailable").toHaveText(
            "Status unavailable — the invoices keep sending in the background"
        );
        fail = false;
        await tick();
        expect(".o-apix-run-unavailable").toHaveCount(0);
        expect(".o-apix-run-row").toHaveCount(1);
    });

    test("sending — worker waiting: the banner shows", async () => {
        onRpc("goodahead.invoice.send.wizard", "get_run_status", () =>
            status([row(1, "queued")], { waiting_for_worker: true })
        );
        await mountWizard();
        expect(".o-apix-run-waiting").toHaveCount(1);
    });

    test("closed while sending: no poll after the dialog closes", async () => {
        let calls = 0;
        onRpc("goodahead.invoice.send.wizard", "get_run_status", () => {
            calls++;
            return status([row(1, "queued")]);
        });
        const view = await mountWizard();
        await tick();
        expect(calls).toBe(2);
        view.__owl__.destroy();
        await tick();
        await tick();
        expect(calls).toBe(2);
    });
});
