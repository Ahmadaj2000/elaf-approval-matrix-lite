from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user


class ApprovalCommon(TransactionCase):
    """Shared fixtures: one company, a 2-level rule from 1,000 upwards."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Odoo 17: _() falls back to the user's language when the context language is not
        # active (the test template only has Arabic active). The assertions check the
        # English messages, so make 'en_US' a valid context language (18/19 env._ does not
        # need this).
        cls.env['res.lang']._activate_lang('en_US')
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, mail_notrack=True, lang='en_US'))
        cls.company = cls.env.company
        purchase_user = 'purchase.group_purchase_user'
        appr_user = 'elaf_approval_matrix_lite.group_approval_user'
        appr_mgr = 'elaf_approval_matrix_lite.group_approval_manager'
        cls.requester = new_test_user(cls.env, login='t_requester', groups=f'{purchase_user},{appr_user}',
                                      company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.approver1 = new_test_user(cls.env, login='t_approver1', groups=f'{purchase_user},{appr_user}',
                                      company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.approver2 = new_test_user(cls.env, login='t_approver2', groups=f'{purchase_user},{appr_user}',
                                      company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.outsider = new_test_user(cls.env, login='t_outsider', groups=f'{purchase_user},{appr_user}',
                                     company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.manager = new_test_user(cls.env, login='t_manager',
                                    groups=f'purchase.group_purchase_manager,{appr_mgr}',
                                    company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.vendor = cls.env['res.partner'].create({'name': 'Test Vendor A', 'company_id': False})
        cls.vendor2 = cls.env['res.partner'].create({'name': 'Test Vendor B', 'company_id': False})
        cls.product = cls.env['product.product'].create({
            'name': 'Approval Test Product', 'purchase_ok': True, 'type': 'consu', 'standard_price': 10.0})
        cls.product2 = cls.env['product.product'].create({
            'name': 'Approval Test Product 2', 'purchase_ok': True, 'type': 'consu', 'standard_price': 10.0})
        # Archive any pre-existing rule so the Lite single-active-rule limit holds.
        cls.env['elaf.approval.rule'].search([]).write({'active': False})
        cls.rule = cls.env['elaf.approval.rule'].with_user(cls.manager).create({
            'name': 'Purchases from 1,000',
            'company_id': cls.company.id,
            'min_amount': 1000.0,
            'max_amount': 0.0,
            'level_ids': [
                Command.create({'name': 'Department head', 'sequence': 1, 'approver_type': 'users',
                                'user_ids': [Command.set(cls.approver1.ids)]}),
                Command.create({'name': 'Finance', 'sequence': 2, 'approver_type': 'users',
                                'user_ids': [Command.set(cls.approver2.ids)]}),
            ],
        })
        cls.usage = cls.env['elaf.approval.edition']._elaf_usage_row()

    def _po(self, amount, user=None, partner=None, currency=None, **kw):
        vals = {
            'partner_id': (partner or self.vendor).id,
            'company_id': self.company.id,
            'order_line': [Command.create({
                'product_id': self.product.id, 'product_qty': 1.0, 'price_unit': amount,
                'taxes_id': [Command.clear()]})],
        }
        if currency:
            vals['currency_id'] = currency.id
        vals.update(kw)
        return self.env['purchase.order'].with_user(user or self.requester).create(vals)

    def _request(self, po, user=None):
        po.with_user(user or self.requester).action_elaf_request_approval()
        po.invalidate_recordset()
        return po.elaf_approval_request_id

    def _approve_all(self, req):
        req.with_user(self.approver1)._elaf_do_approve('ok level 1')
        req.with_user(self.approver2)._elaf_do_approve('ok level 2')
        req.invalidate_recordset()

    def _skip_if_pro(self):
        """Lite-edition limit tests do not apply once Pro removes the limits;
        the Pro test-suite asserts the opposite behaviour."""
        if self.env['elaf.approval.edition']._elaf_approval_is_pro():
            self.skipTest("Lite edition limit: not applicable with Pro installed")

    def _used(self):
        self.usage.invalidate_recordset()
        return self.usage.used

    def _set_limit(self, limit):
        self.usage._elaf_usage_internal_write({'limit': limit})
