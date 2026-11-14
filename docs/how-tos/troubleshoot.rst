Troubleshoot
############

Two places tell you what happened to a payment:

* **The cart's audit trail.** In the LMS admin, open the cart under *Zeitlabs_Payments > Carts* and follow its
  **Audit log** link. It lists every step the cart went through, with the gateway's answers and the reasons for every
  refusal. Start here: see :doc:`../concepts/the-audit-log`.
* **The LMS log.** Every message the plugin logs comes from a logger under ``zeitlabs_payments``, and those of the
  status view start with the processor's slug, for example ``dev:``. With Tutor:

  .. code-block:: bash

      tutor local logs --tail=1000 lms | grep zeitlabs_payments

A cart is found by its id, by its learner's username or email, or by the gateway's order id, which every outcome page
shows the learner as *Reference*: search for it under *Carts*.

Checkout shows no buy button
****************************

* ``IS_ZEITLABS_PAYMENTS_ENABLED`` is not ``True`` for the site.
* Open edX's commerce configuration is missing, or does not have **Checkout on ecommerce service** ticked: see
  :doc:`install-and-enable`.
* The course mode has no SKU, or no catalogue item has that SKU.

Checkout refuses the course
***************************

The learner is shown the reason. The LMS log has the details, on a line starting ``Checkout refused for user``. See the
list in :doc:`sell-a-course-or-a-bundle`.

A processor is not offered at checkout
**************************************

* It is not under *Zeitlabs_Payments > Payment processors*: it did not register when the LMS started. Search the log for
  ``skipping``: the line names the processor and why -- it could not be loaded, has no ``SLUG`` or a malformed one,
  declares another ``contract_version``, or ``does not meet the hand-off contract, lacking`` some of what it needs.
* It is registered, but hides itself for this learner or this site, as the dev processor does until its waffle flag is
  active for the user. A processor that fails while deciding is hidden: search for ``treating it as hidden`` and
  ``Failed to prepare processor``.

The learner sees "Payment Error" right after checkout
*****************************************************

The hand-off to the gateway failed, before the learner reached it: search for ``could not be handed off``. The cart is
closed, nothing was charged, and *Try again* starts a new cart.

The learner sees "Payment Not Started"
**************************************

The checkout link was refused before anything was handed off: an unknown or hidden payment method, a cart that is not
the learner's or no longer pending -- a replay, or an older tab -- or a site whose domain is misconfigured. Search for
``not handed off to`` and ``asked to pay for``.

The learner is stuck on "Payment Not Yet Confirmed"
***************************************************

The waiting page gave up before the gateway gave a final answer. Nothing is lost: the learner is never asked to pay
again, and coming back to buy the course sends them to the same payment's waiting page, which asks the gateway again.

* The gateway still says pending: the payment is going through, or was abandoned on the gateway's page. It ends when
  the gateway says so.
* The gateway could not be asked: search for ``unable to ask the gateway``.
* The gateway answered something the processor cannot read: the trail has a ``gateway_response_rejected`` entry with
  the answer.

A cart is unreconciled
**********************

The gateway reports money taken, but the plugin did not settle it. The trail's ``transaction_not_reconciled`` and
``gateway_response_rejected`` entries give the reason; the transaction row keeps the gateway's whole answer. Check the
payment in the gateway's dashboard, then accept it or record its refund: :doc:`resolve-a-payment-as-staff`.

A cart is paid but has no invoice
*********************************

The payment was settled, but the invoice or the enrollment failed. The trail's ``invoice_creation_failed`` entry gives
the error, and the log has it with its traceback: search for ``Failed to fulfill cart``. Common causes:

* ``invoice_prefix`` or ``valid_currency`` is missing or invalid in the site's settings;
* the course mode's SKU or course no longer matches the catalogue item;
* Open edX refused the enrollment;
* a function named in an ``OVERRIDE_ZEITLABS_PAYMENTS_*`` setting raised or returned something an invoice cannot store.

Fix the cause, then retry: :doc:`resolve-a-payment-as-staff`.

An invoice opens on another site
********************************

An invoice is shown only on the site the payment was made on, so it is addressed in its QR code and links by that
site's domain. Asked for on another site, it redirects there. Check that site's domain under *Sites* in the LMS admin.

A page shows in English
***********************

The plugin ships English and Arabic. Other languages show English until they are translated. In Arabic, a page in
English means its text is not in the plugin's catalogue: check you run a released version.
