/** @odoo-module */

import { Component, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";

export class FailedImportsMenu extends Component {
    static components = { Dropdown };
    static props = [];
    static template = "goodahead_invoice_ai_import.FailedImportsMenu";

    setup() {
        this.action = useService("action");
        this.dropdown = useDropdownState();
        this.state = useState({ count: 0, imports: [], isVisible: false });
        this.checkVisibility();
    }

    async checkVisibility() {
        // Must match `ai.invoice.import._ai_user_works_imports` and the menus:
        // Bookkeeper or accounting Administrator, not plain Invoicing users.
        const [isBookkeeper, isAdministrator] = await Promise.all([
            user.hasGroup("account.group_account_user"),
            user.hasGroup("account.group_account_manager"),
        ]);
        if (isBookkeeper || isAdministrator) {
            this.state.isVisible = true;
            this.fetchFailedImports();
        }
    }

    async fetchFailedImports() {
        try {
            const data = await rpc("/goodahead_invoice_ai_import/failed_imports");
            this.state.count = data.count;
            this.state.imports = data.imports;
        } catch {
            this.state.count = 0;
            this.state.imports = [];
        }
    }

    openAllFailedImports() {
        this.dropdown.close();
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Failed AI Imports",
            res_model: "ai.invoice.import",
            views: [
                [false, "list"],
                [false, "form"],
            ],
            domain: [["state", "=", "failed"]],
        });
    }

    openImport(id) {
        this.dropdown.close();
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ai.invoice.import",
            res_id: id,
            views: [[false, "form"]],
        });
    }
}

registry
    .category("systray")
    .add("goodahead_invoice_ai_import.failed_imports_menu", {
        Component: FailedImportsMenu,
    }, { sequence: 90 });
