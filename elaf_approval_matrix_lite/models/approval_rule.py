from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .approval_usage import LITE_DOCUMENT_MODELS, LITE_MAX_ACTIVE_RULES, LITE_MAX_LEVELS


class ElafApprovalRule(models.Model):
    _name = 'elaf.approval.rule'
    _description = 'Approval Rule'
    _inherit = ['mail.thread']
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True, tracking=True)
    sequence = fields.Integer(default=10, tracking=True,
                              help="When several rules match a document, the lowest sequence wins.")
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company,
                                 tracking=True)
    res_model = fields.Selection(
        selection=[('purchase.order', 'Purchase Order')], string='Document Type',
        required=True, default='purchase.order', tracking=True)
    currency_id = fields.Many2one(related='company_id.currency_id', string='Currency', readonly=True)
    min_amount = fields.Monetary('Minimum Amount', currency_field='currency_id', tracking=True,
                                 help="Documents whose total (in company currency) is at least this amount.")
    max_amount = fields.Monetary('Maximum Amount', currency_field='currency_id', tracking=True,
                                 help="Upper bound of the band (inclusive). 0 means no maximum.")
    amount_tolerance_pct = fields.Float(
        'Amount Tolerance (%)', default=0.0, tracking=True,
        help="After approval, quantity or price edits that keep the total within this percentage "
             "of the approved total keep the approval valid. 0 = any line change requires a new "
             "approval. Vendor, currency or product changes always invalidate.")
    level_ids = fields.One2many('elaf.approval.rule.level', 'rule_id', string='Levels', copy=True)
    level_count = fields.Integer(compute='_compute_level_count')
    request_count = fields.Integer(compute='_compute_request_count')
    note = fields.Text('Description', translate=True)

    @api.depends('level_ids')
    def _compute_level_count(self):
        for rule in self:
            rule.level_count = len(rule.level_ids)

    def _compute_request_count(self):
        data = self.env['elaf.approval.request']._read_group(
            [('rule_id', 'in', self.ids)], ['rule_id'], ['__count'])
        counts = {rule.id: count for rule, count in data}
        for rule in self:
            rule.request_count = counts.get(rule.id, 0)

    @api.constrains('min_amount', 'max_amount')
    def _check_amounts(self):
        for rule in self:
            if rule.min_amount < 0 or rule.max_amount < 0:
                raise ValidationError(self.env._("Rule amounts cannot be negative."))
            if rule.max_amount and rule.max_amount < rule.min_amount:
                raise ValidationError(self.env._("The maximum amount must be greater than the minimum amount."))

    @api.constrains('amount_tolerance_pct')
    def _check_tolerance(self):
        for rule in self:
            if rule.amount_tolerance_pct < 0 or rule.amount_tolerance_pct > 100:
                raise ValidationError(self.env._("The tolerance must be between 0 and 100%."))

    # ------------------------------------------------------------------
    # Lite edition limits
    # ------------------------------------------------------------------
    @api.constrains('active', 'res_model', 'company_id', 'level_ids')
    def _check_lite_limits(self):
        edition = self.env['elaf.approval.edition']
        if edition._elaf_approval_is_pro():
            return
        for rule in self:
            if rule.res_model not in LITE_DOCUMENT_MODELS:
                edition._elaf_pro_only(self.env._("Approval rules for other document types"))
            if len(rule.level_ids) > LITE_MAX_LEVELS:
                raise ValidationError(self.env._(
                    "The Lite edition supports up to %(max)s levels per rule. "
                    "The Pro edition supports unlimited levels.", max=LITE_MAX_LEVELS))
        # sudo: count every active rule, whatever the current user can read.
        active_count = self.sudo().with_context(active_test=True).search_count([])
        if active_count > LITE_MAX_ACTIVE_RULES:
            raise ValidationError(self.env._(
                "The Lite edition supports one active approval rule. Archive the current rule "
                "first, or use the Pro edition for an unlimited approval matrix."))

    @api.model_create_multi
    def create(self, vals_list):
        edition = self.env['elaf.approval.edition']
        for vals in vals_list:
            company = self.env['res.company'].browse(vals.get('company_id') or self.env.company.id)
            edition._elaf_check_company_allowed(company, bind=True)
        return super().create(vals_list)

    def write(self, vals):
        if 'company_id' in vals:
            self.env['elaf.approval.edition']._elaf_check_company_allowed(
                self.env['res.company'].browse(vals['company_id']), bind=True)
        return super().write(vals)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def _amount_in_band(self, amount_company):
        self.ensure_one()
        cur = self.currency_id
        if cur.compare_amounts(amount_company, self.min_amount) < 0:
            return False
        if self.max_amount and cur.compare_amounts(amount_company, self.max_amount) > 0:
            return False
        return True

    def _match_facts(self, facts):
        """Return True if this rule applies to the given facts dict.
        Lite: document type, company and amount band. Pro adds conditions."""
        self.ensure_one()
        return (self.res_model == facts['res_model']
                and self.company_id.id == facts['company_id']
                and self._amount_in_band(facts['amount_company']))

    @api.model
    def _find_rule(self, facts):
        # sudo: rule matching is a system decision; the requester need not read
        # every rule. Result is only used for business checks.
        rules = self.sudo().search([
            ('res_model', '=', facts['res_model']),
            ('company_id', '=', facts['company_id']),
        ], order='sequence, id')
        for rule in rules:
            if rule._match_facts(facts):
                return rule
        return self.sudo().browse()

    def action_view_requests(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Approval Requests"),
            'res_model': 'elaf.approval.request',
            'view_mode': 'list,form',
            'domain': [('rule_id', '=', self.id)],
        }


