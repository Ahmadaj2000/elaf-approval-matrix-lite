/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class ElafApprovalComparison extends Component {
    static template = "elaf_approval_matrix_lite.Comparison";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, error: null, data: null });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.error = null;
        try {
            this.state.data = await this.orm.call("elaf.approval.request", "get_edition_comparison", []);
        } catch (e) {
            this.state.error = (e && e.data && e.data.message) || (e && e.message) || String(e);
        } finally {
            this.state.loading = false;
        }
    }

    isBool(value) {
        return value === true || value === false;
    }

    openDashboard() {
        this.action.doAction("elaf_approval_matrix_lite.elaf_approval_dashboard_action");
    }
}

registry.category("actions").add("elaf_approval_matrix_lite.comparison", ElafApprovalComparison);
