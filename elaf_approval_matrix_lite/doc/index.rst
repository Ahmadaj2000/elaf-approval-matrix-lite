====================
Approval Matrix Lite
====================

Amount-based approval rules for Purchase Orders, enforced on the server, with
material-change invalidation and an audit log that cannot be edited.

.. contents:: Contents
   :local:
   :depth: 1

Overview
========

Approval Matrix Lite adds an approval step before a Purchase Order can be confirmed.

* **Approval rules** select approvers from the order total, converted to the company currency.
  Each rule has an amount band (minimum and maximum; a maximum of 0 means no maximum), an
  amount tolerance (default 0%) and up to two levels.
* Each **level** names specific users or a security group. One approval is needed per level,
  and levels are approved in order.
* **Request Approval** on the Purchase Order creates an approval request. The approvers of the
  current level get a to-do activity.
* **Server-side enforcement**: an order that falls under a rule cannot be confirmed (button,
  write of the state, creation in a confirmed state, or an RPC call of these) unless its
  request is approved and the order still matches it. The check runs on the values the order
  has after the confirming call, so fields changed in the same call are included.
* **Material-change invalidation**: a fingerprint of vendor, currency, company, lines, delivery
  (dropship) address and payment terms is stored with the request. A change of any of them, or an
  amount increase beyond the tolerance, invalidates the approval.
* **Confirmed orders are frozen**: once an order is confirmed, a change of governed content that
  its approval does not cover (vendor, currency, lines, prices, quantities, taxes, discount,
  delivery address, payment terms) is refused. Receipts, vendor bills and other Odoo operations
  after confirmation are not affected.
* **Audit log** of every request, decision, invalidation and cancellation.
* **Approvals Overview** dashboard and the **Lite vs Pro** page.
* English and Arabic user interface.

Installation
============

1. Copy the module into your addons path, or install it from the Odoo Apps menu.
2. Update the apps list and install **Approval Matrix Lite**. The Purchase and Discuss
   applications are installed automatically if needed.
3. Odoo Online is not supported, because it does not run third-party Python modules. Use
   Odoo.sh or an on-premise installation.

Compatibility: Odoo 18.0 Community Edition (tested). Odoo 17.0 and 18.0 are distributed as
separate packages.

Configuration
=============

1. **Users**: in *Settings > Users*, set the *Approval Matrix* access to **User** for buyers and
   approvers, and to **Manager** for the people who maintain the rules.
2. **Rule**: open *Approvals > Configuration > Approval Rules* (also available under
   *Purchase > Configuration > Approval Rules*) and create a rule:

   * Company, minimum and maximum amount (company currency).
   * Amount tolerance in percent. With 0, any change of quantity or price after approval
     invalidates the request.
   * Levels: for each level choose *Specific Users* or *Security Group*.

Approvers must be active internal users with access to the order's company.

Usage
=====

Request an approval
-------------------

1. Create a Request for Quotation. If it falls under a rule, a banner says that approval is required.
2. Click **Request Approval**. The request is created in the *Pending* state, and the first-level
   approvers get an activity.

Approve or reject
-----------------

* Approvers open *Approvals > My Approvals* (filter *Waiting for Me*) or the order itself and
  click **Approve** or **Reject**.
* A comment is optional when approving and required when rejecting.
* When the last level approves, the request is *Approved* and the order can be confirmed.

Changes after approval
----------------------

If the order changes materially after approval (or while pending), the request becomes
*Invalidated*. Click **Request Approval** again. A change within the tolerance is only logged.

After the order is confirmed, a material change is refused with the message *"... is confirmed
and its approval does not cover this change"*. To amend a confirmed order: cancel it, set it back
to RFQ, change it and request a new approval. Receiving goods, creating and posting the vendor
bill, changing planned dates, notes or references, and locking or unlocking the order remain
possible. A change within the tolerance is accepted and logged. An order confirmed without
approval (below every rule) cannot be raised so that a rule applies.

Cancel
------

The requester or a manager can cancel a request. Cancelling the order cancels its open request.

Overview and audit log
----------------------

* *Approvals > Overview* shows requests waiting for you, pending requests and amount,
  approved, rejected, average approval time and invalidations for 7, 30, 90 or 365 days.
  Each figure opens the requests behind it.
* *Approvals > Audit Log* lists every action with user, time, level, amount and comment.

Lite edition limits
===================

* One company per database: the first company that uses the app.
* Purchase Orders only.
* One active approval rule. To use another rule, archive the current one first.
* Up to 2 levels per rule, one approval per level, sequential.
* Up to 20 approval requests in total.

  * A request is counted when it is submitted.
  * Resubmitting after invalidation counts as a new request.
  * A second click while a request is active does not count.
  * Deleting requests does not give them back.

When the 20 requests are used:

* Approval enforcement stays on. New Purchase Orders that need approval cannot be confirmed,
  and the order shows *Approval allowance used*.
* Existing pending requests can still be approved or rejected, and approved orders can still be
  confirmed.
* A manager may archive the rule to stop requiring approvals. That is the customer's choice.
* History stays viewable.

The usage counter is shown on the Overview and under *Approvals > Lite vs Pro*. It is a local
record in your database. There is no activation key and no remote check. Someone with direct
database access could change it, so it is not a licence protection.

The **Discover Pro** button on the Lite vs Pro page opens the Pro listing on the Odoo Apps
store in your browser. The link works once the Pro listing is published. No data is sent.

Security and permissions
========================

* **User**: request approvals, see own requests and the requests they must approve, act on the
  levels where they are an eligible approver.
* **Manager**: configure rules, see all requests of the allowed companies.
* A requester can never approve or reject their own request. If the requester is the only
  approver of a level, the request is refused.
* Approval requests, their levels and the audit log are read-only for users. Only the
  application's own actions create or change them. Audit log entries cannot be changed or
  deleted, even by administrators.
* Multi-company record rules apply to rules, requests and the audit log.
* All data stays in your Odoo database. The module makes no external network calls.
  Notifications and activities use Odoo's standard messaging and your own outgoing mail
  server, if one is configured.

Enforcement boundary
--------------------

* **Enforced**: Purchase Order confirmation through Odoo's methods (``button_confirm``,
  ``button_approve``, writing ``state = 'purchase'``, creating an order in that state, and RPC
  calls of these), for every user, including code running as superuser, checked on the values
  after the call. Changes of governed content of a confirmed order (through the order or its
  lines: write, create, unlink) unless the approval covers them.
* **Not enforced**:

  * Direct SQL changes and database restores.
  * Administrators archiving a rule, deleting levels or uninstalling the module.
  * Orders that match no active rule.
  * Other state changes, for example sending an RFQ or cancelling.
  * Odoo's own operations after confirmation (receipts, bills, planned dates, lock/unlock).

The full text is in ``doc/ENFORCEMENT.md`` of Approval Matrix (Pro).

FAQ
===

**Can a requester approve their own request?**
  No.

**Which amount is used?**
  The order total, converted to the company currency.

**What if two rules match?**
  Lite allows only one active rule. In general, the rule with the lowest sequence wins.

**Can I uninstall the module?**
  Yes. Uninstalling removes the approval step and its data.

Known limitations
=================

* Enforcement works at the Odoo ORM level. Direct SQL is not controlled.
* Only Purchase Orders are covered.
* Not available on Odoo Online.

Support
=======

Community support, best effort: info@elaf-development.com
