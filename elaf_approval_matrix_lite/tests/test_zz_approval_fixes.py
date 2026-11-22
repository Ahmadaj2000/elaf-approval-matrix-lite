"""Regression tests for the fixes of the security review findings AM-01, AM-02/03,
AM-I1 (Purchase Orders). Each docstring names the finding it covers."""
import json

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import ApprovalCommon


@tagged('post_install', '-at_install', 'elaf_approval', 'elaf_security_review')
class TestApprovalFixes(ApprovalCommon):

    def _approved_confirmed_po(self, amount=2500.0, **kw):
        po = self._po(amount, **kw)
        req = self._request(po)
        self._approve_all(req)
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')
        return po, req

    def _assert_refused(self, po, req, call):
        amount = po.amount_total
        # assertRaisesRegex (unlike Odoo's assertRaises) opens no savepoint: an RPC call
        # rolls the whole transaction back, the test rolls back to a savepoint.
        with self.assertRaisesRegex(UserError, 'does not cover this change'):
            with self.env.cr.savepoint():
                call()
        self.env.invalidate_all()
        self.assertEqual(req.state, 'approved', "the refused change must not leave the request invalidated")
        self.assertEqual(po.amount_total, amount)

    # ------------------------------------------------------------------ AM-01
    def test_fix_am01_confirm_write_checks_post_write_values(self):
        """AM-01: write(state + a governed field) is judged on the values after the write.
        An approved PO whose delivery address is changed in the same call as the
        confirmation is refused (the approval no longer covers it)."""
        address = self.env['res.partner'].create({'name': 'Other delivery site'})
        po = self._po(2500.0)
        req = self._request(po)
        self._approve_all(req)
        with self.assertRaises(UserError):
            po.with_user(self.requester).write({'state': 'purchase', 'dest_address_id': address.id})
        po.invalidate_recordset()
        self.assertNotEqual(po.state, 'purchase')
        self.assertFalse(po.dest_address_id)

    def test_fix_am01_approved_po_confirms_through_write(self):
        """AM-01 (no regression): an approved PO still confirms through write(state)."""
        po = self._po(2500.0)
        self._approve_all(self._request(po))
        po.with_user(self.requester).write({'state': 'purchase'})
        self.assertEqual(po.state, 'purchase')

    # ------------------------------------------------------------------ AM-02 / AM-03
    def test_fix_am02_confirmed_po_material_changes_refused(self):
        """AM-02/AM-03: price, quantity, product, discount, taxes, vendor, currency, added
        and removed lines of a confirmed approved PO are refused (line and header paths)."""
        po, req = self._approved_confirmed_po(2500.0)
        line = po.order_line
        tax = self.env['account.tax'].create({'name': 'Fix 10%', 'amount': 10.0, 'type_tax_use': 'purchase'})
        other_cur = self.env['res.currency'].with_context(active_test=False).search(
            [('id', '!=', po.currency_id.id), ('name', 'in', ('EUR', 'USD', 'GBP'))], limit=1)
        other_cur.active = True
        calls = [
            lambda: line.with_user(self.requester).write({'price_unit': 2600.0}),
            lambda: line.with_user(self.requester).write({'product_qty': 2.0}),
            lambda: line.with_user(self.requester).write({'product_id': self.product2.id}),
            lambda: line.with_user(self.requester).write({'discount': 50.0}),
            lambda: line.with_user(self.requester).write({'tax_ids': [Command.set(tax.ids)]}),
            lambda: po.with_user(self.requester).write({'partner_id': self.vendor2.id}),
            lambda: po.with_user(self.requester).write({'currency_id': other_cur.id}),
            lambda: po.with_user(self.requester).write(
                {'order_line': [Command.update(line.id, {'price_unit': 9000.0})]}),
            lambda: po.with_user(self.requester).write({'order_line': [Command.create({
                'product_id': self.product2.id, 'product_qty': 1.0, 'price_unit': 1.0,
                'tax_ids': [Command.clear()]})]}),
            lambda: self.env['purchase.order.line'].with_user(self.requester).create({
                'order_id': po.id, 'product_id': self.product2.id, 'product_qty': 1.0,
                'price_unit': 1.0, 'tax_ids': [Command.clear()]}),
            lambda: po.sudo().order_line.write({'price_unit': 2600.0}),
        ]
        for call in calls:
            self._assert_refused(po, req, call)

    def test_fix_am02_confirmed_po_line_removed_refused(self):
        """AM-02: removing a line of a confirmed approved PO through the order is refused."""
        po = self._po(2500.0)
        self.env['purchase.order.line'].with_user(self.requester).create({
            'order_id': po.id, 'product_id': self.product2.id, 'product_qty': 1.0, 'price_unit': 100.0,
            'tax_ids': [Command.clear()]})
        req = self._request(po)
        self._approve_all(req)
        po.with_user(self.requester).button_confirm()
        line2 = po.order_line.filtered(lambda l: l.product_id == self.product2)
        with self.assertRaises(UserError):
            po.with_user(self.requester).write({'order_line': [Command.unlink(line2.id)]})
        po.invalidate_recordset()
        self.assertIn(line2, po.order_line)
        self.assertEqual(req.state, 'approved')

    def test_fix_am02_non_material_changes_allowed(self):
        """AM-02 boundary: non-governed fields of a confirmed PO stay editable, and a change
        within the rule tolerance is accepted (the request is adjusted, not invalidated)."""
        self.rule.with_user(self.manager).write({'amount_tolerance_pct': 10.0})
        po, req = self._approved_confirmed_po(2000.0)
        po.with_user(self.requester).write({'partner_ref': 'REF-2', 'note': '<p>dock 3</p>'})
        po.order_line.with_user(self.requester).write({
            'date_planned': fields.Datetime.add(fields.Datetime.now(), days=7), 'name': 'Renamed line'})
        po.order_line.with_user(self.requester).write({'price_unit': 2100.0})  # +5%
        req.invalidate_recordset()
        self.assertEqual(req.state, 'approved')
        self.assertIn('adjusted', req.log_ids.mapped('action'))
        self._assert_refused(po, req, lambda: po.order_line.with_user(self.requester).write({'price_unit': 2300.0}))

    def test_fix_am02_unapproved_confirmed_po_cannot_grow_into_rule(self):
        """AM-02: a PO confirmed below the threshold (no approval needed) cannot then be
        raised above it."""
        po = self._po(500.0)
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')
        po.order_line.with_user(self.requester).write({'price_unit': 600.0})  # still ungoverned
        with self.assertRaisesRegex(UserError, 'requires an approval'):
            with self.env.cr.savepoint():
                po.order_line.with_user(self.requester).write({'price_unit': 5000.0})
        with self.assertRaises(UserError):
            self.env['purchase.order.line'].with_user(self.requester).create({
                'order_id': po.id, 'product_id': self.product2.id, 'product_qty': 10.0,
                'price_unit': 500.0, 'tax_ids': [Command.clear()]})
        self.env.invalidate_all()
        self.assertEqual(po.amount_total, 600.0)

    def test_fix_am02_cancel_reset_and_reapprove(self):
        """AM-02 documented path: cancel, reset to RFQ, change, request a new approval."""
        po, req = self._approved_confirmed_po(2500.0)
        po.with_user(self.requester).button_cancel()
        req.invalidate_recordset()
        self.assertEqual(req.state, 'cancelled')
        po.with_user(self.requester).button_draft()
        po.order_line.with_user(self.requester).write({'price_unit': 3000.0})
        with self.assertRaisesRegex(UserError, 'requires approval'):
            po.with_user(self.requester).button_confirm()
        self._approve_all(self._request(po))
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')
        self.assertEqual(po.amount_total, 3000.0)

    def test_fix_am02_receive_and_bill_after_approval(self):
        """AM-02 boundary: receiving the goods and creating/posting the vendor bill of an
        approved PO are Odoo's own post-confirmation updates and are not blocked."""
        po, req = self._approved_confirmed_po(2500.0)
        if 'picking_ids' not in po._fields:
            self.skipTest("purchase_stock is not installed")
        admin = self.env.ref('base.user_admin')
        picking = po.picking_ids.with_user(admin)
        self.assertTrue(picking)
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.button_validate()
        self.assertEqual(picking.state, 'done')
        self.assertEqual(po.order_line.qty_received, 1.0)
        po.with_user(admin).action_create_invoice()
        bill = po.invoice_ids.with_user(admin)
        self.assertEqual(len(bill), 1)
        bill.invoice_date = fields.Date.today()
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        self.assertEqual(po.order_line.qty_invoiced, 1.0)
        # Lock / unlock (19: `locked` flag; 17/18: the 'done' state) is not blocked, and a
        # locked order stays frozen for governed changes.
        po_admin = po.with_user(admin)
        (getattr(po_admin, 'button_lock', None) or po_admin.button_done)()
        with self.assertRaises(UserError):
            po.order_line.with_user(self.requester).write({'price_unit': 9000.0})
        self.env.invalidate_all()
        po_admin.button_unlock()
        req.invalidate_recordset()
        self.assertEqual(req.state, 'approved')
        self.assertEqual(po.state, 'purchase')
        self.assertEqual(po.amount_total, 2500.0)

    # ------------------------------------------------------------------ AM-I1
    def test_fix_ami1_dropship_address_and_payment_terms_are_covered(self):
        """AM-I1: the delivery (dropship) address and the payment terms are part of the
        approved content: changing them invalidates a draft approval and is refused on a
        confirmed PO."""
        address = self.env['res.partner'].create({'name': 'Dropship Customer Site'})
        term = self.env['account.payment.term'].search([], limit=1)
        po = self._po(2500.0)
        req = self._request(po)
        self._approve_all(req)
        po.with_user(self.requester).write({'dest_address_id': address.id})
        req.invalidate_recordset()
        self.assertEqual(req.state, 'invalidated')
        self.assertIn('payment terms', req.invalidation_reason)
        po2, req2 = self._approved_confirmed_po(2500.0)
        self._assert_refused(po2, req2, lambda: po2.with_user(self.requester).write({'dest_address_id': address.id}))
        if term:
            self._assert_refused(po2, req2, lambda: po2.with_user(self.requester).write({'payment_term_id': term.id}))

    def test_fix_ami1_requests_recorded_without_new_keys_stay_valid(self):
        """AM-I1 compatibility: a request whose snapshot predates the extra keys is compared
        on the keys it recorded (no approval is voided by an upgrade or by installing Pro)."""
        po = self._po(2500.0)
        req = self._request(po)
        self._approve_all(req)
        snapshot = json.loads(req.snapshot_json)
        snapshot.pop('terms')
        legacy = {k: v for k, v in snapshot.items() if k in ('partner', 'currency', 'company', 'lines')}
        req.sudo().write({'snapshot_json': json.dumps(legacy), 'fingerprint': po._elaf_approval_hash(legacy)})
        self.assertTrue(po._elaf_approval_fingerprint_matches(req))
        po.with_user(self.requester).write({'partner_ref': 'unrelated'})
        self.assertEqual(req.state, 'approved')
        po.with_user(self.requester).button_confirm()
        self.assertEqual(po.state, 'purchase')
        # A real change is still detected on the legacy keys.
        with self.assertRaises(UserError):
            po.order_line.with_user(self.requester).write({'price_unit': 9000.0})
