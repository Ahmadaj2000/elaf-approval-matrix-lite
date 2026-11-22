import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class ElafApprovalDashboard extends Component {
    static template = "elaf_approval_matrix_lite.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, error: null, data: null, period: 30 });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.error = null;
        try {
            this.state.data = await this.orm.call("elaf.approval.request", "get_dashboard_data", [
                { period: this.state.period },
            ]);
        } catch (e) {
            this.state.error = (e && e.data && e.data.message) || (e && e.message) || String(e);
        } finally {
            this.state.loading = false;
        }
    }

    onPeriodChange(ev) {
        this.state.period = parseInt(ev.target.value, 10);
        this.load();
    }

    get usage() {
        return this.state.data && this.state.data.usage;
    }

    get usageBarStyle() {
        return `width: ${this.usage ? this.usage.pct : 0}%`;
    }

    openKpi(kpi) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: kpi.label,
            res_model: "elaf.approval.request",
            views: [
                [false, "list"],
                [false, "form"],
            ],
            domain: kpi.domain,
            target: "current",
        });
    }

    onKpiKey(ev, kpi) {
        if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            this.openKpi(kpi);
        }
    }

    openRequest(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "elaf.approval.request",
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openMyPending() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("My Pending Requests"),
            res_model: "elaf.approval.request",
            views: [
                [false, "list"],
                [false, "form"],
            ],
            domain: this.state.data.my_pending_domain,
        });
    }

    openRules() {
        this.action.doAction("elaf_approval_matrix_lite.elaf_approval_rule_action");
    }

    openPurchase() {
        this.action.doAction("purchase.purchase_rfq");
    }

    openComparison() {
        this.action.doAction("elaf_approval_matrix_lite.elaf_approval_comparison_action");
    }
}

registry.category("actions").add("elaf_approval_matrix_lite.dashboard", ElafApprovalDashboard);
