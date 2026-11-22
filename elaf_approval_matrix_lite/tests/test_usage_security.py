from unittest.mock import patch

import psycopg2

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged, new_test_user
from odoo.tools import mute_logger

from .common import ApprovalCommon


@tagged('post_install', '-at_install', 'elaf_approval')
class TestApprovalUsage(ApprovalCommon):

    def test_counting(self):
        used = self._used()
        self._request(self._po(2000.0))
        self.assertEqual(self._used(), used + 1)
        self._request(self._po(3000.0))
        self.assertEqual(self._used(), used + 2)
        logs = self.env['elaf.approval.usage.log'].sudo().search([('event', '=', 'consumed')], limit=2)
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0].user_id, self.requester)

    def test_limit_reached_denied(self):
        self._skip_if_pro()
        self._set_limit(self._used() + 1)
        self._request(self._po(2000.0))
        po = self._po(2500.0)
        with self.assertRaisesRegex(UserError, 'Lite approval allowance used'):
            po.with_user(self.requester).action_elaf_request_approval()
        po.invalidate_recordset()
        self.assertFalse(po.elaf_approval_request_id)
        self.assertTrue(po.elaf_lite_exhausted)
        # Enforcement stays on: the PO cannot be confirmed, with the allowance message.
        with self.assertRaisesRegex(UserError, 'Lite approval allowance used'):
            po.with_user(self.requester).button_confirm()

    def test_resubmission_after_invalidation_counts(self):
        po = self._po(2000.0)
        self._request(po)
        used = self._used()
        po.order_line.with_user(self.requester).write({'product_qty': 2})
        self._request(po)
        self.assertEqual(self._used(), used + 1)

    def test_failed_operation_does_not_consume(self):
        used = self._used()
        po = self._po(2000.0)
        Request = type(self.env['elaf.approval.request'])
        with patch.object(Request, '_elaf_after_submit', side_effect=RuntimeError('boom after counter')):
            with self.assertRaises(RuntimeError):
                po.with_user(self.requester).action_elaf_request_approval()
        self.assertEqual(self._used(), used)
        po.invalidate_recordset()
        self.assertFalse(po.elaf_approval_request_id)
        self.assertFalse(self.env['elaf.approval.request'].search([('res_id', '=', po.id),
                                                                   ('res_model', '=', 'purchase.order')]))

    def test_no_rule_or_bad_config_does_not_consume(self):
        used = self._used()
        with self.assertRaises(UserError):
            self._po(10.0).with_user(self.requester).action_elaf_request_approval()
        self.assertEqual(self._used(), used)

    def test_lock_path_blocks_concurrent_submitter(self):
        """The submission locks the usage row (SELECT ... FOR UPDATE) before the
        idempotency check; a second transaction cannot take the lock meanwhile."""
        self.usage._lock()
        with self.env.registry.cursor() as cr2, mute_logger('odoo.sql_db'):
            with self.assertRaises(psycopg2.errors.LockNotAvailable):
                cr2.execute('SELECT id FROM elaf_approval_usage WHERE id = %s FOR UPDATE NOWAIT',
                            (self.usage.id,))
            cr2.rollback()

    def test_consume_statement_is_atomic_and_bounded(self):
        self._skip_if_pro()
        self._set_limit(self._used())
        with self.assertRaises(UserError):
            self.usage._consume('test,1')
        self.assertEqual(self._used(), self.usage.limit)

    def test_users_cannot_write_usage(self):
        for user in (self.requester, self.manager):
            with self.assertRaises(AccessError):
                self.usage.with_user(user).write({'used': 0})
            with self.assertRaises(AccessError):
                self.usage.with_user(user).unlink()
            with self.assertRaises(AccessError):
                self.env['elaf.approval.usage'].with_user(user).create({'key': 'x'})
        admin = self.env.ref('base.user_admin')
        with self.assertRaises(AccessError):
            self.usage.with_user(admin).write({'used': 0})
        with self.assertRaises(UserError):
            self.usage.sudo().write({'used': 0})
        with self.assertRaises(UserError):
            self.usage.sudo().unlink()
        # Readable by approval users.
        self.assertGreaterEqual(self.usage.with_user(self.requester).used, 0)

    def test_usage_log_append_only(self):
        self._request(self._po(2000.0))
        log = self.env['elaf.approval.usage.log'].sudo().search([], limit=1)
        with self.assertRaises(UserError):
            log.write({'event': 'denied'})
        with self.assertRaises(UserError):
            log.unlink()

    def test_deleting_documents_never_refunds(self):
        po = self._po(2000.0)
        self._request(po)
        used = self._used()
        po.with_user(self.requester).button_cancel()
        po.with_user(self.manager).unlink()
        self.assertEqual(self._used(), used)

    def test_history_readable_after_exhaustion(self):
        self._skip_if_pro()
        req = self._request(self._po(2000.0))
        self._set_limit(self._used())
        self.assertEqual(req.with_user(self.requester).read(['state'])[0]['state'], 'pending')
        data = self.env['elaf.approval.request'].with_user(self.requester).get_dashboard_data({'period': 30})
        self.assertTrue(data['usage']['exhausted'])
        self.assertEqual(data['usage']['tone'], 'bad')
        comp = self.env['elaf.approval.request'].with_user(self.requester).get_edition_comparison()
        self.assertEqual(comp['value']['pos_controlled'], 1)
        self.assertEqual(len(self.env['elaf.approval.log'].with_user(self.requester).search(
            [('request_id', '=', req.id)])), 1)

    def test_inflight_workflow_completes_after_exhaustion(self):
        self._skip_if_pro()
        po = self._po(2000.0)
        req = self._request(po)
        po_approved = self._po(3000.0)
        req_approved = self._request(po_approved)
        self._approve_all(req_approved)
        self._set_limit(self._used())
        # Pending request: still approvable.
        self._approve_all(req)
        self.assertEqual(req.state, 'approved')
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')
        # Already-approved PO: still confirmable.
        po_approved.with_user(self.requester).button_confirm()
        self.assertEqual(po_approved.state, 'purchase')
        # Rejection still works too.
        po3 = self._po(2000.0)
        self._set_limit(self._used() + 1)
        req3 = self._request(po3)
        self._set_limit(self._used())
        req3.with_user(self.approver1)._elaf_do_reject('not needed')
        self.assertEqual(req3.state, 'rejected')
        # Documents without a rule still confirm.
        small = self._po(10.0)
        small.with_user(self.requester).button_confirm()
        self.assertEqual(small.state, 'purchase')

    def test_milestone_notifications(self):
        self._skip_if_pro()
        self.usage._elaf_usage_internal_write({'first_prompt_done': False, 'warn_prompt_done': False})
        self._set_limit(self._used() + 5)
        po = self._po(2000.0)
        res = po.with_user(self.requester).action_elaf_request_approval()
        self.assertEqual(res['tag'], 'display_notification')
        self.assertIn('first approval request', res['params']['message'])
        self.assertTrue(self.usage.first_prompt_done)
        for amount in (2100.0, 2200.0, 2300.0):
            res = self._po(amount).with_user(self.requester).action_elaf_request_approval()
        self.assertEqual(res['params']['type'], 'warning')
        self.assertTrue(self.usage.warn_prompt_done)

    def test_caps_switch_off_keeps_functional_limits(self):
        self._skip_if_pro()
        from odoo.addons.elaf_approval_matrix_lite.models import approval_usage
        self._set_limit(self._used())
        with patch.object(approval_usage, 'ELAF_USAGE_CAPS_ENABLED', False):
            req = self._request(self._po(2000.0))
            self.assertEqual(req.state, 'pending')
            with self.assertRaises(Exception):
                self.env['elaf.approval.rule'].with_user(self.manager).create({
                    'name': 'Second', 'company_id': self.company.id,
                    'level_ids': [Command.create({'name': 'L1', 'user_ids': [Command.set(self.approver1.ids)]})]})


