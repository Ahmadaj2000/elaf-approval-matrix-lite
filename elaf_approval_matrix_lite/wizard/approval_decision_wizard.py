from odoo import fields, models


class ElafApprovalDecisionWizard(models.TransientModel):
    _name = 'elaf.approval.decision.wizard'
    _description = 'Approve or reject an approval request'

    request_id = fields.Many2one('elaf.approval.request', required=True, readonly=True)
    decision = fields.Selection([('approve', 'Approve'), ('reject', 'Reject')], required=True, readonly=True)
    document_name = fields.Char(related='request_id.document_name')
    amount = fields.Monetary(related='request_id.amount', currency_field='currency_id')
    currency_id = fields.Many2one(related='request_id.currency_id')
    level_name = fields.Char(related='request_id.current_level_name')
    comment = fields.Text(help="Optional for an approval, required for a rejection.")

    def action_confirm(self):
        self.ensure_one()
        # The request methods perform every permission check server-side.
        if self.decision == 'approve':
            self.request_id._elaf_do_approve(self.comment)
        else:
            self.request_id._elaf_do_reject(self.comment)
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}