class ElafApprovalRuleLevel(models.Model):
    _name = 'elaf.approval.rule.level'
    _description = 'Approval Rule Level'
    _order = 'rule_id, sequence, id'

    rule_id = fields.Many2one('elaf.approval.rule', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='rule_id.company_id', store=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, translate=True)
    approver_type = fields.Selection(
        [('users', 'Specific Users'), ('group', 'Security Group')],
        required=True, default='users')
    user_ids = fields.Many2many('res.users', 'elaf_approval_level_user_rel', 'level_id', 'user_id',
                                string='Approvers', domain=[('share', '=', False)])
    group_id = fields.Many2one('res.groups', string='Approver Group')

    @api.constrains('approver_type', 'user_ids', 'group_id')
    def _check_approvers(self):
        for level in self:
            if level.approver_type == 'users' and not level.user_ids:
                raise ValidationError(self.env._("Level '%s' needs at least one approver.", level.name))
            if level.approver_type == 'group' and not level.group_id:
                raise ValidationError(self.env._("Level '%s' needs an approver group.", level.name))

    @api.constrains('rule_id')
    def _check_lite_level_count(self):
        if self.env['elaf.approval.edition']._elaf_approval_is_pro():
            return
        for rule in self.rule_id:
            if len(rule.level_ids) > LITE_MAX_LEVELS:
                raise ValidationError(self.env._(
                    "The Lite edition supports up to %(max)s levels per rule. "
                    "The Pro edition supports unlimited levels.", max=LITE_MAX_LEVELS))

    def _required_approvals(self, eligible_count):
        """Approvals needed to complete this level. Lite: one approver per level."""
        self.ensure_one()
        return 1

    def _candidate_users(self, company):
        """Internal, active users configured for the level who can work in the
        document's company."""
        self.ensure_one()
        users = self.user_ids if self.approver_type == 'users' else self.group_id.users
        return users.filtered(lambda u: u.active and not u.share and company in u.company_ids)

    def _flow_description(self):
        self.ensure_one()
        if self.approver_type == 'group':
            return self.group_id.display_name or ''
        return ', '.join(self.user_ids.mapped('name'))
