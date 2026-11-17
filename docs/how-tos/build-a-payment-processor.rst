Build a payment processor
#########################

A payment processor connects the plugin to one payment gateway. It is a Python package installed beside the plugin,
which registers a processor class and two views. The plugin does everything else: checkout, the cart's statuses, the
waiting page, recording the payment, the invoice, the enrollment, the outcome pages and the audit trail.

The `dev processor`_ is a complete processor against a stand-in gateway, and the best example to start from. Every
snippet below is taken from it.

.. _dev processor: https://github.com/zeit-labs/zeitlabs-dev-processor

How a payment runs through a processor
**************************************

#. The learner clicks your processor's button at checkout. The plugin moves the cart to *processing* and calls your
   processor to hand it off.
#. Your processor opens an order for the cart at the gateway, and returns the page that sends the learner there. The
   plugin records the gateway's order id on the cart before the learner leaves.
#. The learner pays on the gateway's page. The gateway sends them back to your **return view**, with the order id in
   the address. It shows the plugin's waiting page.
#. The waiting page polls your **status view**. Each poll asks the gateway about the order, and your view tells the
   plugin what the answer means: paid, still pending, declined, or cancelled.
#. On a success, the plugin checks the answer through your processor, records the payment, issues the invoice, enrolls
   the learner, and sends them to the invoice. On a decline or cancellation it ends the cart and shows the decline page.

Register the processor
**********************

Declare your processor class under the ``zeitlabs_payments.v1`` entry point group, and your package as an LMS Django
app if it ships templates, translations or views of its own:

.. code-block:: python

    # setup.py
    entry_points={
        'lms.djangoapp': [
            'my_gateway = my_gateway.apps:MyGatewayConfig',
        ],
        'zeitlabs_payments.v1': [
            'my_gateway = my_gateway.processor:MyGatewayProcessor',
        ],
    },

The plugin loads every processor when the LMS starts, and lists those it registered under *Zeitlabs_Payments >
Payment processors* in the LMS admin. A processor that breaks a rule below is skipped, with an error in the LMS log
naming what is wrong, and the LMS starts all the same.

The processor class
*******************

Subclass ``BaseProcessor`` and declare:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Attribute
     - Meaning
   * - ``SLUG``
     - The processor's name everywhere it is recorded: in the URLs, on carts, transactions and audit entries.
       Lowercase letters, digits and underscores, at most 50 characters. Never change it once payments are recorded.
   * - ``NAME``
     - The processor's name for staff, as the admin shows it.
   * - ``CHECKOUT_TEXT``
     - The text of its button at checkout. Mark it for translation.
   * - ``TEMPLATE_NAME``
     - The hand-off page: the template that sends the learner to the gateway. It is rendered with
       ``transaction_parameters``, what ``get_transaction_parameters`` returned.
   * - ``RETURN_VIEW``, ``STATUS_VIEW``
     - The dotted paths of your return view and status view. The plugin serves them at
       ``/payment/v1/processor/<SLUG>/return/`` and ``/payment/v1/processor/<SLUG>/status/``.
   * - ``GATEWAY_ORDER_ID_PARAM``
     - The query parameter your gateway's redirect back carries the order id in, as the gateway names it.
   * - ``contract_version``
     - ``1``. A processor declaring another version is skipped.
   * - ``SETTINGS_ROOT_KEY``
     - Optional: the name of the Django setting holding your processor's configuration, for example
       ``'MY_GATEWAY_SETTINGS'``. Read it with ``self.get_settings()``, which takes a site's configuration first, so
       each site can have its own credentials.

And implement:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Method
     - What it does
   * - ``get_transaction_parameters(cart, request, ...)``
     - Opens an order for the cart at the gateway, and returns what the hand-off page needs, including the order id.
       Start from ``self.get_transaction_parameters_base(cart, request)``: it gives the learner's language, the
       ``order_reference`` the gateway must keep for the order, the ``amount`` and ``currency`` to charge, the learner's
       email and an order description. Raise anything to fail the hand-off: the plugin closes the cart and shows the
       learner an error page with a way to try again.
   * - ``get_gateway_order_id(transaction_parameters)``
     - Returns the gateway's order id from what ``get_transaction_parameters`` returned. Required: a processor that does
       not implement it is skipped, and a hand-off that names no order fails.
   * - ``reconcile_payment(response, cart)``
     - Checks that the money the gateway reports taken is the cart's total, in the cart's currency, and raises
       ``GatewayError`` when it is not. Only your processor knows how its gateway reports an amount. The default refuses
       every payment, so a processor that leaves it out has every payment held for staff.
   * - ``is_hidden_for(request)``
     - Optional: returns ``True`` to keep the processor off checkout for this request -- not configured for this site,
       or only for some users. A hidden processor cannot be used by typing its address either.
   * - ``map_currency(value)``
     - Optional: turns the currency your gateway reports into an ISO 4217 code. The default already reads ISO numeric
       codes, so ``'840'`` becomes ``'USD'``.

The dev processor's hand-off, in short:

.. code-block:: python

    class DevProcessor(BaseProcessor):
        SLUG = 'dev'
        NAME = 'Dev Processor'
        CHECKOUT_TEXT = _('Checkout with the Dev Processor (no real payment)')
        TEMPLATE_NAME = 'zeitlabs_dev_processor/handoff.html'
        RETURN_VIEW = 'zeitlabs_dev_processor.views.DevReturnView'
        STATUS_VIEW = 'zeitlabs_dev_processor.views.DevStatusView'
        GATEWAY_ORDER_ID_PARAM = 'gateway_order_id'
        contract_version = 1

        def get_transaction_parameters(self, cart, request=None, use_client_side_checkout=False, **kwargs):
            base = self.get_transaction_parameters_base(cart, request)
            order = self.gateway.create_order(
                merchant_reference=base['order_reference'],
                amount=base['amount'],
                currency=base['currency'],
                user_id=cart.user_id,
                return_url=self.get_return_url(),
            )
            return {
                'gateway_order_id': order['gateway_order_id'],
                'payment_page_url': reverse('zeitlabs_dev_processor:gateway', args=[order['gateway_order_id']]),
            }

        def get_gateway_order_id(self, transaction_parameters):
            return transaction_parameters['gateway_order_id']

