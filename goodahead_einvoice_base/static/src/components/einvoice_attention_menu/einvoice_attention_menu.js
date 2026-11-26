// Copyright 2026 Goodahead Oy
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import { Component, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * The shared e-invoicing attention bubble.
 *
 * One bubble with the sum of every section the server returns for the user
 * (failed imports, failed sends, connection trouble...) and one block per
 * section. Who sees which section is decided by the server (each section's
 * groups), so the component shows whatever `/goodahead_einvoice/attention`
 * answers and hides itself when the total is 0. A failed call keeps the last
 * value: the systray shows no error.
 */
export class EinvoiceAttentionMenu extends Component {
    static components = { Dropdown };
    static props = [];
    static template = "goodahead_einvoice_base.EinvoiceAttentionMenu";

    setup() {
        this.action = useService("action");
        this.dropdown = useDropdownState();
        this.state = useState({ total: 0, sections: [] });
        this.fetchAttention();
    }

    async fetchAttention() {
        let data;
        try {
            data = await rpc("/goodahead_einvoice/attention", {}, { silent: true });
        } catch {
            return;
        }
        this.state.total = data.total;
        this.state.sections = data.sections;
    }

    openSection(section) {
        this.dropdown.close();
        this.action.doAction(section.action);
    }

    openItem(section, item) {
        this.dropdown.close();
        this.action.doAction({ ...section.item_action, res_id: item.id });
    }
}

registry
    .category("systray")
    .add("goodahead_einvoice_base.einvoice_attention_menu", {
        Component: EinvoiceAttentionMenu,
    }, { sequence: 90 });
