Open edX Payments Extension by Zeitlabs
#######################################

A pluggable Django/Python plugin for Open edX that simplifies payment integration.

Purpose
*******

A lightweight, Django/Python-based, pluggable payment framework for Open edX as an edx-platform plugin.

Comparision to Existing Solutions
*********************************

There's more than one option for payment in Open edX. Most have their own flaws, and this is a quick comparision of why this is the most-viable solution:

.. list-table:: Open edX Payment Solutions Comparison
   :header-rows: 1
   :widths: auto

   * - **Feature**
     - **Zeitlabs Payments**
     - **Openedx/ecommerce**
     - **WooCommerce**
     - **Saleor**
   * - URL
     - https://github.com/zeit-labs/openedx-zeitlabs-payments/
       Supported for all Open edX releases starting from Redwood
     - https://github.com/openedx/unsupported/ecommerce (Deprecated)
     - https://github.com/openedx/openedx-wordpress-ecommerce
       Not supported yet. Proof of concept ready.
     - https://saleor.io/
   * - Support
     - Supported for all Open edX releases starting from Redwood
     - Deprecated
     - Supported (from ??)
     - Not supported yet
   * - Technology
     - Python, Django
     - Python, Django
     - PHP and Python
     - Python
   * - Integration
     - edx-platform plugin
     - Micro-service
     - Plugin, and WordPress Micro-service
     - (empty in source)
   * - Type of Solution
     - Payments Feature
     - Full ecommerce solution, modified for Open edX
     - Full ecommerce solution, modified for Open edX
     - Full ecommerce solution, modified for Open edX
   * - Multi-tenancy model
     - Standard Open edX multi-tenancy using SiteConfiguration or eox_tenant
     - Customized, different Open edX multi-tenancy (Oscar Commerce)
     - ?
     - Customized, different Open edX multi-tenancy (Oscar Commerce)
   * - Small Instance
     - Suitable via Tutor plugin, ready
     - N/A
     - Needs PHP expertise
     - Needs Saleor expertise
   * - Ideal User
     - Most Open edX installations
     - Only suitable for 2U / edX.org from now on
     - Established WooCommerce users with in-house PHP expertise
     - Established Saleor users
   * - Ease of Install
     - High
     - Medium
     - Medium to Low
     - Low
   * - Recurring Subscriptions
     - Planned feature
     - Not available
     - (empty)
     - (empty)
   * - Solution Complexity
     - Low: Focused, simple
     - High: Complex shipping & unused components
     - High: Complex WordPress, ecommerce shipping, etc.
     - High: Complex ecommerce, shipping, etc.
   * - Admin Experience
     - High: Centralized in /admin panels
     - Low: Fractured across multiple /admin panels
     - Low: Fractured across multiple systems
     - Low: Fractured across multiple systems
   * - Purchase Experience
     - High: Smooth payment flow
     - High: Smooth payment flow
     - High: Smooth payment flow
     - High: Smooth payment flow
   * - Make a Course Paid UX
     - High: Single click & configure
     - Low: Fractured across /admin panels
     - Low: Fractured across /admin panels
     - Low: Fractured across multiple systems
   * - Payment Processors Support
     - New system, processors must migrate from ecommerce
     - Deprecated
     - High
     - ??
   * - Cost of Installation / Operation
     - Free / Open Source, works out of the box
     - Deprecated
     - Additional WordPress hosting fees needed. Free otherwise
     - Additional Saleor hosting fees needed. Free otherwise
   * - Transaction Fees
     - None. Free / Open Source. No transaction fees
     - None. Free / Open Source. No transaction fees
     - None. Free / Open Source. No transaction fees
     - None. Free / Open Source. No transaction fees


What Open edX requires is not a full eCommerce store, but reliable and straightforward payment flows -- something simple enough for a the admin to make a course purchasable, yet flexible enough to scale for larger institutional deployments.

This plugin brings a simpler, smarter, and more maintainable approach to Open edX payments -- one that works today and grows with tomorrow’s needs.

Features
********

- **Seamless Integration** – Built with Django/Python, feels native to Open edX.
- **Lightweight & Cost-Effective** – No extra servers or infrastructure overhead.
- **Plug-and-Play Setup** – Fast integration, easy to manage via the admin panel.
- **Familiar User Experience** – Checkout flow consistent with Open edX’s deprecated eCommerce.
- **Future-Ready** – Starting with Payfort support, with more gateways, discounts, and coupon features on the roadmap.
- **Tailored for Open edX:** Supports course purchase, program purchase and subscriptions in addition to custom purchase models due to its native integration with the edx-platform.
- **No Burden** – No need to maintain another server, or have your team learn new technologies.

Getting Started with Development
********************************

Clone repo
==========

.. code-block:: bash

   git clone git@github.com:zeit-labs/openedx-zeitlabs-payments.git


Add the app to the Open edX platform in Tutor development mode
==============================================================

1. Create (or open existing) ``docker-compose.override.yml`` file:

.. code-block:: bash

   vi "$(tutor config printroot)/env/dev/docker-compose.override.yml"

2. Add the volume mapping so your project will be available inside the container without the need to rebuild it:

