"""Edition hook and Lite usage allowance.

The usage counter is a local, monotonic, append-only record of how many
approval requests were submitted. It is not DRM: a database superuser or
anyone with SQL access can change local rows. It exists so the Lite limits
are enforced consistently by the ORM, as disclosed in the manifest.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError

# Switch for cumulative usage caps (store compliance kill-switch, see
# docs/FREEMIUM_SPEC.md §C). Functional limits stay active when it is False.
ELAF_USAGE_CAPS_ENABLED = True

# Lite edition limits (Python constants by design: not editable config).
LITE_MAX_REQUESTS = 20
LITE_MAX_ACTIVE_RULES = 1
LITE_MAX_LEVELS = 2
LITE_DOCUMENT_MODELS = ('purchase.order',)
USAGE_KEY_REQUESTS = 'approval_requests'
PRO_URL = 'https://apps.odoo.com/apps/modules/19.0/elaf_approval_matrix'

# Context flag set only by _elaf_usage_internal_write(); the ORM rejects any
# other write/unlink on usage rows. RPC callers cannot reach the guarded
# methods because they are private (leading underscore).
_USAGE_WRITE_FLAG = 'elaf_approval_usage_internal_write'


class ElafApprovalEdition(models.AbstractModel):
    _name = 'elaf.approval.edition'
    _description = 'Approval Matrix edition helpers'

    @api.model
    def _elaf_approval_is_pro(self):
        """Pro overrides this hook and returns True; every Lite limit checks it."""
        return False

    @api.model
    def _elaf_caps_active(self):
        return ELAF_USAGE_CAPS_ENABLED and not self._elaf_approval_is_pro()

    @api.model
    def _elaf_usage_row(self):
        return self.env['elaf.approval.usage']._get_or_create(USAGE_KEY_REQUESTS)

    @api.model
    def _elaf_bound_company(self):
        usage = self._elaf_usage_row()
        return usage.bound_company_id

    @api.model
    def _elaf_check_company_allowed(self, company, bind=False):
        """Lite works with one company. Viewing is never blocked; this is only
        called before starting a new operation (rule creation, request)."""
        if self._elaf_approval_is_pro() or not company:
            return True
        usage = self._elaf_usage_row()
        bound = usage.bound_company_id
        if bound and bound != company:
            raise UserError(self.env._(
                "The Lite edition works with one company: %(company)s. "
                "The Pro edition supports multi-company approval matrices.",
                company=bound.sudo().name))
        if not bound and bind:
            usage._elaf_usage_internal_write({'bound_company_id': company.id})
        return True

    @api.model
    def _elaf_pro_only(self, feature):
        """Raise the standard explanation when a Pro-only capability is attempted."""
        raise UserError(self.env._(
            "%(feature)s is available in the Pro edition of Approval Matrix. "
            "Open Approvals > Lite vs Pro to compare editions.", feature=feature))


class ElafApprovalUsage(models.Model):
    _name = 'elaf.approval.usage'
    _description = 'Approval Matrix usage counter'
    _rec_name = 'key'

    key = fields.Char(required=True, readonly=True, index=True)
    used = fields.Integer(readonly=True, default=0)
    limit = fields.Integer(readonly=True, default=LITE_MAX_REQUESTS)
    bound_company_id = fields.Many2one('res.company', readonly=True)
    last_used_at = fields.Datetime(readonly=True)
    first_prompt_done = fields.Boolean(readonly=True)
    warn_prompt_done = fields.Boolean(readonly=True)
    log_ids = fields.One2many('elaf.approval.usage.log', 'usage_id', readonly=True)

    _key_uniq = models.Constraint('unique(key)', 'One usage row per key.')

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(_USAGE_WRITE_FLAG):
            self.check_access('create')
            raise UserError(self.env._("Usage counters are maintained by the application only."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get(_USAGE_WRITE_FLAG):
            self.check_access('write')  # AccessError: no group has write ACL
            raise UserError(self.env._("Usage counters cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get(_USAGE_WRITE_FLAG):
            self.check_access('unlink')  # AccessError: no group has unlink ACL
            raise UserError(self.env._("Usage counters cannot be deleted."))
        return super().unlink()

    def _elaf_usage_internal_write(self, vals):
        # sudo: the counter has no write ACL for any group; callers have
        # already performed their own business checks.
        return self.sudo().with_context(**{_USAGE_WRITE_FLAG: True}).write(vals)

    @api.model
    def _get_or_create(self, key):
        row = self.sudo().search([('key', '=', key)], limit=1)
        if not row:
            # sudo: runtime row, created once; never through user input.
            row = self.sudo().with_context(**{_USAGE_WRITE_FLAG: True}).create({
                'key': key, 'limit': LITE_MAX_REQUESTS})
        return row.with_env(self.env)

    def _lock(self):
        """Row lock serialising concurrent submissions (SELECT ... FOR UPDATE)."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            'SELECT id, used FROM elaf_approval_usage WHERE id = %s FOR UPDATE', (self.id,))
        return self.env.cr.fetchone()

    def _consume(self, record_ref):
        """Atomically consume one unit. Same transaction as the request
        creation: if the operation fails later the increment rolls back.
        Returns the new used value. Raises UserError when exhausted."""
        self.ensure_one()
        edition = self.env['elaf.approval.edition']
        caps = edition._elaf_caps_active()
        self.flush_recordset()
        cr = self.env.cr
        if caps:
            cr.execute(
                'UPDATE elaf_approval_usage SET used = used + 1, last_used_at = now() at time zone \'UTC\' '
                'WHERE id = %s AND used < "limit" RETURNING used', (self.id,))
        else:
            # Pro (or caps switched off): keep counting for statistics, no limit.
            cr.execute(
                'UPDATE elaf_approval_usage SET used = used + 1, last_used_at = now() at time zone \'UTC\' '
                'WHERE id = %s RETURNING used', (self.id,))
        row = cr.fetchone()
        self.invalidate_recordset(['used', 'last_used_at'])
        if not row:
            self._log_event('denied', record_ref)
            raise UserError(self._exhausted_message())
        self._log_event('consumed', record_ref)
        return row[0]

    def _log_event(self, event, record_ref):
        # sudo: append-only log, no create ACL for users.
        self.env['elaf.approval.usage.log'].sudo().with_context(**{_USAGE_WRITE_FLAG: True}).create({
            'usage_id': self.id,
            'key': self.key,
            'user_id': self.env.user.id,
            'record_ref': record_ref,
            'event': event,
        })

    def _exhausted_message(self):
        return self.env._(
            "Lite approval allowance used (%(used)s/%(limit)s). Existing approvals "
            "continue to work; upgrade to Pro for unlimited approvals.",
            used=self.limit, limit=self.limit)

    def _is_exhausted(self):
        self.ensure_one()
        return self.env['elaf.approval.edition']._elaf_caps_active() and self.used >= self.limit

    def _usage_summary(self):
        self.ensure_one()
        edition = self.env['elaf.approval.edition']
        is_pro = edition._elaf_approval_is_pro()
        caps = edition._elaf_caps_active()
        used, limit = self.used, self.limit
        pct = min(100, round(used * 100.0 / limit)) if (caps and limit) else 0
        return {
            'is_pro': is_pro,
            'caps_enabled': caps,
            'used': used,
            'limit': limit if caps else 0,
            'remaining': max(0, limit - used) if caps else None,
            'pct': pct,
            'tone': 'bad' if pct >= 100 else ('warn' if pct >= 80 else 'ok'),
            'exhausted': caps and used >= limit,
            'bound_company': self.bound_company_id.sudo().name or '',
        }


class ElafApprovalUsageLog(models.Model):
    _name = 'elaf.approval.usage.log'
    _description = 'Approval Matrix usage log'
    _order = 'id desc'

    usage_id = fields.Many2one('elaf.approval.usage', required=True, readonly=True, ondelete='restrict')
    key = fields.Char(readonly=True)
    user_id = fields.Many2one('res.users', readonly=True)
    record_ref = fields.Char('Record', readonly=True)
    event = fields.Selection([('consumed', 'Consumed'), ('denied', 'Denied')], readonly=True)
    at = fields.Datetime(default=fields.Datetime.now, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(_USAGE_WRITE_FLAG):
            raise UserError(self.env._("The usage log is append-only and maintained by the application."))
        return super().create(vals_list)

    def write(self, vals):
        raise UserError(self.env._("The usage log cannot be modified."))

    def unlink(self):
        raise UserError(self.env._("The usage log cannot be deleted."))
