The audit log
#############

Every cart keeps an audit trail: one entry for each thing that happened to it, from checkout to invoice or to its
ending. It is the first place to look when a learner asks what happened to their payment, and the record staff resolve
payments from.

Each entry holds:

* **Cart**: the cart it is about. A gateway answer naming a cart that does not exist is kept with no cart.
* **Action**: what happened, one of the names below.
* **Gateway**: the slug of the processor involved, when one was.
* **Details**: the event in a sentence, with the gateway's answer when it decided something.
* **Created at**: when.

Entries are never edited or deleted. In the LMS admin, *Zeitlabs_Payments > Audit logs* lists them all, filtered by
action, gateway and date; a cart's **Audit log** link opens its trail.

Reading a trail
***************

A settled purchase reads, oldest first:

#. ``cart_status_updated`` -- *pending* to *processing*: handed off.
#. ``redirect_to_payment_gateway`` -- the learner was sent to the gateway.
#. ``gw.*`` -- whatever the processor records of its own, such as the order it opened.
#. ``cart_status_updated`` -- *processing* to *payment_pending*: the gateway answered.
#. ``cart_status_updated`` -- *payment_pending* to *paid*: the payment was recorded.
#. ``user_enrolled`` or ``user_upgraded`` -- once per course.
#. ``cart_fulfilled`` -- the invoice was issued and the learner enrolled.

A payment still pending writes nothing on each poll: only answers that decide something are kept.

Actions
*******

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Action
     - Written when
   * - ``cart_cancelled``
     - An open cart was cancelled before checkout, because the learner started a new one.
   * - ``cart_status_updated``
     - The cart moved from one status to another, with nothing more to say about it.
   * - ``redirect_to_payment_gateway``
     - The learner was sent to the gateway's payment page.
   * - ``payment_handoff_failed``
     - The hand-off failed before the learner reached the gateway, or no gateway order was recorded within ten minutes
       of it. The cart is closed. Gives the error.
   * - ``payment_declined``
     - The gateway declined the payment. The cart is cancelled. Gives the gateway's answer.
   * - ``payment_cancelled``
     - The learner cancelled the payment on the gateway's page. The cart is cancelled. Gives the gateway's answer.
   * - ``gateway_response_rejected``
     - The gateway answered, and the plugin refused to act on the answer: unreadable, failed verification, under an
       order the cart was not handed off to, or a decline for a cart no longer awaiting payment. Gives the reason and
       the answer.
   * - ``response_for_invalid_cart``
     - A success arrived for a cart no longer awaiting payment -- cancelled, closed or refunded -- so it was not
       settled.
   * - ``transaction_not_reconciled``
     - A payment was recorded, but marked failed instead of settled. Gives the reason: another amount or currency, a
       processor that does not check payments, an item the learner already held, a second order for the cart.
   * - ``payment_unreconciled``
     - The cart was held for staff, after money the plugin did not settle.
   * - ``duplicate_transaction_detected``
     - A payment was reported again under a transaction id already recorded, so nothing new was written.
   * - ``transaction_rolled_back``
     - Recording a payment failed and nothing was written. The log has the error.
   * - ``user_enrolled``
     - The learner was enrolled in a course of the cart, in the mode bought.
   * - ``user_upgraded``
     - The learner, auditing a course of the cart, was upgraded to the mode bought.
   * - ``user_already_enrolled``
     - The learner already held a course of the cart, so it was left as it was. For a bundle, the held course is
       skipped and the rest enrolled.
   * - ``user_enrolled_error``
     - Open edX refused an enrollment. The invoice is not issued either.
   * - ``cart_fulfillment_error``
     - An item of the cart could not be fulfilled: its SKU names no course mode, or its type has no handler.
   * - ``bundle_fulfillment_failed``
     - A bundle's fulfilment failed part way. Names the courses enrolled before the failure, those already held, and
       the one that failed.
   * - ``invoice_creation_failed``
     - The payment was settled, but the invoice or the enrollment failed. The cart stays paid without an invoice, for
       staff to retry. Gives the error.
   * - ``cart_fulfilled``
     - The invoice was issued and every course enrolled.
   * - ``fulfillment_retried``
     - A staff member retried the invoice and enrollment. Names them.
   * - ``payment_accepted``
     - A staff member accepted an unreconciled payment, as confirmed with the gateway. Names them, the transaction, the
       amount, and what the plugin had recorded before.
   * - ``refund_recorded``
     - A staff member recorded a refund made in the gateway's dashboard. Names them and the refund's reference.
   * - ``payment_expired``
     - No longer written. Kept on carts that expired under an earlier version, which are now closed.
   * - ``gw.<event>``
     - Written by the processor itself, about its own work: ``gw.order_opened``, for example. Each processor documents
       its own.
