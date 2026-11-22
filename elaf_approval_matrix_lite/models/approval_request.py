import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

REQUEST_STATES = [
    ('draft', 'Draft'),
    ('pending', 'Pending'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
    ('cancelled', 'Cancelled'),
    ('invalidated', 'Invalidated'),
]
ACTIVE_STATES = ('pending', 'approved')

# Business fields that only the application may change (through sudo after
# explicit checks). Any attempt by a user, including via RPC, is refused.
_PROTECTED_FIELDS = {
    'name', 'res_model', 'res_id', 'document_name', 'company_id', 'requester_id', 'rule_id',
    'partner_id', 'currency_id', 'amount', 'amount_company', 'baseline_amount_company', 'fingerprint', 'snapshot_json',
    'state', 'level_ids', 'current_approver_ids', 'all_approver_ids', 'reject_reason',
    'invalidation_reason', 'date_requested', 'date_decided', 'cycle_hours', 'current_level_name',
}


class ElafApprovalRequest(models.Model):
    _name = 'elaf.approval.request'
    _description = 'Approval Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _mail_post_access = 'read'

    name = fields.Char(readonly=True, default='/', copy=False)
    res_model = fields.Char('Document Model', required=True, readonly=True, index=True)
    res_id = fields.Many2oneReference('Document ID', model_field='res_model', required=True,
                                      readonly=True, index=True)
    document_type = fields.Char(compute='_compute_document_type')
    document_name = fields.Char('Document', readonly=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    requester_id = fields.Many2one('res.users', 'Requested by', required=True, readonly=True, index=True)
    rule_id = fields.Many2one('elaf.approval.rule', readonly=True, ondelete='restrict', index=True)
    partner_id = fields.Many2one('res.partner', 'Partner', readonly=True, index=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    amount = fields.Monetary(readonly=True, currency_field='currency_id',
                             help="Document total when approval was requested.")
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Company Currency')
    amount_company = fields.Monetary('Amount (Company Currency)', readonly=True,
                                     currency_field='company_currency_id')
    baseline_amount_company = fields.Monetary(
        'Submitted Amount (Company Currency)', readonly=True, currency_field='company_currency_id',
        help="Amount when approval was requested. The rule tolerance is measured against it.")
    fingerprint = fields.Char(readonly=True, copy=False,
                              help="Hash of the approved document content (partner, currency, lines).")
    snapshot_json = fields.Text(readonly=True, copy=False)
    state = fields.Selection(REQUEST_STATES, default='draft', required=True, readonly=True,
                             tracking=True, index=True)
    level_ids = fields.One2many('elaf.approval.request.level', 'request_id', 'Levels', readonly=True)
    current_level_name = fields.Char('Current Level', readonly=True)
    current_approver_ids = fields.Many2many(
        'res.users', 'elaf_approval_request_current_user_rel', 'request_id', 'user_id',
        string='Waiting For', readonly=True)
    all_approver_ids = fields.Many2many(
        'res.users', 'elaf_approval_request_all_user_rel', 'request_id', 'user_id',
        string='All Approvers', readonly=True)
    reject_reason = fields.Text(readonly=True)
    invalidation_reason = fields.Text(readonly=True)
    date_requested = fields.Datetime(readonly=True, index=True)
    date_decided = fields.Datetime(readonly=True, index=True)
    cycle_hours = fields.Float('Cycle Time (h)', readonly=True, aggregator='avg', digits=(16, 1))
    log_ids = fields.One2many('elaf.approval.log', 'request_id', 'Audit Log', readonly=True)
    can_act = fields.Boolean(compute='_compute_can_act')
    is_requester = fields.Boolean(compute='_compute_can_act')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('res_model')
    def _compute_document_type(self):
        for req in self:
            model = self.env['ir.model']._get(req.res_model) if req.res_model else False
            req.document_type = model.name if model else req.res_model

    @api.depends_context('uid')
    @api.depends('state', 'level_ids.state', 'level_ids.approved_user_ids')
    def _compute_can_act(self):
        user = self.env.user
        for req in self:
            req.can_act = bool(req.state == 'pending' and req._elaf_find_actionable_level(user)[0])
            req.is_requester = req.requester_id == user

    # ------------------------------------------------------------------
    # ORM guards
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(self.env._(
                "Approval requests are created with the 'Request Approval' button on the document."))
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('elaf.approval.request') or '/'
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and _PROTECTED_FIELDS.intersection(vals):
            raise AccessError(self.env._(
                "Approval requests can only change through Approve, Reject, Cancel or a document "
                "change. Direct edits are not allowed."))
        return super().write(vals)

    def unlink(self):
        raise UserError(self.env._(
            "Approval requests are part of the audit trail and cannot be deleted. Cancel them instead."))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _elaf_document(self):
        self.ensure_one()
        if self.res_model not in self.env:
            return None
        return self.env[self.res_model].sudo().browse(self.res_id).exists()

    def _elaf_active_levels(self):
        self.ensure_one()
        return self.level_ids.filtered(lambda lv: lv.state == 'active')

    def _elaf_level_actors(self, level):
        """Users allowed to act on a level, as a dict {acting_user: on_behalf_user}.
        Lite: the eligible approvers themselves. Pro adds delegates."""
        pending = level.approver_ids - level.approved_user_ids
        return {user: self.env['res.users'] for user in pending}

    def _elaf_find_actionable_level(self, user):
        """Return (level, on_behalf_user) where `user` may act now, or (False, False)."""
        self.ensure_one()
        if self.state != 'pending' or user == self.requester_id:
            return False, False
        for level in self._elaf_active_levels().sorted('sequence'):
            actors = self._elaf_level_actors(level)
            if user in actors:
                return level, actors[user]
        return False, False

    @api.model
    def _elaf_filter_eligible(self, level, users, document, requester):
        """Return (eligible_users, [(user, reason), ...] conflicts) for a level.
        Lite: everybody configured except the requester (already removed)."""
        return users, []

    def _elaf_after_submit(self, document, conflicts):
        """Hook after a request is created (Pro: SoD conflicts, split detection)."""
        return True

    def _elaf_check_actor(self, level, user, on_behalf):
        """Hook for additional checks before an approval (Pro: SoD rules)."""
        return True

    def _elaf_log(self, action, level=False, comment=False, on_behalf=False):
        # sudo: the audit log has no create ACL; entries are only written here.
        self.env['elaf.approval.log'].sudo().create([{
            'request_id': req.id,
            'action': action,
            'level_name': level.name if level else False,
            'level_sequence': level.sequence if level else 0,
            'user_id': self.env.user.id,
            'on_behalf_of_id': on_behalf.id if on_behalf else False,
            'comment': comment or False,
            'amount': req.amount,
            'currency_id': req.currency_id.id,
            'fingerprint': req.fingerprint,
            'res_model': req.res_model,
            'res_id': req.res_id,
            'company_id': req.company_id.id,
        } for req in self])

    def _elaf_post_on_document(self, body):
        for req in self:
            doc = req._elaf_document()
            if doc and hasattr(doc, 'message_post'):
                # sudo: approvers may lack write access on the document; the
                # message is authored by the acting user.
                doc.sudo().message_post(body=body, author_id=self.env.user.partner_id.id,
                                        subtype_xmlid='mail.mt_note')

    # ------------------------------------------------------------------
    # State machine (all methods below run on sudo() records after checks)
    # ------------------------------------------------------------------
    def _elaf_levels_to_activate(self):
        """Lite: strictly sequential, the next waiting level."""
        self.ensure_one()
        waiting = self.level_ids.filtered(lambda lv: lv.state == 'waiting').sorted('sequence')
        return waiting[:1]

    def _elaf_activate_next(self):
        for req in self:
            if req._elaf_active_levels():
                continue
            to_activate = req._elaf_levels_to_activate()
            if not to_activate:
                req._elaf_finish_approved()
                continue
            to_activate.write({'state': 'active', 'date_activated': fields.Datetime.now()})
            to_activate._elaf_on_activate()
        self._elaf_refresh_current()

    def _elaf_finish_approved(self):
        now = fields.Datetime.now()
        for req in self:
            req.write({
                'state': 'approved',
                'date_decided': now,
                'cycle_hours': (now - req.date_requested).total_seconds() / 3600.0 if req.date_requested else 0.0,
            })
            req._elaf_log('approved')
            req._elaf_post_on_document(self.env._("Approval %(req)s completed: the document is approved.", req=req.name))
            req.message_post(body=self.env._("All levels approved."), partner_ids=req.requester_id.partner_id.ids,
                             subtype_xmlid='mail.mt_comment')

    def _elaf_refresh_current(self):
        for req in self:
            users = self.env['res.users']
            names = []
            if req.state == 'pending':
                for level in req._elaf_active_levels().sorted('sequence'):
                    for actor in req._elaf_level_actors(level):
                        users |= actor
                    names.append(level.name)
            req.write({
                'current_approver_ids': [fields.Command.set(users.ids)],
                'current_level_name': ', '.join(names) or False,
            })
            req._elaf_sync_activities()

    def _elaf_sync_activities(self):
        act_type = self.env.ref('elaf_approval_matrix_lite.mail_activity_type_approval', raise_if_not_found=False)
        if not act_type:
            return
        for req in self:
            activities = req.activity_ids.filtered(lambda a: a.activity_type_id == act_type)
            wanted = req.current_approver_ids if req.state == 'pending' else self.env['res.users']
            stale = activities.filtered(lambda a: a.user_id not in wanted)
            have = (activities - stale).user_id
            stale.unlink()
            for user in wanted - have:
                req.activity_schedule(
                    act_type_xmlid='elaf_approval_matrix_lite.mail_activity_type_approval',
                    user_id=user.id,
                    summary=self.env._("Approve %(doc)s (%(req)s)", doc=req.document_name, req=req.name),
                    note=self.env._("Level: %(level)s. Amount: %(amount)s %(cur)s.",
                                    level=req.current_level_name, amount=req.amount,
                                    cur=req.currency_id.name))

    def _elaf_close(self, state, **vals):
        now = fields.Datetime.now()
        for req in self:
            open_levels = req.level_ids.filtered(lambda lv: lv.state in ('waiting', 'active'))
            open_levels.write({'state': 'cancelled'})
            values = dict(vals, state=state, date_decided=now)
            if req.date_requested:
                values['cycle_hours'] = (now - req.date_requested).total_seconds() / 3600.0
            req.write(values)
        self._elaf_refresh_current()

    def _elaf_invalidate(self, reason):
        """Called by documents on a material change (pending or approved requests)."""
        reqs = self.filtered(lambda r: r.state in ACTIVE_STATES).sudo()
        for req in reqs:
            req._elaf_close('invalidated', invalidation_reason=reason)
            req._elaf_log('invalidated', comment=reason)
            msg = self.env._("Approval %(req)s invalidated: %(reason)s. A new approval is required.",
                             req=req.name, reason=reason)
            req._elaf_post_on_document(msg)
            req.message_post(body=msg, partner_ids=req.requester_id.partner_id.ids,
                             subtype_xmlid='mail.mt_comment')
        return reqs

    def _elaf_adjust_snapshot(self, fingerprint, snapshot, amount, amount_company, comment):
        """Change within tolerance: keep the approval, record the new content."""
        for req in self.sudo():
            req.write({'fingerprint': fingerprint, 'snapshot_json': json.dumps(snapshot),
                       'amount': amount, 'amount_company': amount_company})
            req._elaf_log('adjusted', comment=comment)

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------
    def _elaf_do_approve(self, comment=False):
        self.ensure_one()
        user = self.env.user
        if self.state != 'pending':
            raise UserError(self.env._("Only pending requests can be approved (current state: %s).",
                                       dict(REQUEST_STATES)[self.state]))
        if user == self.requester_id:
            raise AccessError(self.env._("You cannot approve your own approval request."))
        level, on_behalf = self._elaf_find_actionable_level(user)
        if not level:
            raise AccessError(self.env._(
                "You are not an approver of the current level of %(req)s.", req=self.name))
        self._elaf_check_actor(level, user, on_behalf)
        # sudo: permission verified above (current-level approver, not requester).
        req = self.sudo()
        level = level.sudo()
        slot_user = on_behalf or user
        level.write({'approved_user_ids': [fields.Command.link(slot_user.id)],
                     'acted_user_ids': [fields.Command.link(user.id)]})
        req._elaf_log('approve', level=level, comment=comment, on_behalf=on_behalf)
        if len(level.approved_user_ids) >= level.required_approvals:
            level.write({'state': 'approved', 'date_done': fields.Datetime.now()})
            req._elaf_post_on_document(self.env._(
                "Level %(level)s approved by %(user)s.", level=level.name, user=user.name))
        req._elaf_activate_next()
        return True

    def _elaf_do_reject(self, reason):
        self.ensure_one()
        reason = (reason or '').strip()
        if not reason:
            raise UserError(self.env._("A reason is required to reject an approval request."))
        user = self.env.user
        if self.state != 'pending':
            raise UserError(self.env._("Only pending requests can be rejected."))
        if user == self.requester_id:
            raise AccessError(self.env._("You cannot reject your own approval request; cancel it instead."))
        level, on_behalf = self._elaf_find_actionable_level(user)
        if not level:
            raise AccessError(self.env._(
                "You are not an approver of the current level of %(req)s.", req=self.name))
        self._elaf_check_actor(level, user, on_behalf)
        # sudo: permission verified above.
        req = self.sudo()
        level.sudo().write({'state': 'rejected', 'date_done': fields.Datetime.now(),
                            'acted_user_ids': [fields.Command.link(user.id)]})
        req._elaf_close('rejected', reject_reason=reason)
        req._elaf_log('reject', level=level, comment=reason, on_behalf=on_behalf)
        msg = self.env._("Approval %(req)s rejected by %(user)s: %(reason)s",
                         req=req.name, user=user.name, reason=reason)
        req._elaf_post_on_document(msg)
        req.message_post(body=msg, partner_ids=req.requester_id.partner_id.ids,
                         subtype_xmlid='mail.mt_comment')
        return True

    def action_approve(self):
        return self._elaf_open_decision('approve')

    def action_reject(self):
        return self._elaf_open_decision('reject')

    def _elaf_open_decision(self, decision):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Approve") if decision == 'approve' else self.env._("Reject"),
            'res_model': 'elaf.approval.decision.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_request_id': self.id, 'default_decision': decision},
        }

    def action_cancel(self):
        for req in self:
            if req.state not in ACTIVE_STATES:
                raise UserError(self.env._("Only pending or approved requests can be cancelled."))
            if req.requester_id != self.env.user and not self.env.user.has_group(
                    'elaf_approval_matrix_lite.group_approval_manager'):
                raise AccessError(self.env._("Only the requester or an approval manager can cancel a request."))
            # sudo: permission verified above.
            req.sudo()._elaf_close('cancelled')
            req.sudo()._elaf_log('cancel')
            req.sudo()._elaf_post_on_document(self.env._("Approval %s cancelled.", req.name))
        return True

    def action_open_document(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }


class ElafApprovalRequestLevel(models.Model):
    _name = 'elaf.approval.request.level'
    _description = 'Approval Request Level'
    _order = 'request_id, sequence, id'

    request_id = fields.Many2one('elaf.approval.request', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='request_id.company_id', store=True)
    level_id = fields.Many2one('elaf.approval.rule.level', ondelete='set null')
    sequence = fields.Integer()
    name = fields.Char()
    approver_ids = fields.Many2many('res.users', 'elaf_approval_req_level_user_rel', 'level_id', 'user_id',
                                    string='Eligible Approvers')
    approved_user_ids = fields.Many2many('res.users', 'elaf_approval_req_level_done_rel', 'level_id', 'user_id',
                                         string='Approved By')
    acted_user_ids = fields.Many2many('res.users', 'elaf_approval_req_level_acted_rel', 'level_id', 'user_id',
                                      string='Acted By')
    required_approvals = fields.Integer(default=1)
    approval_progress = fields.Char(compute='_compute_progress')
    state = fields.Selection([
        ('waiting', 'Waiting'), ('active', 'In Progress'), ('approved', 'Approved'),
        ('rejected', 'Rejected'), ('cancelled', 'Cancelled')], default='waiting', required=True)
    date_activated = fields.Datetime()
    date_done = fields.Datetime()

    @api.depends('approved_user_ids', 'required_approvals')
    def _compute_progress(self):
        for level in self:
            level.approval_progress = '%s / %s' % (len(level.approved_user_ids), level.required_approvals)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(self.env._("Request levels are managed by the application."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(self.env._("Request levels are managed by the application."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(self.env._("Request levels are managed by the application."))
        return super().unlink()

    def _elaf_on_activate(self):
        """Hook called when levels become active (Pro: SLA deadlines)."""
        return True
