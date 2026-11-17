Admin pages
###########

The plugin's pages are in the LMS admin under *Zeitlabs_Payments*. Every record of a payment is read-only there:
carts, items, transactions, invoices and audit entries are written by the plugin alone. Only the catalogue and the
payment site permissions are edited by hand, and payments are resolved through the cart actions.

Most pages link to the cart a row belongs to, and to that cart's audit trail.

Carts
*****

Every learner's orders.

* **List**: id, learner, status, created. Filtered by status, by **Paid without invoice**, and by date. Searched by
  cart id, learner's username or email, and the gateway's order id -- the *Reference* the learner sees.
* **Detail**: learner, status, the gateway it was handed to, the gateway's order id, the site it was paid on, a link to
  its invoice once paid, and a link to its audit trail.
* **Actions**: retry the invoice and enrollment, accept a payment, record a refund. Each is offered only to staff
  holding its permission. See :doc:`../how-tos/resolve-a-payment-as-staff`.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Permission
     - Allows
   * - Can retry the invoice and enrollment of a paid cart without an invoice
     - *Retry the invoice and enrollment of the selected carts*
   * - Can accept the payment of an unreconciled cart
     - *Accept the payment of the selected unreconciled cart*
   * - Can record the refund of a paid cart without an invoice, or of an unreconciled cart
     - *Record the refund of the selected cart*

Cart items
**********

What each cart holds: the catalogue item, its price at the time, and the price paid. Filtered by the cart's status;
searched by cart id and SKU.

Catalogue items
***************

What the plugin sells, and the only page you edit. See :doc:`../how-tos/sell-a-course-or-a-bundle`.

* **List**: id, SKU, type, price, currency. Filtered by type, searched by SKU.
* **Detail**: SKU, type (*paid course* or *program bundle*), title, description, item ref id, price and currency. A
  bundle's form lists its courses below.

Saving refuses an item with no price, a currency other than the deployment's, or more decimal places than the
currency has.

Bundle course items
*******************

Each course of each bundle: the bundle and the course's catalogue item. The same links are edited on the bundle's own
page.

Transactions
************

Every payment and refund recorded.

* **List**: id, cart, type (*payment* or *refund*), status, gateway, the gateway's transaction id, method, amount,
  currency, the user who made it, created. Filtered by type, status, gateway, method, currency and date; searched by
  the gateway's transaction id, cart id, and the user's username or email.
* **Detail**: adds the gateway's whole answer (``response``) and the reason. A payment the plugin did not settle is
  recorded with status *failed* and its reason after ``not reconciled:``.

The amount is what the learner was asked to pay. What the gateway reported is in its answer.

Audit logs
**********

Every audit entry. Filtered by action, gateway and date; searched by learner, action, details and gateway. See
:doc:`../concepts/the-audit-log`.

Invoices
********

Every invoice issued.

* **List**: number, cart, status, total, currency, the payment it bills, paid at, created. Filtered by status and
  currency; searched by number and cart id.
* **Detail**: adds the gross total, the learner and organization details stored when it was issued, and its items.

Invoice items
*************

The lines of each invoice: the cart item billed, its prices.

Payment site permissions
************************

What each site's employees may do with its payments. See :doc:`../how-tos/give-site-employees-access`.

* **List**: user, site, **Users order history**, updated. Filtered by site and by the flag; searched by the user's
  username or email.
* **Detail**: the user, picked by id, the site, and the flag.

One row per user and site. A row with the flag off grants nothing, and is kept.

Payment processors
******************

The processors registered when the LMS started: name, slug and class. A processor installed but missing here was
skipped: see :doc:`../how-tos/troubleshoot`.
