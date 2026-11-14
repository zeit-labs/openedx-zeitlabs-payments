Try it with the dev processor
#############################

This walks you through one purchase on a Tutor development or local install, from adding the plugin to opening the
invoice. It uses the **dev processor**, a payment processor that charges nothing: instead of paying, you pick how its
stand-in gateway answers. Everything after that answer is the plugin's own, exactly as with a real gateway.

You need a running Tutor install, a superuser account, and one course you can edit.

1. Install the plugin and the dev processor
*******************************************

Add both to your Tutor ``config.yml``:

.. code-block:: yaml

    OPENEDX_EXTRA_PIP_REQUIREMENTS:
      - git+https://github.com/zeit-labs/openedx-zeitlabs-payments.git
      - git+https://github.com/zeit-labs/zeitlabs-dev-processor.git

Then add the settings the plugin needs, through a Tutor inline plugin. Save this as
``$(tutor plugins printroot)/zeitlabs_payments.yml``:

.. code-block:: yaml

    name: zeitlabs_payments
    version: 1.0.0
    patches:
      openedx-lms-common-settings: |
        IS_ZEITLABS_PAYMENTS_ENABLED = True
        ZEITLABS_PAYMENTS_SETTINGS = {
            "valid_currency": "USD",
            "invoice_prefix": "DEV",
            "organization_info": {"name": "My Academy"},
        }

Enable it, rebuild the image and launch:

.. code-block:: bash

    tutor plugins enable zeitlabs_payments
    tutor images build openedx
    tutor local launch

Every setting is explained in :doc:`../references/settings`.

2. Switch on checkout in Open edX
*********************************

Open edX shows its buy and upgrade buttons only while its commerce configuration says checkout happens on an ecommerce
service. The plugin takes the place of that service, so in the LMS admin, under *Commerce > Commerce configurations*,
add a configuration with **Enabled** and **Checkout on ecommerce service** ticked.

3. Let yourself use the dev processor
*************************************

The dev processor is offered only to the users its waffle flag is active for, since anyone it is offered to enrolls for
free. Activate it for your own user:

.. code-block:: bash

    tutor local run lms ./manage.py lms waffle_flag zeitlabs_dev_processor.enabled --create --append --user <your-username>

4. Put a course on sale
***********************

The LMS sells a course mode; the plugin sells a catalogue item. The two are joined by the SKU.

#. In the LMS admin, under *Course Modes*, add a **verified** mode to your course, with a price and a SKU of your
   choice, for example ``VERIFIED-DEMO``.
#. Under *Zeitlabs_Payments > Catalogue items*, add an item with:

   * **SKU**: the same ``VERIFIED-DEMO``;
   * **Type**: *paid course*;
   * **Item ref id**: the course id, for example ``course-v1:Demo+DEMO+2026``;
   * **Price**: what the learner pays;
   * **Currency**: ``USD``, the ``valid_currency`` you configured.

5. Buy it
*********

Log in as a learner with the waffle flag active and choose the verified track of the course: from its track selection
page after enrolling, or from the *Upgrade* button on the learner dashboard.

#. Checkout shows the course, its price and a *Checkout with the Dev Processor (no real payment)* button. Click it.
#. The dev processor's payment page shows the order and the answers a gateway can give. Pick **Approved**.
#. You land on the waiting page, which asks for the payment's status, then sends you to the invoice.
#. The course is in your dashboard, in the verified mode.

6. Look behind the scenes
*************************

In the LMS admin, under *Zeitlabs_Payments*:

* **Carts** shows your cart as *paid*, with a link to its invoice and to its audit trail.
* **Audit logs** filtered to that cart shows every step: the hand-off, the order the dev processor opened, the answer
  you picked, the payment recorded, the enrollment and the fulfilment.
* **Transactions** shows the payment, with the gateway's whole answer.

Next
****

* Try the other answers -- a decline, a timeout, an approval for the wrong amount -- with
  :doc:`../how-tos/test-payment-scenarios-with-the-dev-processor`.
* Read :doc:`../concepts/a-purchase-start-to-finish` for what the learner sees at each step.