``self.get_return_url()`` is the address of your return view: give it to the gateway as the page to send the learner
back to.

The return view
***************

The return view is where the gateway sends the learner back. It shows the plugin's waiting page, which polls your status
view. Combine ``PaymentReturnViewMixin`` with Django's ``View`` and name your processor:

.. code-block:: python

    from django.views import View
    from zeitlabs_payments.providers.mixins import PaymentReturnViewMixin

    class DevReturnView(PaymentReturnViewMixin, View):
        processor_class = DevProcessor
        MAX_ATTEMPTS = 10   # how many times the waiting page polls
        WAIT_TIME = 3000    # milliseconds between polls

The waiting page polls 24 times, five seconds apart, unless you say otherwise; then it tells the learner the payment is
not confirmed yet. The view reads the order id from the ``GATEWAY_ORDER_ID_PARAM`` query parameter. The plugin serves
it without CSRF checks, since a gateway posting the learner back cannot carry the site's token.

The status view
***************

The status view answers each poll. Combine ``PaymentStatusViewMixin`` with ``View``, name your processor, and override
the methods that talk to your gateway. Each one's default is the answer of a gateway with nothing to say, so a method
left out fails the poll, never the payment.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Method
     - What it returns
   * - ``get_order_cart_reference(gateway_order_id)``
     - The ``order_reference`` the gateway holds for the order -- the one ``get_transaction_parameters_base`` gave you,
       which reads ``<site id>-<cart id>`` -- or ``None`` when the gateway holds no such order. This is how the plugin
       finds the cart.
   * - ``get_order_status(gateway_order_id, cart)``
     - The gateway's answer about the order, as a dict, or ``None`` when it gave none. It is kept as the evidence: on
       the transaction when the payment is recorded, in the audit trail otherwise.
   * - ``get_payment_status(data)``
     - Where that answer says the payment stands: ``PaymentStatus.SUCCESS``, ``PENDING``, ``DECLINED`` or
       ``CANCELLED``, or ``None`` for an answer you cannot read.
   * - ``verify_success_response(data, cart)``
     - Nothing, once you have checked that a success really is this cart's payment: its reference, its transaction id,
       its signature. Raise ``GatewayError`` otherwise. The default refuses every success, so a status view that does not
       check never settles anything.
   * - ``get_transaction_details(data, cart)``
     - What the payment is recorded with: ``transaction_id`` (the gateway's id for the payment), ``method``,
       ``amount``, ``currency`` and ``reason``.

Raise ``GatewayError`` from ``get_order_cart_reference`` or ``get_order_status`` when the gateway cannot be asked:
nothing changes, and the learner is told the payment is not confirmed yet. A success refused by
``verify_success_response`` is kept on the trail and the cart held for staff.

The rules
*********

The plugin relies on these. A processor that breaks them can lose track of money.

* **One single-use order per cart, recorded at hand-off.** The plugin knows a payment by its gateway order, whatever
  transaction id it is reported under -- an authorization's id, then its capture's. So each cart is handed to one
  order, and the order must stop taking payment once paid. A gateway that cannot issue such an order -- a reusable
  payment link, an invoice payable more than once -- is not supported. A success reported under an order the cart was
  not handed to is recorded but never settled, for staff to refund.
* **The order id the gateway sends the learner back with is the one recorded at hand-off.**
* **A decline or a cancellation is final.** Report ``DECLINED`` or ``CANCELLED`` only for an answer your gateway treats
  as final: they end the cart for good, and a success reported for it later is never settled. Report anything that can
  still change -- a payment held for review, one the gateway may yet complete -- as ``PENDING``, however long it lasts.
* **Record the amount the plugin asked for.** Return the cart's total as ``amount`` in ``get_transaction_details``,
  and leave the gateway's own figures in its answer. ``reconcile_payment`` is where the two are compared: a mismatch is
  recorded, marked failed, and held for staff.
* **Read your settings through** ``get_settings()``, never from ``django.conf.settings`` directly, so a site's
  configuration can override them.

Write to the audit trail
************************

Record your processor's own events on the cart's audit trail under an action of the form ``gw.<event>``:

.. code-block:: python

    from zeitlabs_payments.models import AuditLog

    AuditLog.log(
        action=f'{AuditLog.AuditActions.GATEWAY_LOG}{AuditLog.GATEWAY_LOG_SEPARATOR}order_opened',
        cart=cart,
        processor=self,
        context={'message': f'Order {gateway_order_id} opened at the gateway.'},
    )

The event name is lowercase letters, digits and underscores. See :doc:`../concepts/the-audit-log`.

Test it
*******

Install your processor beside the plugin, on a Tutor development install, and buy a course with it as in
:doc:`../quickstarts/try-it-with-the-dev-processor`. Then make your gateway's sandbox give each answer it can --
approved, declined, cancelled, abandoned, an error -- and compare what happens with the dev processor's table in
:doc:`test-payment-scenarios-with-the-dev-processor`: yours should end each one the same way.

For automated tests, run your processor inside the plugin, as the dev processor's test settings do: install the plugin
in your test environment and drive your views with Django's test client.
