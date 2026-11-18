/**
 * Systray menu listing the approval queues the current user can act on.
 *
 * Always registered and hidden by the template's `t-if` until data arrives: the registry's
 * `isDisplayed` is synchronous and cannot wait for a group check. The server sends no data
 * for users holding neither level, and decides which rows, labels and actions to show.
 */

import { Component, onWillStart, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class BillApprovalMenu extends Component {
    static components = { Dropdown };
    static props = [];
    static template = "goodahead_bill_approval.BillApprovalMenu";

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.dropdown = useDropdownState();
        // Stays null until the server answers, and for users holding neither level.
        this.state = useState({ data: null });
        onWillStart(() => {
            // Do not await: the counts are not needed to draw the navbar, so the request
            // must not hold up its render.
            // `silent` keeps the loading indicator out of it. The catch handles the rejection,
            // so a failing count never raises an error dialog on every page load; the menu
            // then simply stays hidden.
            this.orm
                .silent.call("account.move", "bill_approval_systray_data", [])
                .then((data) => {
                    this.state.data = data && data.rows ? data : null;
                })
                .catch(() => {
                    this.state.data = null;
                });
        });
    }

    get rows() {
        return this.state.data ? this.state.data.rows : [];
    }

    get total() {
        return this.rows.reduce((sum, row) => sum + row.count, 0);
    }

    /**
     * Open one queue's list. Each row has its own action, so no context merging is needed
     * (`additionalContext` is merged under an action's context and cannot cancel its defaults).
     */
    openQueue(row) {
        this.dropdown.close();
        return this.action.doAction(row.action_xml_id);
    }

    get title() {
        return _t("Bill and credit note approvals");
    }

    get emptyLabel() {
        return _t("Nothing is waiting for you.");
    }
}

export const systrayBillApproval = {
    Component: BillApprovalMenu,
};

// Next to the activity and messaging menus; a unique sequence keeps the order stable.
registry
    .category("systray")
    .add("goodahead_bill_approval.bill_approval_menu", systrayBillApproval, {
        sequence: 98,
    });
