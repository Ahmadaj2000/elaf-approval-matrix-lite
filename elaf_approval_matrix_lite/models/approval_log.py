from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

LOG_ACTIONS = [
    ('submit', 'Submitted'),
    ('approve', 'Approved Level'),
    ('approved', 'Request Approved'),
    ('reject', 'Rejected'),
    ('cancel', 'Cancelled'),
    ('invalidated', 'Invalidated'),
    ('adjusted', 'Change Within Tolerance'),
]


class ElafApprovalLog(models.Model):
    """Immutable audit trail. Rows are only created by the approval business
    methods (under sudo, after their own checks). Nobody can edit or delete them
    through the ORM."""
    _name = 'elaf.approval.log'
    _description = 'Approval Audit Log'
    _order = 'id desc'
    _rec_name = 'action'

    request_id = fields.Many2one('elaf.approval.request', required=True, readonly=True,
                                 ondelete='restrict', index=True)
    date = fields.Datetime(default=fields.Datetime.now, readonly=True, required=True)
    user_id = fields.Many2one('res.users', 'User', readonly=True, required=True)
    on_behalf_of_id = fields.Many2one('res.users', 'On Behalf Of', readonly=True)
    action = fields.Selection(LOG_ACTIONS, required=True, readonly=True)
    level_name = fields.Char('Level', readonly=True)
    level_sequence = fields.Integer(readonly=True)
    comment = fields.Text(readonly=True)
    amount = fields.Monetary(readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', readonly=True)
    fingerprint = fields.Char(readonly=True)
    res_model = fields.Char('Document Model', readonly=True)
    res_id = fields.Many2oneReference('Document ID', model_field='res_model', readonly=True)
    document_name = fields.Char(related='request_id.document_name')
    company_id = fields.Many2one('res.company', readonly=True, index=True)
    actor_display = fields.Char('Actor', compute='_compute_actor_display')

    @api.depends('user_id', 'on_behalf_of_id')
    def _compute_actor_display(self):
        for log in self:
            if log.on_behalf_of_id:
                log.actor_display = self.env._("%(user)s on behalf of %(other)s",
                                               user=log.user_id.name, other=log.on_behalf_of_id.name)
            else:
                log.actor_display = log.user_id.name

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(self.env._("Audit log entries are written by the application only."))
        return super().create(vals_list)

    def write(self, vals):
        raise UserError(self.env._("The approval audit log is immutable."))

    def unlink(self):
        raise UserError(self.env._("The approval audit log is immutable and cannot be deleted."))
