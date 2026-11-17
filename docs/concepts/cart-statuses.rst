Cart statuses
#############

A cart is a learner's order. Its status says where the payment stands, and decides whether the learner can buy the
same item again.

.. mermaid::

    stateDiagram-v2
        direction TB
        [*] --> pending
        pending --> processing : handed off
        pending --> cancelled : a newer cart replaced it
        processing --> payment_pending : the gateway answered
        processing --> paid : payment settled
        processing --> closed : hand-off failed
        payment_pending --> paid : payment settled
        payment_pending --> cancelled : declined or cancelled
        payment_pending --> unreconciled : money taken, not settled
        unreconciled --> paid : staff accepted the payment
        unreconciled --> refunded : staff recorded the refund
        paid --> refunded : staff recorded the refund (no invoice)

Statuses with no arrow out of them are final.

.. list-table::
   :header-rows: 1
   :widths: 18 52 15

   * - Status
     - Meaning
     - Blocks buying again
   * - ``pending``
     - The learner's open cart, at checkout. Starting a new checkout cancels it.
     - No
   * - ``processing``
     - Handed off to a gateway, which has not answered yet. A cart the learner abandoned on the gateway's page stays
       here, since nothing tells the plugin.
     - Yes
   * - ``payment_pending``
     - The gateway has answered, and the payment has not settled yet.
     - Yes
   * - ``paid``
     - The payment is recorded. The invoice and the enrollment follow; a paid cart without an invoice is one whose
       fulfilment failed, waiting for staff.
     - Yes
   * - ``cancelled``
     - Ended without payment: declined or cancelled at the gateway, or replaced by a newer cart before hand-off.
     - No
   * - ``closed``
     - Ended without payment because the hand-off failed on the plugin's side, before the learner reached the gateway,
       or no gateway order was recorded within ten minutes of it.
     - No
   * - ``unreconciled``
     - The gateway reports money taken, but the plugin did not settle it. Waits for staff to accept or refund it.
     - Yes
   * - ``refund_requested``
     - **Not implemented yet.** Reserved for refund requests, a future feature. Nothing in the plugin sets it, so no
       cart holds it today.
     - Yes
   * - ``refunded``
     - The payment was refunded in the gateway's dashboard, and staff recorded it.
     - No

"Blocks buying again" covers the same item: the same course in any paid mode, or a bundle containing it, for a course;
the same bundle, for a bundle. A learner whose payment is still open is sent back to it rather than refused.

A cart is never moved back. A cart that left *pending* is never handed off again: the learner starts a new one.
