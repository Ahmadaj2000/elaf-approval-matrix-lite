from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import ApprovalCommon


@tagged('post_install', '-at_install', 'elaf_approval')
class TestApprovalWorkflow(ApprovalCommon):

    # ---------------------------------------------------------------- matching
    def test_rule_matching_amount_band(self):
        self.assertFalse(self._po(999.99)._elaf_approval_rule())
        self.assertEqual(self._po(1000.0)._elaf_approval_rule(), self.rule)
        self.assertEqual(self._po(250000.0)._elaf_approval_rule(), self.rule)
        self.rule.with_user(self.manager).write({'max_amount': 5000.0})
        self.assertEqual(self._po(5000.0)._elaf_approval_rule(), self.rule)
        self.assertFalse(self._po(5000.01)._elaf_approval_rule())

    def test_rule_matching_converts_foreign_currency(self):
        company_cur = self.company.currency_id
        other = self.env['res.currency'].with_context(active_test=False).search(
            [('id', '!=', company_cur.id), ('name', 'in', ('EUR', 'USD', 'GBP'))], limit=1)
        other.active = True
        self.env['res.currency.rate'].search([('currency_id', 'in', (other | company_cur).ids)]).unlink()
        # 1 company currency = 0.5 other currency -> 600 other = 1,200 company currency
        self.env['res.currency.rate'].create({
            'currency_id': other.id, 'company_id': self.company.id,
            'name': fields.Date.to_date('2000-01-01'), 'rate': 0.5})
        po = self._po(600.0, currency=other)
        facts = po._elaf_approval_facts()
        self.assertAlmostEqual(facts['amount_company'], 1200.0, places=2)
        self.assertEqual(po._elaf_approval_rule(), self.rule)
        po_small = self._po(400.0, currency=other)  # 800 in company currency
        self.assertFalse(po_small._elaf_approval_rule())

    def test_no_rule_confirms_directly(self):
        po = self._po(100.0)
        po.button_confirm()
        self.assertEqual(po.state, 'purchase')
        with self.assertRaises(UserError):
            po.with_user(self.requester).action_elaf_request_approval()

    # ---------------------------------------------------------------- sequential flow
    def test_sequential_levels_and_activities(self):
        po = self._po(2500.0)
        self.assertTrue(po.elaf_approval_required)
        self.assertTrue(po.elaf_can_request_approval)
        req = self._request(po)
        self.assertEqual(req.state, 'pending')
        self.assertEqual(req.amount, 2500.0)
        self.assertEqual(req.rule_id, self.rule)
        self.assertEqual(req.requester_id, self.requester)
        self.assertEqual(req.current_approver_ids, self.approver1)
        self.assertEqual(req.level_ids.mapped('state'), ['active', 'waiting'])
        act_type = self.env.ref('elaf_approval_matrix_lite.mail_activity_type_approval')
        acts = req.activity_ids.filtered(lambda a: a.activity_type_id == act_type)
        self.assertEqual(acts.user_id, self.approver1)

        # Level 2 approver cannot jump ahead.
        with self.assertRaises(AccessError):
            req.with_user(self.approver2)._elaf_do_approve()
        self.assertFalse(po.with_user(self.approver2).elaf_can_approve)
        self.assertTrue(po.with_user(self.approver1).elaf_can_approve)

        req.with_user(self.approver1)._elaf_do_approve('fine')
        self.assertEqual(req.state, 'pending')
        self.assertEqual(req.level_ids.mapped('state'), ['approved', 'active'])
        self.assertEqual(req.current_approver_ids, self.approver2)
        acts = req.activity_ids.filtered(lambda a: a.activity_type_id == act_type)
        self.assertEqual(acts.user_id, self.approver2)
        # Approver 1 cannot approve twice.
        with self.assertRaises(AccessError):
            req.with_user(self.approver1)._elaf_do_approve()

        # Through the wizard, as the UI does.
        wiz = self.env['elaf.approval.decision.wizard'].with_user(self.approver2).create({
            'request_id': req.id, 'decision': 'approve', 'comment': 'budget ok'})
        wiz.action_confirm()
        self.assertEqual(req.state, 'approved')
        self.assertTrue(req.date_decided)
        self.assertFalse(req.current_approver_ids)
        self.assertFalse(req.activity_ids.filtered(lambda a: a.activity_type_id == act_type))
        self.assertEqual(req.log_ids.mapped('action'), ['approved', 'approve', 'approve', 'submit'])
        self.assertEqual(req.log_ids.filtered(lambda l: l.action == 'approve').user_id,
                         self.approver1 | self.approver2)
        po.invalidate_recordset()
        self.assertEqual(po.elaf_approval_state, 'approved')
        self.assertTrue(po.elaf_approval_valid)
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')

    def test_group_level_approver(self):
        group = self.env['res.groups'].create({'name': 'Approval test finance group',
                                               'user_ids': [Command.set(self.outsider.ids)]})
        self.rule.level_ids.filtered(lambda l: l.sequence == 2).write(
            {'approver_type': 'group', 'group_id': group.id, 'user_ids': [Command.clear()]})
        req = self._request(self._po(3000.0))
        req.with_user(self.approver1)._elaf_do_approve()
        self.assertEqual(req.current_approver_ids, self.outsider)
        req.with_user(self.outsider)._elaf_do_approve()
        self.assertEqual(req.state, 'approved')

    # ---------------------------------------------------------------- reject
    def test_reject_requires_reason(self):
        po = self._po(1500.0)
        req = self._request(po)
        with self.assertRaises(UserError):
            req.with_user(self.approver1)._elaf_do_reject('   ')
        wiz = self.env['elaf.approval.decision.wizard'].with_user(self.approver1).create({
            'request_id': req.id, 'decision': 'reject', 'comment': ''})
        with self.assertRaises(UserError):
            wiz.action_confirm()
        req.with_user(self.approver1)._elaf_do_reject('Price above market')
        self.assertEqual(req.state, 'rejected')
        self.assertEqual(req.reject_reason, 'Price above market')
        self.assertEqual(req.level_ids.mapped('state'), ['rejected', 'cancelled'])
        log = req.log_ids.filtered(lambda l: l.action == 'reject')
        self.assertEqual(log.comment, 'Price above market')
        self.assertEqual(log.user_id, self.approver1)
        with self.assertRaises(UserError):
            po.with_user(self.requester).button_confirm()
        # Can be resubmitted after rejection: a new request.
        self.assertTrue(po.elaf_can_request_approval)
        req2 = self._request(po)
        self.assertNotEqual(req, req2)
        self.assertEqual(req2.state, 'pending')

    # ---------------------------------------------------------------- enforcement
    def test_confirm_blocked_without_approval(self):
        po = self._po(5000.0)
        with self.assertRaisesRegex(UserError, 'requires approval'):
            po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'draft')
        # Manager is not exempt either.
        with self.assertRaises(UserError):
            po.with_user(self.manager).button_confirm()
        # Pending is still blocked.
        req = self._request(po)
        with self.assertRaisesRegex(UserError, 'pending'):
            po.with_user(self.requester).button_confirm()
        req.with_user(self.approver1)._elaf_do_approve()
        with self.assertRaises(UserError):
            po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'draft')

    def test_direct_state_write_blocked(self):
        po = self._po(5000.0)
        with self.assertRaises(UserError):
            po.with_user(self.requester).write({'state': 'purchase'})
        with self.assertRaises(UserError):
            po.with_user(self.manager).button_approve()
        with self.assertRaises(UserError):
            po.sudo().write({'state': 'purchase'})
        self.assertEqual(po.state, 'draft')

    def test_create_confirmed_po_blocked(self):
        with self.assertRaises(UserError):
            self._po(5000.0, state='purchase')

    def test_rpc_style_calls_blocked(self):
        """Simulate JSON-RPC: call_kw on button_confirm and write."""
        from odoo.service.model import call_kw
        po = self._po(4000.0)
        env_req = self.env(user=self.requester)
        with self.assertRaises(UserError):
            call_kw(env_req['purchase.order'], 'button_confirm', [po.ids], {})
        with self.assertRaises(UserError):
            call_kw(env_req['purchase.order'], 'write', [po.ids, {'state': 'purchase'}], {})
        self.assertEqual(po.state, 'draft')

    def test_request_record_protected(self):
        req = self._request(self._po(2000.0))
        for user in (self.requester, self.approver1, self.manager):
            with self.assertRaises(AccessError):
                req.with_user(user).write({'state': 'approved'})
        with self.assertRaises(AccessError):
            self.env['elaf.approval.request'].with_user(self.manager).create({
                'res_model': 'purchase.order', 'res_id': 1, 'company_id': self.company.id,
                'requester_id': self.manager.id, 'state': 'approved'})
        with self.assertRaises(AccessError):
            req.level_ids[0].with_user(self.manager).write({'state': 'approved'})
        with self.assertRaises(UserError):
            req.with_user(self.manager).unlink()
        self.assertEqual(req.state, 'pending')

    def test_audit_log_immutable(self):
        req = self._request(self._po(2000.0))
        log = req.log_ids[:1]
        self.assertTrue(log)
        with self.assertRaises(UserError):
            log.with_user(self.manager).write({'comment': 'tampered'})
        with self.assertRaises(UserError):
            log.sudo().write({'comment': 'tampered'})
        with self.assertRaises(UserError):
            log.with_user(self.manager).unlink()
        with self.assertRaises(UserError):
            log.sudo().unlink()
        with self.assertRaises(AccessError):
            self.env['elaf.approval.log'].with_user(self.manager).create({
                'request_id': req.id, 'action': 'approve', 'user_id': self.manager.id})

    # ---------------------------------------------------------------- SoD (Lite)
    def test_requester_cannot_self_approve(self):
        # Requester configured as level-1 approver together with approver1.
        self.rule.level_ids.filtered(lambda l: l.sequence == 1).write(
            {'user_ids': [Command.set((self.requester | self.approver1).ids)]})
        req = self._request(self._po(2000.0))
        self.assertNotIn(self.requester, req.level_ids[0].approver_ids)
        with self.assertRaises(AccessError):
            req.with_user(self.requester)._elaf_do_approve()
        with self.assertRaises(AccessError):
            req.with_user(self.requester)._elaf_do_reject('no')
        self.assertFalse(req.with_user(self.requester).can_act)

    def test_requester_only_approver_is_refused(self):
        self.rule.level_ids.filtered(lambda l: l.sequence == 1).write(
            {'user_ids': [Command.set(self.requester.ids)]})
        used = self._used()
        with self.assertRaisesRegex(UserError, 'no eligible approver'):
            self._request(self._po(2000.0))
        self.assertEqual(self._used(), used)

    def test_non_approver_blocked(self):
        po = self._po(2000.0)
        req = self._request(po)
        with self.assertRaises(AccessError):
            req.with_user(self.outsider)._elaf_do_approve()
        with self.assertRaises(AccessError):
            req.with_user(self.outsider)._elaf_do_reject('no')
        # The outsider cannot even read the request (record rule).
        with self.assertRaises(AccessError):
            req.with_user(self.outsider).read(['state'])
        self.assertFalse(po.with_user(self.outsider).elaf_can_approve)
        # An approval manager who is not an approver of the level cannot approve either.
        with self.assertRaises(AccessError):
            req.with_user(self.manager)._elaf_do_approve()

    def test_user_without_approval_group_cannot_request(self):
        buyer = self.env['res.users'].create({
            'name': 'Plain buyer', 'login': 't_plain_buyer',
            'group_ids': [Command.set(self.env.ref('purchase.group_purchase_user').ids)]})
        po = self._po(2000.0, user=buyer)
        with self.assertRaises(AccessError):
            po.with_user(buyer).action_elaf_request_approval()

    # ---------------------------------------------------------------- invalidation
    def test_invalidation_on_quantity_change(self):
        po = self._po(2000.0)
        req = self._request(po)
        self._approve_all(req)
        self.assertEqual(req.state, 'approved')
        po.with_user(self.requester).write({'order_line': [Command.update(po.order_line.id, {'product_qty': 2.0})]})
        self.assertEqual(req.state, 'invalidated')
        self.assertTrue(req.invalidation_reason)
        self.assertIn('invalidated', req.log_ids.mapped('action'))
        with self.assertRaisesRegex(UserError, 'invalidated'):
            po.with_user(self.requester).button_confirm()
        # New approval is possible and required.
        req2 = self._request(po)
        self._approve_all(req2)
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')

    def test_invalidation_on_vendor_change(self):
        po = self._po(2000.0)
        req = self._request(po)
        self._approve_all(req)
        po.with_user(self.requester).write({'partner_id': self.vendor2.id})
        self.assertEqual(req.state, 'invalidated')
        self.assertIn('partner', req.invalidation_reason)

    def test_invalidation_on_line_added_and_removed(self):
        po = self._po(2000.0)
        req = self._request(po)
        self._approve_all(req)
        self.env['purchase.order.line'].with_user(self.requester).create({
            'order_id': po.id, 'product_id': self.product2.id, 'product_qty': 1, 'price_unit': 1.0,
            'tax_ids': [Command.clear()]})
        self.assertEqual(req.state, 'invalidated')
        req2 = self._request(po)
        self._approve_all(req2)
        po.order_line.filtered(lambda l: l.product_id == self.product2).with_user(self.requester).unlink()
        self.assertEqual(req2.state, 'invalidated')

    def test_invalidation_on_price_change_during_pending(self):
        po = self._po(2000.0)
        req = self._request(po)
        po.order_line.with_user(self.requester).write({'price_unit': 2100.0})
        self.assertEqual(req.state, 'invalidated')
        self.assertFalse(req.current_approver_ids)
        with self.assertRaises(UserError):
            req.with_user(self.approver1)._elaf_do_approve()

    def test_tolerance_keeps_minor_changes(self):
        self.rule.with_user(self.manager).write({'amount_tolerance_pct': 10.0})
        po = self._po(2000.0)
        req = self._request(po)
        self._approve_all(req)
        po.order_line.with_user(self.requester).write({'price_unit': 2100.0})  # +5%
        self.assertEqual(req.state, 'approved')
        self.assertEqual(req.amount, 2100.0)
        self.assertIn('adjusted', req.log_ids.mapped('action'))
        self.assertEqual(req.baseline_amount_company, 2000.0)
        # Tolerance is measured against the originally submitted amount, so repeated
        # small edits cannot creep past it: 2,250 is +12.5% of 2,000.
        po.order_line.with_user(self.requester).write({'price_unit': 2250.0})
        self.assertEqual(req.state, 'invalidated')

    def test_irrelevant_change_keeps_approval(self):
        po = self._po(2000.0)
        req = self._request(po)
        self._approve_all(req)
        po.with_user(self.requester).write({'partner_ref': 'VENDOR-REF-1', 'note': '<p>Deliver to dock 2</p>'})
        self.assertEqual(req.state, 'approved')
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')

    def test_change_after_confirmation_refused(self):
        """Was test_change_after_confirmation_invalidates. Since the AM-02 fix a material
        change of a confirmed, approved PO is refused (it used to be accepted with the
        request merely marked invalidated); the approval and the order stay as approved."""
        po = self._po(2000.0)
        req = self._request(po)
        self._approve_all(req)
        po.with_user(self.requester).button_confirm()
        with self.assertRaisesRegex(UserError, 'does not cover this change'):
            with self.env.cr.savepoint():
                po.order_line.with_user(self.requester).write({'product_qty': 3.0})
        self.env.invalidate_all()
        self.assertEqual(req.state, 'approved')
        self.assertEqual(po.order_line.product_qty, 1.0)
        self.assertEqual(po.state, 'purchase')

    def test_duplicate_click_is_idempotent(self):
        po = self._po(2000.0)
        req = self._request(po)
        used = self._used()
        res = po.with_user(self.requester).action_elaf_request_approval()
        self.assertEqual(res['tag'], 'display_notification')
        po.invalidate_recordset()
        self.assertEqual(po.elaf_approval_request_id, req)
        self.assertEqual(self._used(), used)
        self.assertEqual(self.env['elaf.approval.request'].search_count(
            [('res_model', '=', 'purchase.order'), ('res_id', '=', po.id)]), 1)

    def test_cancel_po_cancels_request(self):
        po = self._po(2000.0)
        req = self._request(po)
        po.with_user(self.requester).button_cancel()
        self.assertEqual(req.state, 'cancelled')

    def test_requester_cancel_and_outsider_cannot(self):
        req = self._request(self._po(2000.0))
        with self.assertRaises(AccessError):
            req.with_user(self.approver1).action_cancel()
        req.with_user(self.requester).action_cancel()
        self.assertEqual(req.state, 'cancelled')

    def test_copy_does_not_copy_approval(self):
        po = self._po(2000.0)
        req = self._request(po)
        self._approve_all(req)
        dup = po.with_user(self.requester).copy()
        self.assertFalse(dup.elaf_approval_request_id)
        with self.assertRaises(UserError):
            dup.with_user(self.requester).button_confirm()

    # ---------------------------------------------------------------- Lite rule limits
    def test_lite_single_active_rule(self):
        self._skip_if_pro()
        with self.assertRaises(ValidationError):
            self.env['elaf.approval.rule'].with_user(self.manager).create({
                'name': 'Second', 'company_id': self.company.id, 'min_amount': 50000,
                'level_ids': [Command.create({'name': 'L1', 'user_ids': [Command.set(self.approver1.ids)]})]})
        # Archived second rule is allowed, activating it is not.
        second = self.env['elaf.approval.rule'].with_user(self.manager).create({
            'name': 'Second', 'company_id': self.company.id, 'min_amount': 50000, 'active': False,
            'level_ids': [Command.create({'name': 'L1', 'user_ids': [Command.set(self.approver1.ids)]})]})
        with self.assertRaises(ValidationError):
            second.with_user(self.manager).write({'active': True})
        # Archive the first one -> the second can be activated.
        self.rule.with_user(self.manager).write({'active': False})
        second.with_user(self.manager).write({'active': True})
        self.assertTrue(second.active)

    def test_lite_max_two_levels(self):
        self._skip_if_pro()
        with self.assertRaises(ValidationError):
            self.rule.with_user(self.manager).write({'level_ids': [Command.create(
                {'name': 'L3', 'sequence': 3, 'user_ids': [Command.set(self.manager.ids)]})]})
        with self.assertRaises(ValidationError):
            self.env['elaf.approval.rule.level'].with_user(self.manager).create({
                'rule_id': self.rule.id, 'name': 'L3', 'sequence': 3,
                'user_ids': [Command.set(self.manager.ids)]})

    def test_rule_config_access(self):
        with self.assertRaises(AccessError):
            self.rule.with_user(self.requester).write({'min_amount': 0})
        with self.assertRaises(AccessError):
            self.rule.with_user(self.approver1).unlink()

    def test_archived_rule_stops_requiring_approval(self):
        po = self._po(2000.0)
        self.rule.with_user(self.manager).write({'active': False})
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')
