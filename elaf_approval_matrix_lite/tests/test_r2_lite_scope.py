"""Remediation round 2, requirements review (S3-01): Lite scope checks that had no dedicated test."""
from odoo.tests import tagged

from .common import ApprovalCommon


@tagged('post_install', '-at_install', 'elaf_approval', 'elaf_r2')
class TestApprovalLiteScope(ApprovalCommon):

    def test_lite_purchase_orders_only(self):
        """S3-01: Lite governs Purchase Orders only; other document types are a Pro feature."""
        self._skip_if_pro()
        selection = self.env['elaf.approval.rule']._fields['res_model'].selection
        self.assertEqual([key for key, _label in selection], ['purchase.order'])
        for model in ('sale.order', 'account.move'):
            if model in self.env:
                self.assertFalse(hasattr(type(self.env[model]), 'action_elaf_request_approval'))

    def test_lite_has_no_pro_capabilities(self):
        """S3-01: no workflow builder, parallel or any-N levels, delegation, escalation, simulation
        or control tower in Lite (they exist only in the Pro module)."""
        self._skip_if_pro()
        Rule = type(self.env['elaf.approval.rule'])
        self.assertFalse(hasattr(Rule, 'elaf_builder_save'))
        level_fields = self.env['elaf.approval.rule.level']._fields
        for name in ('approval_mode', 'min_approvals', 'sla_hours', 'backup_user_id', 'auto_escalate'):
            self.assertNotIn(name, level_fields)
        self.assertNotIn('level_flow', self.env['elaf.approval.rule']._fields)
        for model in ('elaf.approval.delegation', 'elaf.approval.simulation', 'elaf.approval.sod.rule'):
            self.assertNotIn(model, self.env)
        self.assertFalse(hasattr(type(self.env['elaf.approval.request']), 'get_control_tower_data'))
        # The comparison page lists the builder as a locked Pro capability.
        data = self.env['elaf.approval.request'].with_user(self.manager).get_edition_comparison()
        builder = [f for f in data['features'] if 'workflow builder' in f['label']]
        self.assertEqual(len(builder), 1)
        self.assertEqual((builder[0]['lite'], builder[0]['pro']), (False, True))