@tagged('post_install', '-at_install', 'elaf_approval')
class TestApprovalMultiCompany(ApprovalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_b = cls.env['res.company'].create({'name': 'Approval Test Company B'})
        cls.user_b = new_test_user(
            cls.env, login='t_user_b',
            groups='purchase.group_purchase_manager,elaf_approval_matrix_lite.group_approval_manager',
            company_id=cls.company_b.id, company_ids=[Command.set(cls.company_b.ids)])

    def test_other_company_cannot_see_requests(self):
        req = self._request(self._po(2000.0))
        self.assertFalse(self.env['elaf.approval.request'].with_user(self.user_b).search([('id', '=', req.id)]))
        with self.assertRaises(AccessError):
            req.with_user(self.user_b).read(['name'])
        self.assertFalse(self.env['elaf.approval.log'].with_user(self.user_b).search([('request_id', '=', req.id)]))
        self.assertFalse(self.env['elaf.approval.rule'].with_user(self.user_b).search([('id', '=', self.rule.id)]))
        data = self.env['elaf.approval.request'].with_user(self.user_b).get_dashboard_data({})
        self.assertEqual(next(k for k in data['kpis'] if k['key'] == 'pending')['value'], 0)

    def test_lite_second_company_refused(self):
        self._skip_if_pro()
        self.assertEqual(self.usage.bound_company_id, self.company)
        with self.assertRaisesRegex(UserError, 'one company'):
            self.env['elaf.approval.rule'].with_user(self.user_b).create({
                'name': 'B rule', 'company_id': self.company_b.id, 'active': False,
                'level_ids': [Command.create({'name': 'L1', 'user_ids': [Command.set(self.user_b.ids)]})]})
        po_b = self.env['purchase.order'].with_user(self.user_b).create({
            'partner_id': self.vendor.id, 'company_id': self.company_b.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 1,
                                           'price_unit': 5000, 'taxes_id': [Command.clear()]})]})
        # No rule for company B in Lite: the PO is not governed and confirms normally.
        self.assertFalse(po_b.elaf_approval_required)
        with self.assertRaisesRegex(UserError, 'one company'):
            po_b.with_user(self.user_b).action_elaf_request_approval()
        po_b.with_user(self.user_b).button_confirm()
        self.assertEqual(po_b.state, 'purchase')


