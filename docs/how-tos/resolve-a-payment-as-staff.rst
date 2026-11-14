Resolve a payment as staff
##########################

Most payments end on their own: settled, declined or cancelled. Two kinds need a person, and the plugin holds them for
staff rather than guessing:

* **Paid without an invoice.** The payment was settled, but issuing the invoice or enrolling the learner failed -- a
  closed course run, a misconfiguration, a learner-info function that raised. The learner paid and holds nothing.
* **Unreconciled.** The gateway reports money taken, but the plugin refused to settle it: an approval for another
  amount or currency, a success that failed verification, a second order for the same cart, or an item the learner
  came to hold while paying. The learner is not enrolled, and the cart keeps blocking the item.

You find both in the LMS admin, under *Zeitlabs_Payments > Carts*: filter **Status** by *unreconciled*, or use the
**Paid without invoice** filter. Open the cart's **Audit log** link first: its trail says what happened and why.

A cart is never edited by hand. It is resolved through three actions, on the cart list's action menu, each behind its
own permission.

Retry the invoice and enrollment
********************************

For a cart **paid without an invoice**, once the cause of the failure is fixed.

#. Fix the cause: the audit trail's ``invoice_creation_failed`` entry gives the error.
#. Select the cart and run *Retry the invoice and enrollment of the selected carts*.

The invoice is issued and the learner enrolled as the settlement would have done, on the site the payment was made on.
If it fails again, the cart stays paid without an invoice and the new error is on the trail. Several carts can be
retried at once; each succeeds or fails on its own.

Permission: *Can retry the invoice and enrollment of a paid cart without an invoice*.

Accept a payment
****************

For an **unreconciled** cart, once you have confirmed in the gateway's dashboard that the money was taken, and was the
cart's total.

#. Select the one cart and run *Accept the payment of the selected unreconciled cart*.
#. The page shows the cart, its total, and every answer the plugin refused with the reason. Enter the gateway's
   **transaction id** and the **amount** taken, as its dashboard shows them, and confirm.

The cart becomes paid, the invoice is issued and the learner enrolled. Accepting is refused:

* when the amount is not the cart's total: record the refund instead, and let the learner buy again at the right price;
* when another cart already pays for, or is paying for, the same item -- accepting would sell it twice: refund this one;
* when the transaction id is already recorded for another payment.

Permission: *Can accept the payment of an unreconciled cart*.

Record a refund
***************

For a cart **paid without an invoice** or **unreconciled**, once you have refunded the payment **in the gateway's
dashboard**. Nothing here calls the gateway: you record a refund already made.

#. Select the one cart and run *Record the refund of the selected cart*.
#. Enter the **reference** the gateway gave the refund. For an unreconciled cart, also enter the **amount** refunded,
   in the cart's currency; a paid cart's refund is its payment, so it takes no amount.

A refund transaction is recorded and the cart becomes refunded, which stops it blocking the item: the learner can buy
it again.

Permission: *Can record the refund of a paid cart without an invoice, or of an unreconciled cart*.

What cannot be resolved here
****************************

A cart with an invoice is a completed sale. None of the actions touch it, and refunding a completed sale is not
supported yet.
