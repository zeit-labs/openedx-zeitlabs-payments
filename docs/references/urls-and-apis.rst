URLs and APIs
#############

Pages
*****

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Address
     - Page
   * - ``/checkout/v1/checkout/?sku=<SKU>``
     - Checkout for one catalogue item. Open edX's checkout links lead here. Without ``sku``, it shows the learner's
       open cart.
   * - ``/payment/v1/history/``
     - The learner's payment history: their orders paid on the site it is served on that reached a gateway, newest
       first, twenty to a page. Nothing in Open edX links to it: link to it from your theme.
   * - ``/payment/v1/invoice/<invoice number>/``
     - An invoice, for the learner who paid it, for superusers, and for the employees of the site it was paid on
       granted access: see :doc:`../how-tos/give-site-employees-access`.
   * - ``/payment/v2/invoice/<token>/``
     - The public receipt the invoice's QR code leads to.

The other pages -- the hand-off, the waiting page, the decline, error and reconciliation pages -- are reached through a
purchase, not linked to.

The cart API
************

``GET /api/cart/v1/cart/``
    The learner's open cart. With none, it answers ``200`` with a ``details`` message instead.

``POST /api/cart/v1/cart/`` with ``sku``
    Opens a cart for the item, as checkout does, and answers it with ``201``. A refusal answers ``400``, with the
    reason under ``details``. When a payment for the item is still open, it answers ``409`` with ``outcome:
    payment_in_progress``, and either the ``redirect_url`` of that payment's waiting page or a ``retry_after`` time.

Both need a logged-in learner.

The history API
***************

``GET /api/payment/v1/history/<user id>/``
    Any learner's payment history, for staff, superusers and the site's employees granted access (see
    :doc:`../how-tos/give-site-employees-access`): the orders their history page lists, as JSON, twenty to a page
    (``?page=2``). Only the orders paid on the site it is served on are listed, for superusers too: read another site's
    on that site's own address. Anyone else is refused with ``403``. A user id naming nobody answers ``404`` to staff
    and superusers, and an empty history to an employee, so the ids cannot be walked to find which exist.

.. code-block:: json

    {
      "count": 21,
      "next": "https://lms.example.com/api/payment/v1/history/3/?page=2",
      "previous": null,
      "results": [
        {
          "cart_id": 42,
          "created_at": "2026-11-14T09:30:00Z",
          "status": "paid",
          "state": "paid",
          "item": {
            "type": "paid_course",
            "sku": "DATA101-VERIFIED",
            "title": "Data 101",
            "item_ref_id": "course-v1:Acme+DATA101+2026"
          },
          "amount": "450.00",
          "currency": "SAR",
          "invoice_number": "ACME-100042",
          "gateway_order_id": "8f3c2a"
        }
      ]
    }

``status`` is the cart's status (see :doc:`../concepts/cart-statuses`); ``state`` is what the learner's page labels it:
``paid``, ``confirming`` or ``refunded``. A fourth value, ``refund_requested``, is reserved for refund requests, a future
feature not implemented yet: no order carries it today. ``amount`` is the invoice's total once invoiced, what the
learner was asked to pay otherwise, with exactly its currency's decimal places.
