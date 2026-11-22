from odoo import _, api, models

from .approval_mixin import _num

PO_WATCHED_FIELDS = {'partner_id', 'currency_id', 'company_id', 'order_line', 'dest_address_id',
                     'payment_term_id'}
LINE_WATCHED_FIELDS = {'product_id', 'product_qty', 'price_unit', 'discount', 'taxes_id',
                       'product_uom', 'display_type', 'order_id'}
CONFIRMED_STATES = ('purchase', 'done')


class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['purchase.order', 'elaf.approval.mixin']

    # -- mixin hooks ---------------------------------------------------
    def _elaf_approval_date(self):
        return (self.date_order and self.date_order.date()) or super()._elaf_approval_date()

    def _elaf_approval_lines_data(self):
        return [
            [line.product_id.id or 0, _num(line.product_qty), _num(line.price_unit), _num(line.discount),
             sorted(line.taxes_id.ids), line.product_uom.id or 0]
            for line in self.order_line if not line.display_type
        ]

    def _elaf_approval_terms(self):
        # Dropship/delivery address and payment terms are part of what was approved.
        # date_order is not: it only drives the currency conversion (the rule is
        # re-evaluated with it at confirmation).
        return {'dest_address': self.dest_address_id.id or False,
                'payment_term': self.payment_term_id.id or False}

    def _elaf_approval_can_request(self):
        return self.state in ('draft', 'sent', 'to approve')

    def _elaf_approval_is_confirmed(self):
        return self.state in CONFIRMED_STATES

    def _elaf_approval_amend_hint(self):
        return _("Cancel the purchase order, set it back to RFQ and request a new approval.")

    # -- enforcement ---------------------------------------------------
    def button_confirm(self):
        # Early check for a clear message; write() checks again on the final values.
        self.filtered(lambda o: o.state in ('draft', 'sent'))._elaf_approval_check_allowed()
        return super().button_confirm()

    def button_approve(self, force=False):
        self.filtered(lambda o: o.state not in CONFIRMED_STATES)._elaf_approval_check_allowed()
        return super().button_approve(force=force)

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        # A PO created directly in a confirmed state (import, RPC) must also be approved.
        orders.filtered(lambda o: o.state in CONFIRMED_STATES)._elaf_approval_check_allowed()
        return orders

    def write(self, vals):
        confirming = self.browse()
        if vals.get('state') in CONFIRMED_STATES:
            confirming = self.filtered(lambda o: o.state not in CONFIRMED_STATES)
        material = bool(PO_WATCHED_FIELDS.intersection(vals))
        condition = bool(self._elaf_approval_condition_fields().intersection(vals))
        before = self._elaf_approval_before_edit(material, condition)
        res = super().write(vals)
        if material or condition:
            self._elaf_approval_after_edit(before, material)
        # Any path to a confirmed state (button, write, RPC) is checked server-side on the
        # values AFTER the write, so a field changed in the same call cannot slip past
        # the rule; the UserError rolls the write back.
        confirming._elaf_approval_check_allowed()
        return res

    def button_cancel(self):
        res = super().button_cancel()
        self.filtered(lambda o: o.state == 'cancel')._elaf_approval_cancel_open()
        return res


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    def _elaf_approval_condition_fields(self):
        """Line fields read by rule conditions (Pro extends)."""
        return set()

    @api.model_create_multi
    def create(self, vals_list):
        order_ids = {vals['order_id'] for vals in vals_list if vals.get('order_id')}
        condition = any(self._elaf_approval_condition_fields().intersection(vals) for vals in vals_list)
        before = self.env['purchase.order'].browse(order_ids)._elaf_approval_before_edit(True, condition)
        lines = super().create(vals_list)
        lines.order_id._elaf_approval_after_edit(before)
        return lines

    def write(self, vals):
        material = bool(LINE_WATCHED_FIELDS.intersection(vals))
        condition = bool(self._elaf_approval_condition_fields().intersection(vals))
        if not (material or condition):
            return super().write(vals)
        orders = self.order_id
        if 'order_id' in vals:
            orders |= self.env['purchase.order'].browse(vals['order_id'])
        before = orders._elaf_approval_before_edit(material, condition)
        res = super().write(vals)
        orders._elaf_approval_after_edit(before, material)
        return res

    def unlink(self):
        orders = self.order_id
        condition = bool(self._elaf_approval_condition_fields()) and any(
            line.analytic_distribution for line in self if 'analytic_distribution' in line._fields)
        before = orders._elaf_approval_before_edit(True, condition)
        res = super().unlink()
        orders._elaf_approval_after_edit(before)
        return res
