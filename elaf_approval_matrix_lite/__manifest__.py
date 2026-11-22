{
    'name': 'Approval Matrix Lite',
    'version': '17.0.1.1.0',
    'category': 'Purchases',
    'summary': 'Amount-based approval rules for Purchase Orders with server-side '
               'enforcement and audit trail. Lite: one company, one rule, '
               'up to 2 levels, up to 20 approval requests in total.',
    'description': """
Approval Matrix Lite
====================
Control every critical business decision.

* Approval rules for Purchase Orders by amount band (company currency).
* Sequential approval levels; a specific user list or a security group per level.
* "Request Approval" on the PO, Approve / Reject (reason required) from the PO
  or from "My Approvals".
* Server-side enforcement: a PO under a rule cannot be confirmed (button, write
  of the state, RPC) unless its approval request is approved and still matches.
* Material-change invalidation: vendor, currency or line changes, or an amount
  increase beyond the rule tolerance, invalidate the approval.
* Immutable audit log, approvals dashboard, English and Arabic.

Lite edition limits (fully disclosed)
-------------------------------------
* One company.
* Purchase Orders only.
* One active approval rule.
* Up to 2 levels per rule, one approver needed per level (sequential).
* Up to 20 approval requests in total (a request is counted when it is
  submitted; resubmitting after invalidation counts as a new request).
* When the allowance is used, approval enforcement stays on; existing requests
  can still be approved or rejected and approved POs can still be confirmed.
  A manager may archive the rule to stop requiring approvals.

A Pro edition is available.
""",
    'author': 'Elaf Development',
    'website': 'https://www.elaf-development.com/',
    'support': 'info@elaf-development.com',
    'license': 'LGPL-3',
    'depends': ['purchase', 'mail'],
    'data': [
        'security/approval_security.xml',
        'security/ir.model.access.csv',
        'data/approval_data.xml',
        'wizard/approval_decision_wizard_views.xml',
        'views/approval_rule_views.xml',
        'views/approval_request_views.xml',
        'views/approval_log_views.xml',
        'views/approval_usage_views.xml',
        'views/purchase_order_views.xml',
        'views/approval_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'elaf_approval_matrix_lite/static/src/scss/elaf_tokens.scss',
            'elaf_approval_matrix_lite/static/src/dashboard/*',
            'elaf_approval_matrix_lite/static/src/comparison/*',
        ],
    },
    'images': ['static/description/banner.png', 'static/description/main_screenshot.png'],
    'application': True,
    'installable': True,
}
