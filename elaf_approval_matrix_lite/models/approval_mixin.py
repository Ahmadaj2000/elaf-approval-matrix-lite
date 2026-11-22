import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_repr, float_round

from .approval_request import ACTIVE_STATES, REQUEST_STATES
from .approval_usage import LITE_DOCUMENT_MODELS


def _num(value):
    return float_repr(float_round(value or 0.0, precision_digits=6), 6)


class ElafApprovalMixin(models.AbstractModel):
    """Approval behaviour shared by every governed document type.

    Concrete models implement the small `_elaf_approval_*` hooks (amount,
    partner, lines, states). Lite applies it to purchase.order; Pro applies it
    to more document types.
    """
    _name = 'elaf.approval.mixin'
    _description = 'Approval Matrix document mixin'

    elaf_approval_request_id = fields.Many2one(
        'elaf.approval.request', 'Approval Request', copy=False, readonly=True, index='btree_not_null')
    elaf_approval_state = fields.Selection(
        string='Approval Status', related='elaf_approval_request_id.state', store=True)
    elaf_approval_rule_id = fields.Many2one('elaf.approval.rule', 'Approval Rule',
                                            compute='_compute_elaf_approval')
    elaf_approval_required = fields.Boolean('Approval Required', compute='_compute_elaf_approval')
    elaf_approval_valid = fields.Boolean('Approval Valid', compute='_compute_elaf_approval')
    elaf_can_request_approval = fields.Boolean(compute='_compute_elaf_approval')
    elaf_can_approve = fields.Boolean(compute='_compute_elaf_approval')
    elaf_lite_exhausted = fields.Boolean(compute='_compute_elaf_approval')
    elaf_approval_count = fields.Integer('Approvals', compute='_compute_elaf_approval_count')
    elaf_approval_waiting_for = fields.Char(related='elaf_approval_request_id.current_level_name',
                                            string='Approval Level')
    elaf_approval_reason = fields.Text(compute='_compute_elaf_approval')

    # ------------------------------------------------------------------
    # Hooks implemented by concrete document models
    # ------------------------------------------------------------------
    def _elaf_approval_amount(self):
        """Return (amount, currency) of the document."""
        return self.amount_total, self.currency_id

    def _elaf_approval_date(self):
        return fields.Date.context_today(self)

    def _elaf_approval_partner(self):
        return self.partner_id

    def _elaf_approval_lines_data(self):
        return []

    def _elaf_approval_can_request(self):
        return True

    def _elaf_approval_enforced(self):
        """Pro may switch enforcement off per document type."""
        return True

    def _elaf_approval_type_label(self):
        return self.env['ir.model']._get(self._name).name

    def _elaf_approval_is_confirmed(self):
        """True when the document is in (or past) its controlled state (PO confirmed,
        SO confirmed, bill posted). Confirmed documents are frozen for governed changes."""
        return False

    def _elaf_approval_terms(self):
        """Commercial terms that an approval covers besides partner, currency and lines
        (delivery address, payment terms). Returned as a JSON-serialisable dict."""
        return {}

    def _elaf_approval_amend_hint(self):
        return _("Cancel the document, reset it to draft and request a new approval.")

    def _elaf_approval_condition_fields(self):
        """Document fields read by rule conditions (Pro extends)."""
        return set()

    def _elaf_approval_track_conditions(self):
        """Pro: True when edits of rule-condition fields by the current user must be tracked."""
        return False

    def _elaf_approval_pin(self, old_rule):
        """Pro: remember a rule that a condition-field edit took the document out of."""
        return False

    # ------------------------------------------------------------------
    # Facts, snapshot, fingerprint
    # ------------------------------------------------------------------
    def _elaf_approval_facts(self, requester=None):
        self.ensure_one()
        amount, currency = self._elaf_approval_amount()
        company = self.company_id or self.env.company
        company_currency = company.currency_id
        amount_company = amount
        if currency and company_currency and currency != company_currency:
            amount_company = currency._convert(amount, company_currency, company, self._elaf_approval_date())
        partner = self._elaf_approval_partner()
        return {
            'res_model': self._name,
            'company_id': company.id,
            'amount': amount,
            'currency_id': currency.id,
            'amount_company': amount_company,
            'partner_id': partner.id,
            'requester_id': (requester or self.env.user).id,
            'record': self,
        }

    def _elaf_approval_snapshot(self):
        self.ensure_one()
        amount, currency = self._elaf_approval_amount()
        return {
            'partner': self._elaf_approval_partner().id or False,
            'currency': currency.id or False,
            'company': self.company_id.id or False,
            'lines': sorted(self._elaf_approval_lines_data()),
            'terms': self._elaf_approval_terms(),
        }

    @api.model
    def _elaf_approval_hash(self, snapshot):
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()

    def _elaf_approval_fingerprint(self):
        return self._elaf_approval_hash(self._elaf_approval_snapshot())

    def _elaf_approval_fingerprint_matches(self, req, snapshot=None):
        """True when the document content still equals what `req` approved.

        A request stores the snapshot it hashed. When a later version (or Pro) adds
        keys to the snapshot, a request recorded before keeps being compared on the
        keys it recorded, so installing Pro or upgrading does not void approvals."""
        self.ensure_one()
        if not req or not req.fingerprint:
            return False
        snapshot = snapshot if snapshot is not None else self._elaf_approval_snapshot()
        if self._elaf_approval_hash(snapshot) == req.fingerprint:
            return True
        try:
            old_keys = set(json.loads(req.snapshot_json or '{}'))
        except ValueError:
            return False
        if not old_keys or not old_keys < set(snapshot):
            return False
        legacy = {key: value for key, value in snapshot.items() if key in old_keys}
        return self._elaf_approval_hash(legacy) == req.fingerprint

    def _elaf_req(self):
        """Current request in sudo: documents read approval status for every user
        who can read the document, even when the request itself is not visible
        to them (record rules). Only status fields are exposed."""
        return self.elaf_approval_request_id.sudo()

    def _elaf_approval_rule(self, requester=None):
        self.ensure_one()
        if not self.company_id:
            return self.env['elaf.approval.rule']
        return self.env['elaf.approval.rule']._find_rule(self._elaf_approval_facts(requester))

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends_context('uid')
    @api.depends('elaf_approval_request_id.state', 'company_id')
    def _compute_elaf_approval(self):
        edition = self.env['elaf.approval.edition']
        exhausted = edition._elaf_usage_row()._is_exhausted()
        for rec in self:
            rule = rec._elaf_approval_rule() if rec.company_id else rec.env['elaf.approval.rule']
            req = rec._elaf_req()
            valid = bool(req and req.state == 'approved' and rec.id
                         and rec._elaf_approval_fingerprint_matches(req))
            rec.elaf_approval_rule_id = rule.id or False
            rec.elaf_approval_required = bool(rule) and rec._elaf_approval_enforced()
            rec.elaf_approval_valid = valid
            active_req = req and req.state in ACTIVE_STATES
            rec.elaf_can_request_approval = bool(
                rule and rec.id and not active_req and rec._elaf_approval_can_request())
            rec.elaf_can_approve = bool(req and req.state == 'pending' and req.sudo().can_act)
            rec.elaf_lite_exhausted = exhausted
            reason = False
            if req and req.state == 'rejected':
                reason = req.reject_reason
            elif req and req.state == 'invalidated':
                reason = req.invalidation_reason
            rec.elaf_approval_reason = reason

    def _compute_elaf_approval_count(self):
        data = self.env['elaf.approval.request']._read_group(
            [('res_model', '=', self._name), ('res_id', 'in', self.ids)], ['res_id'], ['__count'])
        counts = dict(data)
        for rec in self:
            rec.elaf_approval_count = counts.get(rec.id, 0)

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------
    def _elaf_approval_check_allowed(self):
        """Raise UserError unless every record may be in its confirmed state.

        Callers run it on the values the document has AFTER the write that moves it
        (the raised error rolls the whole write back), so fields changed together
        with the state are part of the decision."""
        for rec in self:
            if not rec._elaf_approval_enforced():
                continue
            rule = rec._elaf_approval_rule(requester=rec._elaf_req().requester_id or None)
            if not rule:
                continue
            req = rec._elaf_req()
            if rec._elaf_approval_covers(req, rule):
                continue
            raise UserError(rec._elaf_approval_block_message(rule, req))
        return True

    def _elaf_approval_covers(self, req, rule):
        """An approved request covers the document when its content is unchanged and it
        was granted under the rule that applies now."""
        self.ensure_one()
        return bool(req and req.state == 'approved' and req.rule_id == rule
                    and self._elaf_approval_fingerprint_matches(req))

    # ------------------------------------------------------------------
    # Edits: confirmed documents are frozen, condition edits are tracked
    # ------------------------------------------------------------------
    def _elaf_approval_before_edit(self, material=True, condition=False):
        """Capture the governance state of the documents before an edit.

        `material`: the edit touches governed content (partner, currency, terms, lines).
        `condition`: the edit touches a field that rule conditions read (Pro)."""
        before = {}
        if not (material or condition):
            return before
        track = condition and self._elaf_approval_track_conditions()
        for rec in self:
            if not rec.id or not rec.company_id:
                continue
            if rec._elaf_approval_is_confirmed():
                if rec._elaf_approval_enforced():
                    req = rec._elaf_req()
                    before[rec.id] = ('confirmed', rec._elaf_approval_fingerprint(),
                                      rec._elaf_approval_rule(requester=req.requester_id or None))
            elif track:
                before[rec.id] = ('draft', False, rec._elaf_approval_rule())
        return before

    def _elaf_approval_after_edit(self, before, material=True):
        """Run after an edit: invalidate/adjust open requests, refuse a governed change
        of a confirmed document that its approval does not cover, pin evaded rules."""
        docs = self.exists()
        # Material and condition edits both change the approved content (fingerprint).
        docs._elaf_approval_on_change()
        for rec in docs:
            kind, fingerprint, old_rule = before.get(rec.id, (None, None, None))
            if kind == 'confirmed':
                rec._elaf_approval_guard_confirmed(fingerprint, old_rule)
            elif kind == 'draft' and old_rule and not rec._elaf_approval_is_confirmed() \
                    and rec._elaf_approval_rule() != old_rule:
                rec._elaf_approval_pin(old_rule)
        return True

    def _elaf_approval_guard_confirmed(self, old_fingerprint, old_rule):
        """A confirmed document must stay covered by its approval.

        Allowed: changes that do not touch governed content, changes kept within the
        rule's tolerance (the request was adjusted), and changes of documents that no
        rule governs before or after the change. Everything else is refused; the
        raised error rolls the change (and any invalidation it caused) back."""
        self.ensure_one()
        if not self._elaf_approval_is_confirmed() or not self._elaf_approval_enforced():
            return True
        req = self._elaf_req()
        rule = self._elaf_approval_rule(requester=req.requester_id or None)
        if old_fingerprint == self._elaf_approval_fingerprint() and old_rule == rule:
            return True
        if not old_rule and not rule:
            return True
        if rule and self._elaf_approval_covers(req, rule):
            return True
        if not rule and req and req.state == 'approved' and self._elaf_approval_fingerprint_matches(req):
            return True
        if req and req.state == 'invalidated' and req.invalidation_reason:
            reason = req.invalidation_reason
        elif rule and req and req.state == 'approved' and req.rule_id != rule:
            reason = _("the document now falls under rule '%s'", rule.name)
        elif rule:
            reason = _("rule '%s' requires an approval for this content", rule.name)
        else:
            reason = _("approved content changed")
        raise UserError(_(
            "%(doc)s is confirmed and its approval does not cover this change (%(reason)s). "
            "%(hint)s", doc=self.display_name, reason=reason, hint=self._elaf_approval_amend_hint()))

    def _elaf_approval_block_message(self, rule, req):
        self.ensure_one()
        doc = self.display_name
        if req and req.state == 'pending':
            return _(
                "%(doc)s cannot be confirmed: approval %(req)s is still pending (level: %(level)s; "
                "waiting for: %(users)s).", doc=doc, req=req.name, level=req.current_level_name or '-',
                users=', '.join(req.current_approver_ids.mapped('name')) or '-')
        if req and req.state == 'rejected':
            return _(
                "%(doc)s cannot be confirmed: approval %(req)s was rejected (%(reason)s). "
                "Update the document and request approval again.", doc=doc, req=req.name,
                reason=req.reject_reason or '-')
        if req and req.state == 'invalidated':
            reason = _(
                "%(doc)s cannot be confirmed: approval %(req)s was invalidated (%(reason)s). "
                "Request a new approval.", doc=doc, req=req.name, reason=req.invalidation_reason or '-')
        elif req and req.state == 'approved':
            reason = _(
                "%(doc)s cannot be confirmed: it changed after approval %(req)s. Request a new approval.",
                doc=doc, req=req.name)
        else:
            reason = _(
                "%(doc)s requires approval under rule '%(rule)s' before it can be confirmed. "
                "Use 'Request Approval' first.", doc=doc, rule=rule.name)
        usage = self.env['elaf.approval.edition']._elaf_usage_row()
        if usage._is_exhausted():
            return usage._exhausted_message()
        return reason

    def _elaf_material_change_reason(self, req, snapshot, facts):
        """Return a human-readable reason when the change is material, else False."""
        old = json.loads(req.snapshot_json or '{}')
        if old.get('partner') != snapshot['partner']:
            return _("partner changed")
        if old.get('currency') != snapshot['currency']:
            return _("currency changed")
        if old.get('company') != snapshot['company']:
            return _("company changed")
        if 'terms' in old and old['terms'] != snapshot.get('terms'):
            return _("delivery address or payment terms changed")
        old_products = sorted(str(line[0]) for line in old.get('lines', []))
        new_products = sorted(str(line[0]) for line in snapshot['lines'])
        if old_products != new_products:
            return _("order lines added, removed or products changed")
        company_currency = self.company_id.currency_id
        tol = req.rule_id.amount_tolerance_pct or 0.0
        baseline = req.baseline_amount_company or req.amount_company
        limit = baseline * (1 + tol / 100.0)
        if company_currency.compare_amounts(facts['amount_company'], limit) > 0:
            return _("amount increased from %(old)s to %(new)s",
                              old=company_currency.format(baseline),
                              new=company_currency.format(facts['amount_company']))
        if not tol and json.dumps(old.get('lines', [])) != json.dumps(snapshot['lines']):
            return _("quantities or prices changed")
        new_rule = self._elaf_approval_rule(requester=req.requester_id)
        if new_rule and new_rule != req.rule_id:
            return _("the document now falls under rule '%s'", new_rule.name)
        return False

    def _elaf_approval_on_change(self):
        """Invalidate (or adjust within tolerance) active requests after an edit."""
        for rec in self:
            req = rec._elaf_req()
            if not req or req.state not in ACTIVE_STATES:
                continue
            snapshot = rec._elaf_approval_snapshot()
            fingerprint = rec._elaf_approval_hash(snapshot)
            if rec._elaf_approval_fingerprint_matches(req, snapshot):
                continue
            facts = rec._elaf_approval_facts(requester=req.requester_id)
            reason = rec._elaf_material_change_reason(req, snapshot, facts)
            if reason:
                req._elaf_invalidate(reason)
            else:
                req._elaf_adjust_snapshot(
                    fingerprint, snapshot, facts['amount'], facts['amount_company'],
                    _("Edit within the %s%% tolerance; approval kept.",
                               req.rule_id.amount_tolerance_pct))

    def _elaf_approval_cancel_open(self):
        for rec in self:
            req = rec._elaf_req()
            if req and req.state in ACTIVE_STATES:
                # sudo: the document itself is being cancelled by an authorised user.
                req.sudo()._elaf_close('cancelled')
                req.sudo()._elaf_log('cancel', comment=_("Document cancelled"))

    # ------------------------------------------------------------------
    # Request submission
    # ------------------------------------------------------------------
    def _elaf_approval_prepare_levels(self, rule, requester):
        self.ensure_one()
        request_model = self.env['elaf.approval.request']
        vals_list, conflicts = [], []
        for level in rule.level_ids.sorted('sequence'):
            candidates = level._candidate_users(self.company_id)
            eligible, level_conflicts = request_model._elaf_filter_eligible(
                level, candidates - requester, self, requester)
            conflicts += [(level, user, reason) for user, reason in level_conflicts]
            required = level._required_approvals(len(eligible))
            if len(eligible) < max(required, 1):
                raise UserError(_(
                    "Level '%(level)s' of rule '%(rule)s' has no eligible approver for this document "
                    "(the requester cannot approve their own request; approvers must have access to "
                    "company %(company)s). Ask an approval manager to update the rule.",
                    level=level.name, rule=rule.name, company=self.company_id.name))
            vals_list.append({
                'level_id': level.id,
                'sequence': level.sequence,
                'name': level.name,
                'approver_ids': [fields.Command.set(eligible.ids)],
                'required_approvals': required,
                'state': 'waiting',
            })
        if not vals_list:
            raise UserError(_("Rule '%s' has no levels. Ask an approval manager to add one.", rule.name))
        return vals_list, conflicts

    def action_elaf_request_approval(self):
        self.ensure_one()
        user = self.env.user
        edition = self.env['elaf.approval.edition']
        if not user.has_group('elaf_approval_matrix_lite.group_approval_user'):
            raise AccessError(_("You need the Approvals / User access right to request approvals."))
        self.check_access_rights('write')
        self.check_access_rule('write')
        if not edition._elaf_approval_is_pro() and self._name not in LITE_DOCUMENT_MODELS:
            edition._elaf_pro_only(_("Approvals for %s", self._elaf_approval_type_label()))
        if not self._elaf_approval_can_request():
            raise UserError(_("Approval can only be requested while the document is a draft."))
        edition._elaf_check_company_allowed(self.company_id, bind=True)

        usage = edition._elaf_usage_row()
        usage._lock()  # serialise concurrent submissions, then re-check idempotency
        self.invalidate_recordset(['elaf_approval_request_id'])
        current = self._elaf_req()
        snapshot = self._elaf_approval_snapshot()
        fingerprint = self._elaf_approval_hash(snapshot)
        if current and current.state in ACTIVE_STATES:
            if self._elaf_approval_fingerprint_matches(current, snapshot):
                return self._elaf_notify(_("Approval %(req)s is already %(state)s.", req=current.name,
                                                    state=dict(REQUEST_STATES)[current.state].lower()), 'info')
            current._elaf_invalidate(_("document changed before resubmission"))

        facts = self._elaf_approval_facts(requester=user)
        rule = self._elaf_approval_rule(requester=user)
        if not rule or not self._elaf_approval_enforced():
            raise UserError(_("No approval rule applies to %s; it can be confirmed directly.",
                                       self.display_name))
        levels_vals, conflicts = self._elaf_approval_prepare_levels(rule, user)
        all_approvers = self.env['res.users']
        for vals in levels_vals:
            all_approvers |= self.env['res.users'].browse(vals['approver_ids'][0][2])
        # sudo: requests are created only here, after the checks above.
        request = self.env['elaf.approval.request'].sudo().create({
            'res_model': self._name,
            'res_id': self.id,
            'document_name': self.display_name,
            'company_id': self.company_id.id,
            'requester_id': user.id,
            'rule_id': rule.id,
            'partner_id': facts['partner_id'],
            'currency_id': facts['currency_id'],
            'amount': facts['amount'],
            'amount_company': facts['amount_company'],
            'baseline_amount_company': facts['amount_company'],
            'fingerprint': fingerprint,
            'snapshot_json': json.dumps(snapshot),
            'state': 'pending',
            'date_requested': fields.Datetime.now(),
            'level_ids': [fields.Command.create(v) for v in levels_vals],
            'all_approver_ids': [fields.Command.set(all_approvers.ids)],
        })
        used = usage._consume('%s,%s' % (self._name, self.id))
        self.sudo().write({'elaf_approval_request_id': request.id})
        request._elaf_log('submit')
        request._elaf_after_submit(self, conflicts)
        request._elaf_activate_next()
        self.message_post(body=_("Approval %(req)s requested under rule '%(rule)s'.",
                                          req=request.name, rule=rule.name), subtype_xmlid='mail.mt_note')
        return self._elaf_milestone(usage, used)

    def _elaf_notify(self, message, ntype='success', title=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title or _("Approval Matrix"),
                'message': message,
                'type': ntype,
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _elaf_milestone(self, usage, used):
        """Non-intrusive Lite milestone toasts (first request, 80% of allowance)."""
        edition = self.env['elaf.approval.edition']
        if not edition._elaf_caps_active():
            return self._elaf_notify(_("Approval requested."))
        limit = usage.limit
        if not usage.first_prompt_done:
            usage._elaf_usage_internal_write({'first_prompt_done': True})
            return self._elaf_notify(_(
                "Your first approval request is on its way. Approval Matrix Lite includes %(limit)s "
                "approval requests; %(left)s remain.", limit=limit, left=max(0, limit - used)), 'success',
                _("Approval requested"))
        if used * 100 >= limit * 80 and not usage.warn_prompt_done:
            usage._elaf_usage_internal_write({'warn_prompt_done': True})
            return self._elaf_notify(_(
                "%(used)s of %(limit)s Lite approval requests used. See Approvals > Lite vs Pro for "
                "unlimited approvals.", used=used, limit=limit), 'warning', _("Approval requested"))
        return self._elaf_notify(_("Approval requested (%(used)s of %(limit)s Lite requests used).",
                                            used=used, limit=limit))

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_elaf_approve(self):
        self.ensure_one()
        if not self.elaf_approval_request_id:
            raise UserError(_("There is no approval request on this document."))
        return self.elaf_approval_request_id.action_approve()

    def action_elaf_reject(self):
        self.ensure_one()
        if not self.elaf_approval_request_id:
            raise UserError(_("There is no approval request on this document."))
        return self.elaf_approval_request_id.action_reject()

    def action_elaf_view_requests(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Approval Requests"),
            'res_model': 'elaf.approval.request',
            'view_mode': 'tree,form',
            'domain': [('res_model', '=', self._name), ('res_id', '=', self.id)],
        }

    def action_elaf_open_comparison(self):
        return {'type': 'ir.actions.client', 'tag': 'elaf_approval_matrix_lite.comparison',
                'name': _("Lite vs Pro")}
