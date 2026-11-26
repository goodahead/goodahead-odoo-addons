// Copyright 2026 Goodahead Oy
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import { describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import {
    contains,
    mockService,
    mountWithCleanup,
    onRpc,
} from "@web/../tests/web_test_helpers";
// `account` brings `mail`, whose services start in every mock environment.
import { defineMailModels } from "@mail/../tests/mail_test_helpers";

import { EinvoiceAttentionMenu } from "@goodahead_einvoice_base/components/einvoice_attention_menu/einvoice_attention_menu";

const ROUTE = "/goodahead_einvoice/attention";

function section(key, label, count, titles) {
    return {
        key,
        label,
        groups: "account.group_account_invoice",
        count,
        items: titles.map((title, index) => ({
            id: index + 1,
            title,
            subtitle: `${title} detail`,
            date: false,
        })),
        action: {
            type: "ir.actions.act_window",
            name: label,
            res_model: `zz.${key}`,
            views: [[false, "list"]],
        },
        item_action: {
            type: "ir.actions.act_window",
            res_model: `zz.${key}`,
            views: [[false, "form"]],
        },
    };
}

const IMPORTS = section("failed_imports", "Failed imports", 3, ["Example Supplier 10023"]);
const SENDS = section("failed_sends", "Failed sends", 2, ["INV/0416", "INV/0420"]);

describe.current.tags("desktop");
defineMailModels();
describe("EinvoiceAttentionMenu", () => {
    test("empty: no bubble when the total is 0", async () => {
        onRpc(ROUTE, () => ({ total: 0, sections: [] }));
        await mountWithCleanup(EinvoiceAttentionMenu);
        await animationFrame();
        expect(".o-einvoice-attention-toggle").toHaveCount(0);
    });

    test("default: the total in the bubble and one block per section", async () => {
        onRpc(ROUTE, () => ({ total: 5, sections: [IMPORTS, SENDS] }));
        await mountWithCleanup(EinvoiceAttentionMenu);
        await animationFrame();
        expect(".o-einvoice-attention-toggle .badge").toHaveText("5");
        await contains(".o-einvoice-attention-toggle").click();
        expect(".o-einvoice-attention-section").toHaveCount(2);
        expect(".o-einvoice-attention-section:eq(0) .o-einvoice-attention-heading").toHaveText(
            "Failed imports (3)"
        );
        expect(".o-einvoice-attention-section:eq(1) .o-einvoice-attention-heading").toHaveText(
            "Failed sends (2)"
        );
        expect(".o-einvoice-attention-section:eq(1) .o-einvoice-attention-item").toHaveCount(2);
    });

    test("one section only", async () => {
        onRpc(ROUTE, () => ({ total: 2, sections: [SENDS] }));
        await mountWithCleanup(EinvoiceAttentionMenu);
        await animationFrame();
        expect(".o-einvoice-attention-toggle .badge").toHaveText("2");
        await contains(".o-einvoice-attention-toggle").click();
        expect(".o-einvoice-attention-section").toHaveCount(1);
        expect(".o-einvoice-attention-heading").toHaveText("Failed sends (2)");
    });

    test("an item opens its record and Show all opens the section's list", async () => {
        const actions = [];
        mockService("action", {
            doAction(action) {
                actions.push(action);
            },
        });
        onRpc(ROUTE, () => ({ total: 2, sections: [SENDS] }));
        await mountWithCleanup(EinvoiceAttentionMenu);
        await animationFrame();
        await contains(".o-einvoice-attention-toggle").click();
        await contains(".o-einvoice-attention-item:eq(1)").click();
        expect(actions[0]).toEqual({ ...SENDS.item_action, res_id: 2 });
        await contains(".o-einvoice-attention-toggle").click();
        await contains(".o-einvoice-attention-show-all").click();
        expect(actions[1]).toEqual(SENDS.action);
    });

    test("error: a failed route call keeps the last value", async () => {
        let fail = false;
        onRpc(ROUTE, () => {
            if (fail) {
                throw new Error("route failed");
            }
            return { total: 2, sections: [SENDS] };
        });
        await mountWithCleanup(EinvoiceAttentionMenu);
        await animationFrame();
        fail = true;
        await contains(".o-einvoice-attention-toggle").click();
        await animationFrame();
        expect(".o-einvoice-attention-toggle .badge").toHaveText("2");
        expect(".o-einvoice-attention-section").toHaveCount(1);
    });
});
