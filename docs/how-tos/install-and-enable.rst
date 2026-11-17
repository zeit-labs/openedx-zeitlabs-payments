Install and enable
##################

The plugin supports the Open edX releases from Redwood to Verawood. It is installed into the LMS, and needs at least one
payment processor installed beside it: the plugin takes no payment by itself.

Install
*******

With Tutor, add the plugin and your processor to ``config.yml``, then rebuild the image and run the migrations:

.. code-block:: yaml

    OPENEDX_EXTRA_PIP_REQUIREMENTS:
      - git+https://github.com/zeit-labs/openedx-zeitlabs-payments.git
      - <your payment processor>

.. code-block:: bash

    tutor images build openedx
    tutor local launch

A processor registers itself when the LMS starts. Check *Zeitlabs_Payments > Payment processors* in the LMS admin: a
processor missing from that list did not register, and the LMS log says why (see :doc:`troubleshoot`).

Configure
*********

The plugin reads two settings, from the LMS settings or from a site's configuration:

* ``IS_ZEITLABS_PAYMENTS_ENABLED``: ``True`` to send Open edX's checkout to the plugin. Off by default.
* ``ZEITLABS_PAYMENTS_SETTINGS``: the deployment's currency, its invoice number prefix, the organization its invoices
  name, and the support links and logos its pages show.

With Tutor, set them through an inline plugin patching ``openedx-lms-common-settings``:

.. code-block:: yaml

    name: zeitlabs_payments
    version: 1.0.0
    patches:
      openedx-lms-common-settings: |
        IS_ZEITLABS_PAYMENTS_ENABLED = True
        ZEITLABS_PAYMENTS_SETTINGS = {
            "valid_currency": "SAR",
            "invoice_prefix": "ACME",
            "organization_info": {"name": "Acme Academy", "vat_no": "300000000000003"},
            "support_url": "https://acme.example.com/support",
            "support_email": "support@acme.example.com",
            "logo_url": "https://acme.example.com/logo.svg",
        }

``valid_currency`` and ``invoice_prefix`` are required: without them no catalogue item can be saved and no invoice
issued. :doc:`../references/settings` explains every key, and how a site's configuration overrides them.

Switch on Open edX's checkout
*****************************

Open edX shows its buy and upgrade buttons only while its commerce configuration says checkout happens on an ecommerce
service. The plugin takes that service's place: with ``IS_ZEITLABS_PAYMENTS_ENABLED`` on, every checkout link Open edX
builds leads to the plugin's checkout page instead. So the commerce configuration still has to be switched on.

In the LMS admin, under *Commerce > Commerce configurations*, add a configuration with:

* **Enabled**: ticked;
* **Checkout on ecommerce service**: ticked.

The other fields, such as the basket page, are not read once the plugin is enabled.

Check it
********

#. Put a course on sale: :doc:`sell-a-course-or-a-bundle`.
#. As a learner, choose the course's paid track. You should land on the plugin's checkout page, at
   ``/checkout/v1/checkout/?sku=<SKU>``, listing the processors offered to you.

To try a whole purchase without a real gateway, install the dev processor: :doc:`test-payment-scenarios-with-the-dev-processor`.