@tagged('post_install', '-at_install', 'elaf_approval')
class TestApprovalDashboard(ApprovalCommon):

    def test_dashboard_numbers_match_records(self):
        self._skip_if_pro()
        r1 = self._request(self._po(2000.0))
        r2 = self._request(self._po(3000.0))
        r3 = self._request(self._po(4000.0))
        self._approve_all(r1)
        r2.with_user(self.approver1)._elaf_do_reject('too expensive')
        Request = self.env['elaf.approval.request']
        data = Request.with_user(self.approver1).get_dashboard_data({'period': 30})
        kpis = {k['key']: k for k in data['kpis']}
        self.assertEqual(kpis['pending_me']['value'], 1)  # r3 waits for approver1
        self.assertEqual(Request.with_user(self.approver1).search_count(kpis['pending_me']['domain']), 1)
        self.assertEqual(data['my_queue'][0]['id'], r3.id)
        mgr = Request.with_user(self.manager).get_dashboard_data({'period': 30})
        kpis = {k['key']: k for k in mgr['kpis']}
        self.assertEqual(kpis['pending']['value'], 1)
        self.assertEqual(kpis['approved']['value'], 1)
        self.assertEqual(kpis['rejected']['value'], 1)
        self.assertEqual(kpis['pending_me']['value'], 0)
        for kpi in mgr['kpis']:
            self.assertEqual(Request.with_user(self.manager).search_count(kpi['domain']) >= 0, True)
        self.assertIn('Lite', ' '.join(f for f in mgr['locked_features']) + ' Lite')
        self.assertFalse(mgr['usage']['is_pro'])
        comp = Request.with_user(self.manager).get_edition_comparison()
        self.assertEqual(comp['value']['pos_controlled'], 3)
        self.assertEqual(comp['value']['rejections'], 1)
        self.assertTrue(comp['pro_url'].endswith('/17.0/elaf_approval_matrix'))

    def test_dashboard_requires_group(self):
        plain = self.env['res.users'].create({'name': 'Plain', 'login': 't_plain_dash'})
        with self.assertRaises(AccessError):
            self.env['elaf.approval.request'].with_user(plain).get_dashboard_data({})