.. code-block:: yaml

   version: "3.7"
   services:
     lms:
       volumes:
       - <YOUR-CLONED-DIRECTORY-PATH>:/openedx/requirements/openedx-zeitlabs-payments/

3. Restart containers with the new volume:

.. code-block:: bash

   tutor dev stop;
   tutor dev start -d;


4. Install the app:

.. code-block:: bash

   tutor dev exec lms bash
   cd /openedx/requirements/openedx-zeitlabs-payments && pip install -e .

5. Restart the container:

.. code-block:: bash

   tutor dev restart lms

   # That’s it for the development version. Once you make any changes to your code,
   # the LMS server will be automatically restarted and your changes applied.
   # The URL patterns you specify in your urls.py will also work.

6. How to run migrations:

.. code-block:: bash

   tutor dev exec lms bash
   python manage.py lms migrate zeitlabs_payments

7. Verify Installation

Go to the Django admin panel and ensure that the **ZeitLabs Payments** tables are listed there.


Deploying
*********

To enable the plugin in your Tutor deployment, add the following to your
``$(tutor config printroot)``/config.yml:

.. code-block:: yaml

    OPENEDX_EXTRA_PIP_REQUIREMENTS:
      - git+https://github.com/zeit-labs/openedx-zeitlabs-payments.git


How to use:
***********

Set following config settings
=============================

The following settings need to be added in ``config.yml``:

.. code-block:: yaml

   PAYFORT_SETTINGS = {
       'access_code': '<YOUR-ACCESS-CODE>',
       'request_sha_phrase': '<YOUR-REQUEST-SHA-PHRASE>',
       'response_sha_phrase': '<YOUR-RESPONSE-SHA-PHRASE>',
       'sha_method': 'SHA-256',
       'redirect_url': 'https://sbcheckout.payfort.com/FortAPI/paymentPage'
   }

   ECOMMERCE_PUBLIC_URL_ROOT = <YOUR-LMS-URL>  # e.g. 'https://zeit.labs.io:8000/'
   # Used if LMS_ROOT_URL is not set in site configuration.

   INVOICE_PREFIX = <YOUR-INVOICE-PREFIX>  # e.g. 'DEV'
   # Default invoice prefix (used for invoice numbers). Can also be set
   # per-site in site configurations.

   ORGANIZATION = <YOUR-ORGANIZATION-NAME>
   # This will be shown on invoices.

   CUSTOMER_NUMBER = <YOUR-CUSTOMER-NUMBER>
   # This will be shown on invoices.

   IS_ZEITLABS_PAYMENTS_ENABLED = True
   # Default flag to enable/disable the plugin. Can also be controlled
   # per-site from site configuration.


Convert course to a Paid Course
===============================

1. Create an **edX course mode** and set SKU and price  from django Admin.
2. Create a **Catalogue Item** in ``zeitlabs_payments`` with the same SKU and price, and add the course ID as ``item_ref_id``.
3. Try to **enroll or upgrade** a course. It should redirect to the checkout page where the learner can see the **Pay with Payfort** button.
4. After payment, the user will be redirected back to the platform’s **invoice page**, and their enrollment status will be updated.
5. Check the **Audit Logs** table in case of failures or to review responses from the payment gateway.


Run Tests
*********

1. Create and activate a virtual environment with Python **3.11.0**.

2. Install ``tox``:

   .. code-block:: bash

      pip install tox==3.28.0

3. Run Quality tests:

   .. code-block:: bash

      tox -e quality

4. Run Unit tests:

   .. code-block:: bash

      tox -e py311-django42

Make Migrations
***************

If you make changes to the models, you can create new migrations with the following command:

.. code-block:: bash

   tox -e makemigrations

Extending Zeitlabs Payments
***************************

Zeitlabs Payments is designed to be **pluggable and extensible**.
Adding a new payment processor or gateway is straightforward, thanks to the built-in `BaseProcessor` class.

The `BaseProcessor` already includes all the core functionality you need:
- Cart handling
- Payment flow
- Invoice creation
- Integration hooks with the Open edX platform

All you need to do is:
1. Create an Open edX Django plugin for your processor.
2. Extend the `BaseProcessor` and override the required methods.
3. Add your processor to the entry points, for example:

   .. code-block:: python

       entry_points={
           'lms.djangoapp': [
               'payfort = payfort.apps:PayfortConfig',
           ],
           'zeitlabs_payments.v1': [
               'payfort = payfort.processor:PayFort',
           ]
       }

4. (Optional) Add any custom views your processor may require (e.g., redirects, webhooks).

Zeitlabs Payments will automatically discover and load all processors defined under the
``zeitlabs_payments.v1`` entry point, making them instantly available in the system.

Reference Implementation
========================

We have already built a working PayFort plugin, which you can use as a reference to create your own gateway:
https://github.com/zeit-labs/zeitlabs-payfort

Use it as a **starting point** when building your own payment gateway integration.

Status
******

In active development, currently being deployed on our first cluster.
Test it and provide your feedback.

License
*******

The code in this repository is licensed under the **Apache Software License 2.0**.
