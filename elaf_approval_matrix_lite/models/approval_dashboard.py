from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError

from .approval_usage import (LITE_MAX_ACTIVE_RULES, LITE_MAX_LEVELS, LITE_MAX_REQUESTS, PRO_URL)

PERIODS = (7, 30, 90, 365)


class ElafApprovalRequestDashboard(models.Model):
    _inherit = 'elaf.approval.request'

    @api.model
    def _elaf_check_dashboard_access(self):
        if not self.env.user.has_group('elaf_approval_matrix_lite.group_approval_user'):
            raise AccessError(self.env._("You need the Approvals / User access right."))

    @api.model
    def _elaf_sum_company_amount(self, requests):
        """Sum amount_company converted into the current company currency."""
        target = self.env.company.currency_id
        total = 0.0
        today = fields.Date.context_today(self)
        for req in requests:
            src = req.company_currency_id
            amount = req.amount_company
            if src and src != target:
                amount = src._convert(amount, target, self.env.company, today)
            total += amount
        return total

    @api.model
    def _elaf_fmt_dt(self, value):
        """Datetime as text in the user's timezone (dashboards receive plain JSON)."""
        if not value:
            return ''
        return fields.Datetime.context_timestamp(self, value).strftime('%Y-%m-%d %H:%M')

    @api.model
    def _elaf_row(self, req):
        return {
            'id': req.id,
            'name': req.name,
            'document': req.document_name or '',
            'document_type': req.document_type or '',
            'requester': req.requester_id.name or '',
            'level': req.current_level_name or '',
            'amount': req.currency_id.format(req.amount) if req.currency_id else '',
            'state': req.state,
            'state_label': dict(self.fields_get(['state'])['state']['selection'])[req.state],
            'date': self._elaf_fmt_dt(req.date_requested),
        }

    @api.model
    def get_dashboard_data(self, filters=None):
        self._elaf_check_dashboard_access()
        filters = filters or {}
        period = int(filters.get('period') or 30)
        if period not in PERIODS:
            period = 30
        uid = self.env.uid
        since = fields.Datetime.now() - timedelta(days=period)
        since_s = fields.Datetime.to_string(since)
        company_dom = [('company_id', 'in', self.env.companies.ids)]
        currency = self.env.company.currency_id

        dom_me = company_dom + [('state', '=', 'pending'), ('current_approver_ids', 'in', [uid])]
        dom_pending = company_dom + [('state', '=', 'pending')]
        dom_approved = company_dom + [('state', '=', 'approved'), ('date_decided', '>=', since_s)]
        dom_rejected = company_dom + [('state', '=', 'rejected'), ('date_decided', '>=', since_s)]
        dom_invalid = company_dom + [('state', '=', 'invalidated'), ('date_decided', '>=', since_s)]
        dom_mine = company_dom + [('requester_id', '=', uid), ('state', '=', 'pending')]

        pending = self.search(dom_pending)
        approved = self.search(dom_approved)
        rejected = self.search(dom_rejected)
        invalidated_count = self.search_count(dom_invalid)
        me = self.search(dom_me, order='date_requested asc')
        cycle = approved.filtered(lambda r: r.date_requested and r.date_decided)
        avg_hours = round(sum(cycle.mapped('cycle_hours')) / len(cycle), 1) if cycle else None
        decided = len(approved) + len(rejected)

        kpis = [
            {'key': 'pending_me', 'label': self.env._("Waiting for Me"), 'value': len(me),
             'display': str(len(me)), 'hint': self.env._("Requests you can approve now"),
             'tone': 'warn' if me else 'ok', 'domain': dom_me},
            {'key': 'pending', 'label': self.env._("Pending"), 'value': len(pending),
             'display': str(len(pending)),
             'hint': self.env._("Total %s", currency.format(self._elaf_sum_company_amount(pending))),
             'tone': '', 'domain': dom_pending},
            {'key': 'approved', 'label': self.env._("Approved (%s days)", period), 'value': len(approved),
             'display': str(len(approved)),
             'hint': self.env._("Total %s", currency.format(self._elaf_sum_company_amount(approved))),
             'tone': 'ok', 'domain': dom_approved},
            {'key': 'rejected', 'label': self.env._("Rejected (%s days)", period), 'value': len(rejected),
             'display': str(len(rejected)),
             'hint': (self.env._("%s%% of decisions", round(len(rejected) * 100.0 / decided))
                      if decided else self.env._("No decisions in period")),
             'tone': 'bad' if rejected else '', 'domain': dom_rejected},
            {'key': 'cycle', 'label': self.env._("Avg. Approval Time"), 'value': avg_hours,
             'display': (self.env._("%s h", avg_hours) if avg_hours is not None else '—'),
             'hint': (self.env._("Request to final approval, %s approved", len(cycle)) if cycle
                      else self.env._("No approved requests in period")),
             'tone': '', 'domain': dom_approved},
            {'key': 'invalidated', 'label': self.env._("Invalidated (%s days)", period),
             'value': invalidated_count, 'display': str(invalidated_count),
             'hint': self.env._("Approvals voided by a material change"),
             'tone': 'warn' if invalidated_count else '', 'domain': dom_invalid},
        ]

        # sudo: the rule count is shown to every approval user; no rule data is exposed.
        rule_count = self.env['elaf.approval.rule'].sudo().search_count(
            [('company_id', 'in', self.env.companies.ids)])
        logs = self.env['elaf.approval.log'].search(company_dom, limit=10)
        action_labels = dict(logs.fields_get(['action'])['action']['selection'])
        recent = [{
            'id': log.id, 'request_id': log.request_id.id, 'request': log.request_id.name,
            'document': log.request_id.document_name or '', 'action': log.action,
            'action_label': action_labels[log.action],
            'actor': log.actor_display, 'date': self._elaf_fmt_dt(log.date),
            'comment': log.comment or '',
        } for log in logs]

        usage = self.env['elaf.approval.edition']._elaf_usage_row()._usage_summary()
        return {
            'period': period,
            'periods': list(PERIODS),
            'currency': currency.name,
            'kpis': kpis,
            'my_queue': [self._elaf_row(r) for r in me[:8]],
            'my_pending_count': self.search_count(dom_mine),
            'my_pending_domain': dom_mine,
            'recent': recent,
            'rule_count': rule_count,
            'is_manager': self.env.user.has_group('elaf_approval_matrix_lite.group_approval_manager'),
            'usage': usage,
            'locked_features': [] if usage['is_pro'] else self._elaf_pro_feature_labels(),
        }

    @api.model
    def _elaf_pro_feature_labels(self):
        return [
            self.env._("Sales Orders, Vendor Bills and Customer Invoices"),
            self.env._("Parallel and any-N levels"),
            self.env._("Delegation of authority"),
            self.env._("Segregation of duties rules"),
            self.env._("Policy simulation"),
            self.env._("Split-transaction detection"),
            self.env._("SLA escalation"),
            self.env._("Governance Control Tower"),
            self.env._("Visual drag-and-drop workflow builder"),
        ]

    @api.model
    def get_edition_comparison(self):
        self._elaf_check_dashboard_access()
        usage = self.env['elaf.approval.edition']._elaf_usage_row()._usage_summary()
        company_dom = [('company_id', 'in', self.env.companies.ids)]
        currency = self.env.company.currency_id
        po_reqs = self.search(company_dom + [('res_model', '=', 'purchase.order')])
        approved = po_reqs.filtered(lambda r: r.state == 'approved')
        rejected = po_reqs.filtered(lambda r: r.state == 'rejected')
        invalidated = po_reqs.filtered(lambda r: r.state == 'invalidated')
        _ = self.env._
        yes, no = True, False
        features = [
            (_("Purchase Order approvals"), yes, yes),
            (_("Server-side enforcement and change invalidation"), yes, yes),
            (_("Immutable audit log"), yes, yes),
            (_("Approval requests"), _("%s in total", LITE_MAX_REQUESTS), _("Unlimited")),
            (_("Active approval rules"), str(LITE_MAX_ACTIVE_RULES), _("Unlimited")),
            (_("Levels per rule"), str(LITE_MAX_LEVELS), _("Unlimited")),
            (_("Companies"), _("One"), _("Multi-company")),
            (_("Sales Orders, Vendor Bills and Customer Invoices"), no, yes),
            (_("Conditions (partner, category, analytic, team, domain)"), no, yes),
            (_("Parallel and any-N levels"), no, yes),
            (_("Delegation of authority"), no, yes),
            (_("Segregation of duties rules"), no, yes),
            (_("Policy simulation"), no, yes),
            (_("Split-transaction detection"), no, yes),
            (_("SLA escalation"), no, yes),
            (_("Governance Control Tower"), no, yes),
            (_("Visual drag-and-drop workflow builder"), no, yes),
        ]
        return {
            'usage': usage,
            'pro_url': PRO_URL,
            'support_email': 'info@elaf-development.com',
            'features': [{'label': f[0], 'lite': f[1], 'pro': f[2]} for f in features],
            'limits': [
                _("One company."),
                _("Purchase Orders only."),
                _("One active approval rule."),
                _("Up to %s levels per rule, one approver needed per level (sequential).", LITE_MAX_LEVELS),
                _("Up to %s approval requests in total (resubmitting after invalidation counts as a new request).",
                  LITE_MAX_REQUESTS),
            ],
            'value': {
                'pos_controlled': len(set(po_reqs.mapped('res_id'))),
                'amount_approved': currency.format(self._elaf_sum_company_amount(approved)),
                'rejections': len(rejected),
                'amount_rejected': currency.format(self._elaf_sum_company_amount(rejected)),
                'invalidations': len(invalidated),
            },
        }
