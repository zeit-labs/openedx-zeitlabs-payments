Customize the invoice
#####################

The plugin issues an invoice for every settled payment. It shows the issuing organization, the invoice number, the
learner, what was bought, the totals, the payment method, and a QR code leading to a public copy of the receipt.

An invoice is issued once and never changes: the organization and learner it names are stored on it when it is issued.
Changing a setting later changes the invoices issued from then on, not the ones already issued.

Where an invoice is shown
*************************

* ``/payment/v1/invoice/<invoice number>/`` -- the learner's own copy, where checkout sends them. It opens for the
  learner who paid, for superusers, and for the employees of the site it was paid on granted access
  (:doc:`give-site-employees-access`). Anyone else gets a page saying the address is private, whether or not the number
  exists. It is always shown on the site the payment was made on: the learner and superusers asking on another site are
  redirected there, and an employee gets the private page.
* ``/payment/v2/invoice/<token>/`` -- the public receipt, which the QR code leads to. The token cannot be guessed, so
  anyone holding a printed copy can check it without an account.

Name the organization
*********************

Set the organization the invoice names, and its identifiers, in ``ZEITLABS_PAYMENTS_SETTINGS``:

.. code-block:: python

    ZEITLABS_PAYMENTS_SETTINGS = {
        "organization_info": {
            "name": "Acme Academy",
            "vat_no": "300000000000003",   # printed as the VAT number, when present
            "cr_no": "1010000000",         # printed as the commercial registration number, when present
        },
        ...
    }

Any other key you add is stored on the invoice too, for a themed template to print (see below). Set it in a site's
configuration for a site that invoices as another organization.

Number the invoices
*******************

Invoice numbers read ``<prefix>-<number>``, for example ``ACME-100001``. The prefix is the ``invoice_prefix`` setting,
and is required: no invoice is issued without it.

* It is uppercase letters, digits and inner hyphens, at most 63 characters. It is uppercased for you, so ``acme`` is
  ``ACME``.
* Each prefix has its own series, starting at ``100001``. Changing the prefix starts a new series; it never renumbers
  issued invoices.

Show your logo
**************

``logo_url`` is the logo on the payment pages and the invoice. ``invoice_logo_url``, when set, replaces it on the
invoice only -- a version that prints well, for example.

Take the learner or the organization from elsewhere
***************************************************

By default an invoice names the learner by their Open edX profile name, or else their account's first and last name,
and the organization by the ``organization_info`` setting. To take either from somewhere else -- an identity provider,
a CRM -- write a function in your own plugin and name it in a setting. It is called with the default function as its
first argument, so it can build on it:

.. code-block:: python

    # my_plugin/invoice.py
    def learner_info(prev_fn, user, site):
        info = prev_fn(user, site)                      # {'name': ...}
        info['national_id'] = lookup_national_id(user)  # an extra key, for a themed template
        return info

.. code-block:: python

    # LMS settings
    OVERRIDE_ZEITLABS_PAYMENTS_LEARNER_INFO = 'my_plugin.invoice.learner_info'

.. list-table::
   :header-rows: 1
   :widths: 40 30 30

   * - Setting
     - Called with
     - Returns
   * - ``OVERRIDE_ZEITLABS_PAYMENTS_LEARNER_INFO``
     - ``prev_fn, user, site``
     - ``{'name': ...}`` and any keys of your own
   * - ``OVERRIDE_ZEITLABS_PAYMENTS_ORGANIZATION_INFO``
     - ``prev_fn, site``
     - ``{'name': ..., 'vat_no': ..., 'cr_no': ...}`` and any keys of your own
   * - ``OVERRIDE_ZEITLABS_PAYMENTS_RETIRE_LEARNER_INFO``
     - ``prev_fn, info, invoice``
     - The learner info to show for a learner who asked for their account to be retired

Each must return a dictionary that can be stored as JSON exactly as returned. If one raises or returns anything else,
the invoice is not issued: the payment stays paid without an invoice, for staff to retry once it is fixed (see
:doc:`resolve-a-payment-as-staff`). An invoice is never issued with its organization or learner missing.

Learners who retire their account
*********************************

Open edX erases a retired learner's personal data from its own records. It does not know what the plugin stored on
their invoices, so each time such an invoice is shown, its learner info goes through the retire function first. The
default hides the name. What is stored is never changed. A deployment that must keep showing the name for as long as
the law requires an invoice to be kept replaces the function, and can go by the invoice's age.

Change the layout
*****************

The invoice is the template ``zeitlabs_payments/invoice.html``. To change how it looks, restyle it from your theme as
:doc:`customize-the-theme` describes. To change what it shows -- to print an extra key you added -- copy the template
into your comprehensive theme, at ``lms/templates/zeitlabs_payments/invoice.html``, and edit the copy. It renders with:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Content
   * - ``invoice``
     - The invoice: ``invoice_number``, ``total``, ``gross_total``, its ``items`` and its ``cart``.
   * - ``organization_info``
     - What was stored for the organization when the invoice was issued.
   * - ``learner_info``
     - What was stored for the learner, passed through the retire function for a retired learner.
   * - ``payment_method``
     - The slug of the processor that took the payment.
   * - ``currency``
     - The invoice's currency.

Keep a copied template in step with the plugin's when you upgrade: a copy does not receive later changes.
