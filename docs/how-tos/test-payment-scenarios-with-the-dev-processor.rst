Test payment scenarios with the dev processor
#############################################

The `dev processor`_ is a payment processor that charges nothing, for developing, testing and demonstrating the
plugin. It plugs in like any other processor: checkout offers it, it sends the learner to a payment page, and it brings
them back to the plugin's waiting page while the plugin settles the payment. Only the gateway is a stand-in: instead of
paying, the learner picks how the gateway answers, and the plugin handles that answer exactly as it handles a real one.

It is also the reference for :doc:`build-a-payment-processor`: everything a processor needs, and nothing more.

.. _dev processor: https://github.com/zeit-labs/zeitlabs-dev-processor

Install it
**********

Add it beside the plugin in your Tutor ``config.yml``, then rebuild the image and restart:

.. code-block:: yaml

    OPENEDX_EXTRA_PIP_REQUIREMENTS:
      - git+https://github.com/zeit-labs/openedx-zeitlabs-payments.git
      - git+https://github.com/zeit-labs/zeitlabs-dev-processor.git

It needs no settings of its own.

Enable it for testers
*********************

The processor is behind the ``zeitlabs_dev_processor.enabled`` waffle flag, off by default. Until the flag is active for
a user, checkout does not offer the processor to them and its pages answer 404.

.. warning::

   Anyone the flag is active for can enroll in paid courses without paying. Activate it for testers only, never for
   everyone on a production site.

In the LMS admin, under *Django-Waffle > Flags*, add the flag with *Everyone* set to *Unknown*, untick *Superusers*,
and add the testers under *Users*, or a Django group of them under *Groups*. Or, from the LMS container:

.. code-block:: bash

    ./manage.py lms waffle_flag zeitlabs_dev_processor.enabled --create --append --user alice --user bob@example.com

Without ``--append``, ``--user`` replaces the users the flag already has.

Pick an answer
**************

After checkout, the processor's payment page shows the order and the answers a gateway can give. Each answer below
lists what the learner then sees, where the cart ends, and what the cart's audit trail shows beyond the hand-off. See
:doc:`../concepts/cart-statuses` for the statuses and :doc:`../concepts/the-audit-log` for the entries.

.. list-table::
   :header-rows: 1
   :widths: 20 30 15 35

   * - Answer
     - The learner sees
     - Cart
     - Records
   * - **Approved**
     - The invoice; the course is in their dashboard.
     - paid
     - A successful transaction; ``user_enrolled`` (or ``user_upgraded``), ``cart_fulfilled``.
   * - **Refused**, **Insufficient funds**
     - The decline page: nothing was charged, and *Try again* starts a new cart.
     - cancelled
     - ``payment_declined``, with the gateway's answer.
   * - **Cancelled**
     - The same page, worded for a payment they cancelled.
     - cancelled
     - ``payment_cancelled``, with the gateway's answer.
   * - **Timeout**
     - The waiting page, then *Payment not yet confirmed*: do not pay again.
     - payment pending
     - Nothing more: a pending answer is not recorded.
   * - **Gateway error**
     - *Payment not yet confirmed*.
     - processing
     - Nothing: the gateway could not be asked.
   * - **Approved after a delay**
     - The waiting page for ten seconds, then the invoice.
     - paid
     - As for *Approved*.
   * - **Approved for another amount**
     - *Payment received*, but the enrollment was not completed: contact support.
     - unreconciled
     - A failed transaction for the amount asked for, the gateway's figure in its answer and the reason;
       ``transaction_not_reconciled``, ``payment_unreconciled``.
   * - **Unknown status**
     - *Payment not yet confirmed*.
     - payment pending
     - ``gateway_response_rejected``, with the answer it could not read.

The waiting page asks for the status every three seconds and gives up after thirty, so a timeout ends within half a
minute. Coming back to buy the same course afterwards sends the learner back to the waiting page of the payment still
open, rather than letting them pay twice. Answering the order on the payment page then settles it.

The *Approved for another amount* cart is how to try staff resolution: see :doc:`resolve-a-payment-as-staff`.

The processor's own trail entries
*********************************

The processor writes ``gw.order_opened`` when it opens the order at the gateway. The stand-in gateway writes
``gw.answer_picked`` with the answer picked, so the trail shows it even for a timeout or a gateway error, which the
processor records nothing of. That second entry is a test aid: with a real gateway, only the processor writes to the
trail.

Limits
******

* The gateway keeps an order for 24 hours. An order left unanswered longer than that can no longer be answered or
  looked up, and its cart keeps blocking the course for that learner. Use another learner or another course.
* There is no refund at the gateway: to try recording a refund, follow :doc:`resolve-a-payment-as-staff` as if the
  refund had been made.
