Upgrade from 0.x
################

Version 1.0 rewrote the plugin. A deployment running a 0.x release upgrades as any install does (see
:doc:`install-and-enable`), but some of what it relies on changes or stops working. Go through this page before the
upgrade: each item says what breaks and what to do about it.

Before the upgrade
******************

**An active tax rule stops being charged.** 0.x added the newest active tax rule to the price of every cart. 1.0
calculates no tax, and the *Tax rules* admin page is gone, so a deployment with an active rule silently charges its
learners less after the upgrade. Look for a row with ``is_active`` set in the ``zeitlabs_payments_taxrule`` table
before upgrading. If there is one, do not upgrade until you have decided how the tax is to be charged.

**The migrations refuse a database they cannot convert.** Migration ``0004_release_1_0`` stops, changing nothing, while:

* an invoice has no transaction behind it;
* two transactions of the same gateway share a transaction id.

Its error says how many rows are affected and how to list them. Each row records money, so resolve them by hand –
link the invoice to the transaction that paid it, decide what each duplicate records – then run the migrations again.

**Some catalogue items can no longer be saved.** The admin refuses a catalogue item with no price, priced in a currency
other than ``valid_currency``, or with more decimal places than its currency has. Such items still exist after the
upgrade, but cannot be edited until they are corrected: find them before, and decide each one's price. A missing
price is not zero.

**Payments still open cannot be settled as usual.** A cart handed off under 0.x has neither the gateway nor the gateway
order that 1.0 records at hand-off. If its learner comes back to buy the same item, a cart still *processing* is
closed after 24 hours, and a cart in *payment pending* is refused for a person to resolve. Upgrade when no payment is
open: in the LMS admin, under *Zeitlabs_Payments > Carts*, filter *Status* by processing and by payment pending.

Settings
********

Change these in step with the upgrade, for every site that sets them. :doc:`../references/settings` explains each.

**The organization keys moved.** ``organization`` and ``customer_number`` in ``ZEITLABS_PAYMENTS_SETTINGS`` are no
longer read. Move them into ``organization_info``: ``organization`` becomes ``name``, and ``customer_number``, printed
as the tax number, becomes ``vat_no``. An invoice stores its organization when it is issued, so one issued before the
settings move names no organization, for good.

``invoice_prefix`` **is required and checked.** It must be uppercase letters, digits and inner hyphens, at most 63
characters; it is uppercased for you. While it is blank, no invoice is issued: payments are settled and left without
an invoice for staff to retry. A series issued under a lowercase prefix continues under the uppercase one. Issued
invoice numbers never change.

``valid_currency`` **is required.** Without a supported ISO 4217 code there, no catalogue item can be saved and no
payment checked.

Invoices issued under 0.x
*************************

**They show no learner or organization.** 1.0 stores both on an invoice when it is issued, and invoices issued
before have neither. Turn on the ``zeitlabs_payments.invoice.show_live_info`` waffle switch at the upgrade: while it is
on, every invoice shows the learner and organization as they are now instead of as stored. It is a temporary switch:
turn it off once the old invoices' details have been stored on them.

**They print no QR code, open on any site, and are missing from the payment history.** An invoice's QR code, the
site it is shown on, and the site whose payment history lists its order all come from the site its cart was paid on,
which 1.0 records at hand-off and 0.x did not. An order with no site recorded is listed on no site's history, the
learner's page and the history API alike. Recording the site on an old cart brings all three back.

Payment processors
******************

A processor written for 0.x does not work with 1.0 until it is updated. :doc:`build-a-payment-processor` describes
what a processor needs. In short:

* It declares ``RETURN_VIEW``, ``STATUS_VIEW`` and ``GATEWAY_ORDER_ID_PARAM``, implements ``get_gateway_order_id``,
  and has a ``SLUG`` of lowercase letters, digits and underscores. Otherwise it is skipped when the LMS starts.
* The plugin serves its return and status views, at ``/payment/v1/processor/<SLUG>/return/`` and
  ``/payment/v1/processor/<SLUG>/status/``. Give the gateway ``get_return_url()`` as the address to send the learner
  back to.
* It overrides ``reconcile_payment`` and ``verify_success_response``. Their defaults refuse every payment, so a
  processor that leaves them out has every payment held for staff.
* ``create_invoice`` takes ``(cart, transaction_record, request)``.
* ``zeitlabs_payments.helpers.get_settings`` and ``ZeitLabsPluginSettings`` are gone. A processor reads its own
  settings through ``self.get_settings()``, from the setting its ``SETTINGS_ROOT_KEY`` names.

Pages and themes
****************

* ``/payment/v1/success/<reference>/`` is gone: a settled payment leads to its invoice.
* ``/api/payment/v1/manual/``, the manual payment API, is gone.
* The ``payment_successful.html`` and ``payment_pending.html`` templates are gone, and the shared stylesheet was
  rewritten on Paragon variables. A theme overriding the old templates or styling the old classes no longer applies:
  see :doc:`customize-the-theme`.

Admin and reports
*****************

* A cart item's ``discount_amount`` and ``coupon`` can no longer be edited in the admin, and the cart API no longer
  returns them. 0.x never applied either to what the learner was charged.
* The audit actions ``bad_response_signature``, ``received_gateway_response`` and ``invalid_transaction`` are no longer
  written. Entries already holding them are kept. :doc:`../concepts/the-audit-log` lists the actions written now.
* Carts have two new statuses, ``closed`` and ``unreconciled``: see :doc:`../concepts/cart-statuses`. A report counting
  carts by status needs them.

Checkout
********

* A checkout link naming more than one SKU is refused. 0.x sold the learner the last one and dropped the others.
* A cart is handed to a gateway once. Clicking twice, or replaying the address from the browser history, no longer
  opens a second order.
