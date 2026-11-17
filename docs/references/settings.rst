Settings
########

The plugin reads its settings from the LMS settings, and from each site's configuration first: a key set in a site's
configuration (LMS admin, *Site Configuration*) overrides the LMS setting for that site. ``ZEITLABS_PAYMENTS_SETTINGS``
is read as a whole, so a site that sets it sets every key it needs.

Plugin settings
***************

``IS_ZEITLABS_PAYMENTS_ENABLED``
================================

``True`` sends Open edX's checkout links to the plugin. Default ``False``: Open edX's own checkout, if any, is used.

``ZEITLABS_PAYMENTS_SETTINGS``
==============================

A dictionary. Default ``{}``.

.. list-table::
   :header-rows: 1
   :widths: 22 13 65

   * - Key
     - Required
     - Meaning
   * - ``valid_currency``
     - Yes
     - The ISO 4217 code of the currency the deployment trades in, for example ``SAR``. Every catalogue item is priced
       in it; without a supported currency here, no catalogue item can be saved and no payment checked.
   * - ``invoice_prefix``
     - Yes
     - The prefix of invoice numbers: uppercase letters, digits and inner hyphens, at most 63 characters. Each prefix
       numbers its own series from ``100001``. Without it, no invoice is issued. See
       :doc:`../how-tos/customize-the-invoice`.
   * - ``organization_info``
     - No
     - The organization invoices are issued by: ``name``, ``vat_no`` and ``cr_no`` are printed when present; other keys
       are stored for a themed invoice template. Default ``{}``.
   * - ``support_url``
     - No
     - A support page the payment pages link to. Shown only when set.
   * - ``support_email``
     - No
     - A support address the payment pages offer. Shown only when set.
   * - ``logo_url``
     - No
     - The logo on the payment pages and the invoice.
   * - ``invoice_logo_url``
     - No
     - A logo for the invoice only, replacing ``logo_url`` there.

``LMS_ROOT_URL``
================

Open edX's own setting. The plugin builds its checkout links on it.

Customization hooks
*******************

Each names a function of your own, by dotted path, replacing the plugin's default. See
:doc:`../how-tos/customize-the-invoice`.

* ``OVERRIDE_ZEITLABS_PAYMENTS_LEARNER_INFO``: what an invoice says about the learner.
* ``OVERRIDE_ZEITLABS_PAYMENTS_ORGANIZATION_INFO``: what an invoice says about the issuing organization.
* ``OVERRIDE_ZEITLABS_PAYMENTS_RETIRE_LEARNER_INFO``: what an invoice shows of a learner who asked for their account to
  be retired.

Open edX commerce configuration
*******************************

Not a setting but a record in the LMS admin, under *Commerce > Commerce configurations*. Open edX shows its buy and
upgrade buttons only while the current configuration has:

* **Enabled** ticked;
* **Checkout on ecommerce service** ticked.

The plugin takes the ecommerce service's place, so both are needed. Its other fields, such as the basket checkout page,
are not read while ``IS_ZEITLABS_PAYMENTS_ENABLED`` is on.

Course modes
************

Each paid course mode sold needs a **SKU**, set in the LMS admin under *Course Modes*, matching a catalogue item's SKU.
See :doc:`../how-tos/sell-a-course-or-a-bundle`.

Waffle flags and switches
*************************

``zeitlabs_dev_processor.enabled``
    A waffle flag of the dev processor: the users it is active for are offered it at checkout. See
    :doc:`../how-tos/test-payment-scenarios-with-the-dev-processor`.

Processor settings
******************

Each processor reads its own configuration, from the setting it names in ``SETTINGS_ROOT_KEY``, and documents it
itself. Like the plugin's, a site's configuration overrides it.
