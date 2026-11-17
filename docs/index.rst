ZeitLabs Payments
#################

ZeitLabs Payments is an Open edX plugin that lets learners buy courses and course bundles inside the LMS, with no
separate ecommerce service to run. It runs checkout, issues invoices, enrolls learners and gives staff the tools to
resolve payments that did not end cleanly. Taking the money is left to a **payment processor**: a separate plugin that
connects one payment gateway to it.

These pages are for the engineers who install and run the plugin, test it, or write a payment processor for it.

* New here? :doc:`quickstarts/try-it-with-the-dev-processor` takes you through one purchase in about fifteen minutes.
* Running it: :doc:`how-tos/install-and-enable`, :doc:`how-tos/sell-a-course-or-a-bundle`,
  :doc:`how-tos/resolve-a-payment-as-staff` and :doc:`how-tos/troubleshoot`.
* Writing a processor: :doc:`how-tos/build-a-payment-processor`.
* Looking something up: :doc:`references/settings`, :doc:`references/admin-pages` and :doc:`references/urls-and-apis`.

.. toctree::
   :maxdepth: 2

   quickstarts/index
   how-tos/index
   concepts/index
   references/index
