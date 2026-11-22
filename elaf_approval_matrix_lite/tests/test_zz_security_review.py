"""Independent security review (Agent 7). Each test docstring names its finding ID.

Tests marked "VULNERABILITY" fail today and must pass once the defect is fixed.
Tests marked "CONTROL" verify an existing protection and pass today.
"""
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from .common import ApprovalCommon

BLOCKED = (UserError, AccessError, ValidationError)


@tagged('post_install', '-at_install', 'elaf_approval', 'elaf_security_review')
class TestApprovalSecurityReview(ApprovalCommon):

    def _approved_confirmed_po(self, amount=2500.0):
        po = self._po(amount)
        req = self._request(po)
        self._approve_all(req)
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')
        return po, req

    def _valid(self, po):
        po.invalidate_recordset()
        req = po.sudo().elaf_approval_request_id
        return bool(req and req.state == 'approved' and req.fingerprint == po._elaf_approval_fingerprint())

    # ------------------------------------------------------------------ bypasses
    def test_am01_combined_write_state_and_currency_bypasses_approval(self):
        """SECURITY-REVIEW: AM-01 (VULNERABILITY, High).
        purchase.order.write() (elaf_approval_matrix_lite/models/purchase_order.py:459-461)
        evaluates the approval check on the values BEFORE the write. One RPC write that
        sets state='purchase' AND switches the currency confirms a PO worth 5,000 in
        company currency that falls under the 1,000 rule, without any approval.
        (Setting state together with order_line price changes is the same flaw; with
        purchase_stock installed that variant currently crashes in core Odoo.)"""
        company_cur = self.company.currency_id
        other = self.env['res.currency'].with_context(active_test=False).search(
            [('id', '!=', company_cur.id), ('name', 'in', ('EUR', 'USD', 'GBP'))], limit=1)
        other.active = True
        self.env['res.currency.rate'].search([('currency_id', 'in', (other | company_cur).ids)]).unlink()
        # 1 company currency = 10 other: 5,000 other = 500 company currency (below the rule)
        self.env['res.currency.rate'].create({
            'currency_id': other.id, 'company_id': self.company.id,
            'name': fields.Date.to_date('2000-01-01'), 'rate': 10.0})
        po = self._po(5000.0, currency=other)
        self.assertFalse(po._elaf_approval_rule())
        try:
            po.with_user(self.requester).write({'state': 'purchase', 'currency_id': company_cur.id})
        except BLOCKED:
            return
        po.invalidate_recordset()
        self.assertEqual(po.currency_id, company_cur)
        self.assertFalse(
            po.state == 'purchase' and po._elaf_approval_rule() and not self._valid(po),
            "PO confirmed at %s %s under the rule without approval through one combined write"
            % (po.amount_total, po.currency_id.name))

    def test_am02_confirmed_po_amended_after_approval(self):
        """SECURITY-REVIEW: AM-02 (VULNERABILITY, High).
        After an approved PO is confirmed, its lines stay editable. Raising the price
        10x only marks the request 'invalidated'; the PO stays confirmed with the new
        amount and goods/bills follow. A material change to a confirmed governed PO
        must be refused (or require re-approval before it takes effect)."""
        po, req = self._approved_confirmed_po(2500.0)
        with self.assertRaises(UserError, msg="confirmed PO amended 10x after approval without re-approval"):
            po.order_line.with_user(self.requester).write({'price_unit': 25000.0})

    def test_am03_confirmed_po_line_added_after_approval(self):
        """SECURITY-REVIEW: AM-03 (VULNERABILITY, High, same root cause as AM-02).
        A new line can be added to a confirmed, approved PO directly through
        purchase.order.line.create."""
        po, req = self._approved_confirmed_po(2500.0)
        with self.assertRaises(UserError, msg="line added to confirmed approved PO without re-approval"):
            self.env['purchase.order.line'].with_user(self.requester).create({
                'order_id': po.id, 'product_id': self.product2.id, 'product_qty': 100.0,
                'price_unit': 1000.0, 'tax_ids': [Command.clear()]})

    # ------------------------------------------------------------------ controls
    def test_am04_direct_state_paths_blocked(self):
        """SECURITY-REVIEW: AM-04 (CONTROL). write(state), create(state), button_approve
        and button_confirm all refuse an unapproved governed PO, also under sudo()."""
        po = self._po(2500.0)
        for call in (
            lambda: po.with_user(self.requester).write({'state': 'purchase'}),
            lambda: po.with_user(self.requester).button_approve(),
            lambda: po.with_user(self.requester).button_confirm(),
            lambda: po.sudo().button_confirm(),
            lambda: po.with_user(self.manager).write({'state': 'purchase'}),
        ):
            with self.assertRaises(UserError):
                with self.env.cr.savepoint():
                    call()
        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                self._po(2500.0, state='purchase')
        po.invalidate_recordset()
        self.assertNotEqual(po.state, 'purchase')

    def test_am05_copy_and_line_edit_paths(self):
        """SECURITY-REVIEW: AM-05 (CONTROL). copy() does not carry the approval; edits
        through purchase.order.line (qty, price, product, unlink) invalidate a pending
        or approved request before confirmation; vendor change invalidates."""
        po = self._po(2500.0)
        req = self._request(po)
        self._approve_all(req)
        dup = po.with_user(self.requester).copy()
        self.assertFalse(dup.elaf_approval_request_id)
        with self.assertRaises(UserError):
            dup.with_user(self.requester).button_confirm()
        po.order_line.with_user(self.requester).write({'product_qty': 2.0})
        req.invalidate_recordset()
        self.assertEqual(req.state, 'invalidated')
        with self.assertRaises(UserError):
            po.with_user(self.requester).button_confirm()

        po2 = self._po(2500.0)
        req2 = self._request(po2)
        self._approve_all(req2)
        po2.with_user(self.requester).write({'partner_id': self.vendor2.id})
        req2.invalidate_recordset()
        self.assertEqual(req2.state, 'invalidated')

    def test_am06_request_and_log_tampering_refused(self):
        """SECURITY-REVIEW: AM-06 (CONTROL). Users and managers cannot create requests,
        change protected request fields, approve their own request, approve out of
        turn, or edit/delete the audit log."""
        po = self._po(2500.0)
        req = self._request(po)
        for user in (self.requester, self.manager):
            with self.assertRaises(AccessError):
                with self.env.cr.savepoint():
                    req.with_user(user).write({'state': 'approved'})
            with self.assertRaises(AccessError):
                with self.env.cr.savepoint():
                    self.env['elaf.approval.request'].with_user(user).create({
                        'res_model': 'purchase.order', 'res_id': po.id, 'company_id': self.company.id,
                        'requester_id': user.id, 'state': 'approved'})
        with self.assertRaises(AccessError):
            req.with_user(self.requester)._elaf_do_approve()
        with self.assertRaises(AccessError):
            req.with_user(self.approver2)._elaf_do_approve()  # level 2 cannot jump ahead
        with self.assertRaises(AccessError):
            req.with_user(self.outsider)._elaf_do_approve()
        log = req.sudo().log_ids[:1]
        for user in (self.manager, self.env.ref('base.user_admin')):
            with self.assertRaises(UserError):
                log.with_user(user).write({'comment': 'x'})
            with self.assertRaises(UserError):
                log.with_user(user).unlink()
            with self.assertRaises(UserError):
                req.with_user(user).unlink()

    def test_am07_usage_counter_context_flag_not_honoured_for_users(self):
        """SECURITY-REVIEW: AM-07 (CONTROL). An RPC caller can put the internal
        context key in its context; the counter and its log still refuse create,
        write and unlink for user, manager and admin (no ACL)."""
        flag = {'elaf_approval_usage_internal_write': True}
        admin = self.env.ref('base.user_admin')
        for user in (self.requester, self.manager, admin):
            usage = self.usage.with_user(user).with_context(**flag)
            with self.assertRaises(UserError):
                with self.env.cr.savepoint():
                    usage.write({'used': 0, 'limit': 99999})
            with self.assertRaises(UserError):
                with self.env.cr.savepoint():
                    usage.unlink()
            with self.assertRaises(UserError):
                with self.env.cr.savepoint():
                    self.env['elaf.approval.usage'].with_user(user).with_context(**flag).create(
                        {'key': 'approval_requests_2', 'limit': 99999})
            with self.assertRaises(UserError):
                with self.env.cr.savepoint():
                    self.env['elaf.approval.usage.log'].with_user(user).with_context(**flag).create(
                        {'usage_id': self.usage.id, 'event': 'consumed'})
        self.usage.invalidate_recordset()
        self.assertEqual(self.usage.limit, 20)

    def test_am08_other_company_cannot_read_requests(self):
        """SECURITY-REVIEW: AM-08 (CONTROL). An approval manager of another company
        cannot read company A requests, levels or logs, nor see them on the dashboard."""
        po = self._po(2500.0)
        req = self._request(po)
        company_b = self.env['res.company'].create({'name': 'Review Company B'})
        mgr_b = new_test_user(self.env, login='t_sec_mgr_b',
                              groups='purchase.group_purchase_manager,elaf_approval_matrix_lite.group_approval_manager',
                              company_id=company_b.id, company_ids=[Command.set(company_b.ids)])
        Req = self.env['elaf.approval.request'].with_user(mgr_b)
        self.assertFalse(Req.search([('id', '=', req.id)]))
        self.assertFalse(self.env['elaf.approval.log'].with_user(mgr_b).search([('request_id', '=', req.id)]))
        self.assertFalse(self.env['elaf.approval.request.level'].with_user(mgr_b).search(
            [('request_id', '=', req.id)]))
        with self.assertRaises(AccessError):
            req.with_user(mgr_b).read(['amount'])
        data = Req.get_dashboard_data({})
        self.assertNotIn(req.name, str(data))
