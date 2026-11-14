A purchase, start to finish
###########################

What the learner sees at each step of a purchase, and what the plugin does behind it.

1. Checkout
***********

The learner chooses a paid track in Open edX, or follows a bundle's checkout link. Open edX sends them to the plugin's
checkout page with the item's SKU. The plugin checks the learner may buy it (see
:doc:`../how-tos/sell-a-course-or-a-bundle`) and opens a **cart** for it: the order, holding the item and its price.
Checkout lists the payment processors offered to the learner.

A learner has one open cart at a time: starting a new checkout cancels the older open one. If a payment for the same
item is still going through, the learner is sent back to that payment instead of being offered a second one.

2. Hand-off
***********

The learner picks a processor. The plugin moves the cart to *processing*, records which gateway it went to and the site
it was paid on, and asks the processor to open an order at the gateway. The gateway's order id is recorded on the cart,
then the learner is sent to the gateway's payment page.

A cart is handed off once. Clicking twice, or replaying the address from the browser history, reaches the gateway once.

3. At the gateway
*****************

The learner pays on the gateway's page, or leaves it. The plugin sees nothing of this until the gateway sends the
learner back, or the learner comes back to buy the item again.

4. The waiting page
*******************

The gateway sends the learner back to a page that says the payment is being confirmed. It asks the plugin, a few
seconds apart, where the payment stands; each time, the plugin asks the gateway.

* **Paid**: the plugin records the payment, issues the invoice, enrolls the learner, and sends them to the invoice.
* **Declined** or **cancelled**: the cart is cancelled and the learner sees the decline page. Nothing was charged, and
  *Try again* starts a new cart.
* **Still pending** when the page gives up: the learner is told the payment is not confirmed yet, and not to pay
  again. Coming back to buy the item later sends them back to this page.
* **Money taken, but not settled**: the gateway reports a payment the plugin will not settle -- the wrong amount, an
  answer that failed verification, an item the learner came to hold meanwhile. The payment is recorded, marked failed
  with the reason, and the cart held for staff. The learner is told the payment was received and to contact support.

5. The invoice and the course
*****************************

The invoice is issued once, numbered, and never changes. The learner is enrolled in the course, or in every course of a
bundle they do not already hold. A learner auditing the course is upgraded, keeping their progress.

The learner finds their purchases later on their payment history page, ``/payment/v1/history/``: every order paid
on that site that reached a gateway, with its status and its invoice.

When something goes wrong
*************************

The learner is never offered a second payment for an item while one may still go through. What could not be
concluded is held for staff: :doc:`../how-tos/resolve-a-payment-as-staff`. Every step is written to the cart's audit
trail: :doc:`the-audit-log`.
