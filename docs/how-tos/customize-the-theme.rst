.. _chapter-theme-override:

Customize the theme
###################

Payment pages use Paragon ``--pgn-*`` CSS variables, with fallback values when
Paragon is not loaded. To style them, override these variables in your theme
or target the component classes listed below.

Shared styles are defined in
``zeitlabs_payments/templates/zeitlabs_payments/styles.html``.

Scope
*****

These learner pages share ``zeitlabs_payments/base.html``:

* Checkout - ``zeitlabs_payments/checkout.html``
* Invalid cart - ``zeitlabs_payments/invalid_cart.html``
* Payment not started - ``zeitlabs_payments/payment_refused.html``
* Payment in progress - ``zeitlabs_payments/payment_in_progress.html``
* Payment error - ``zeitlabs_payments/payment_error.html``
* Payment decline - ``zeitlabs_payments/payment_decline.html``
* Payment reconciliation - ``zeitlabs_payments/payment_reconciliation.html``
* Invoice / receipt - ``zeitlabs_payments/invoice.html`` (private and public
  routes)
* Invoice gone (410) - ``zeitlabs_payments/invoice_private.html``
* Payment history - ``zeitlabs_payments/order_history.html``

Shared page skeleton: ``zeitlabs_payments/base.html``; shared status-page
layout: ``zeitlabs_payments/status_page.html``; shared stylesheet include:
``zeitlabs_payments/styles.html``.

This reference does not cover ``wait_feedback.html``, Django admin templates,
or gateway pages owned by processor packages. To change what the invoice says
rather than how it looks, see :doc:`customize-the-invoice`.

How theming works
*****************

1. Expose Paragon's core and theme variables on ``:root`` in LMS Django pages.
   Include the abstraction variables as well: shadows use complete
   ``--pgn-elevation-box-shadow-level-*`` values.
2. Payment components read those variables directly, with local fallbacks.
   Derived values use ``calc()`` or ``color-mix()`` at the consuming element.
3. For payment-only overrides, scope Paragon variables to ``.basket``,
   ``.status-page``, ``.receipt-wrapper`` or ``.order-history``, or restyle a
   documented component class. Variables inherit into each wrapper's descendants.
4. Keep overrides in your theme stylesheet rather than editing the plugin's
   shared stylesheet.

CTAs and links use the Paragon **primary** family (not ``brand``).

.. note::

   Classic LMS Django pages do not always load Paragon CSS. Your comprehensive
   theme (or equivalent) must expose the desired variables on ``:root`` or an
   ancestor of the payment components. Missing variables use the fallbacks
   listed below.

Template inheritance and blocks
*******************************

Template inheritance::

    main_django.html
      zeitlabs_payments/base.html
        zeitlabs_payments/status_page.html  (status pages only)

Platform blocks the skeleton fills (from ``main_django.html``):

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Block
     - Role
   * - ``bodyclass``
     - Sets ``zeitlabs-payment``. Use ``body.zeitlabs-payment`` to scope
       payment-page styles
   * - ``headextra``
     - ``base.html`` includes ``zeitlabs_payments/styles.html`` here, then yields ``page_head``
   * - ``body``
     - ``base.html`` renders the page wrapper (see below), then yields the region blocks
   * - ``title``
     - Document title; filled by pages that need one (invoice)

Region blocks provided by ``zeitlabs_payments/base.html``:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Block
     - Role
   * - ``page_class``
     - Root wrapper class: ``basket`` (checkout), ``status-page`` (status
       pages), ``receipt-wrapper`` (invoice), ``order-history`` (payment
       history). The wrapper also carries the
       ``dir`` attribute (``rtl``/``ltr``), computed once from
       ``LANGUAGE_BIDI``.
   * - ``page_head``
     - Extra ``<head>`` content per page (e.g. a viewport ``<meta>``)
   * - ``page_header``
     - Content above the main region, inside the wrapper
   * - ``page_content``
     - Main page content
   * - ``page_support``
     - Shared support region. By default renders a ``.help-section`` with a
       ``support_url`` link and/or ``support_email`` mailto, only when the
       site configuration provides them; empty otherwise
   * - ``page_footer``
     - Content below the main region, inside the wrapper
   * - ``page_scripts``
     - Inline scripts, outside the ``dir`` wrapper

Additional blocks provided by ``zeitlabs_payments/status_page.html`` (invalid
cart, payment not started, payment in progress, payment error, payment decline,
payment reconciliation):

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Block
     - Role
   * - ``status_icon_class``
     - Status icon modifier: ``success``, ``error``, or ``warning``
   * - ``status_icon``
     - Icon markup (SVG) inside ``.status-icon``
   * - ``status_title``
     - Heading inside ``.status-title``
   * - ``status_content``
     - Message body between the title and the reference/actions
   * - ``status_actions``
     - Buttons/links inside ``.status-actions``

When the view provides ``merchant_reference``, the status layout also renders
``.status-reference`` automatically, containing the translated "Reference:"
label and the reference value as plain text.

Paragon variables
*****************

Source of truth: the component declarations and responsive rules in
``zeitlabs_payments/templates/zeitlabs_payments/styles.html``. The plugin does
not declare its own theme variables or assign values to Paragon variables.
Each declaration reads the inherited Paragon value and supplies a local
fallback when that variable is absent.

Typography
==========

.. list-table::
   :header-rows: 1
   :widths: 35 40 25

   * - Variable
     - Use
     - Without Paragon
   * - ``--pgn-typography-font-family-base``
     - Base font family; falls back through
       ``--pgn-typography-font-family-sans-serif``
     - System sans-serif stack, including Arabic fallback
   * - ``--pgn-typography-font-family-sans-serif``
     - Base font family when ``font-family-base`` is absent
     - System sans-serif stack, including Arabic fallback
   * - ``--pgn-typography-headings-font-family``
     - Heading font family
     - Base font family and its fallbacks
   * - ``--pgn-typography-font-family-monospace``
     - Course IDs and payment references
     - System monospace stack
   * - ``--pgn-typography-font-size-base``
     - Base size for text and derived heading sizes
     - ``1rem``

Text sizes multiply the base size by ``0.75``, ``0.875``, ``1``, ``1.125``,
``1.25``, ``1.5``, or ``2``.

At viewport widths up to and including ``768px``, responsive heading rules
reduce the ``2`` multiplier to ``1.875``, ``1.5`` to ``1.25``, and ``1.25`` to
``1.125``. Component-specific size overrides still apply.

Neutrals and surfaces
=====================

.. list-table::
   :header-rows: 1
   :widths: 35 40 25

   * - Variable
     - Use
     - Fallback
   * - ``--pgn-color-gray-900``
     - Text, headings, and bundle headers
     - ``#212529``
   * - ``--pgn-color-gray-800``
     - Control text and amounts
     - ``#333333``
   * - ``--pgn-color-gray-500``
     - Muted text
     - ``#707070``
   * - ``--pgn-color-gray-200``
     - Borders
     - ``#cccccc``
   * - ``--pgn-color-white``
     - Surfaces, primary foregrounds, and receipt backgrounds
     - ``#ffffff``
   * - ``--pgn-color-light-200``
     - Subtle surfaces
     - ``#f8f7f6``
   * - ``--pgn-color-light-100``
     - Raised surfaces
     - ``#fdfdfd``
   * - ``--pgn-color-info-100``
     - Informational and focus surfaces
     - ``#f0f6fa``

Primary, status, and focus
==========================

.. list-table::
   :header-rows: 1
   :widths: 35 40 25

   * - Variable
     - Use
     - Fallback
   * - ``--pgn-color-primary-500``
     - Primary CTAs, links, and accents
     - ``#0a3055``
   * - ``--pgn-color-primary-600``
     - Primary hover states
     - ``#092b4d``
   * - ``--pgn-color-success-500``
     - Success foregrounds
     - ``#178253``
   * - ``--pgn-color-success-100``
     - Success surfaces
     - ``#f1f8f5``
   * - ``--pgn-color-warning-500``
     - Warning foregrounds
     - ``#ffd900``
   * - ``--pgn-color-warning-100``
     - Warning surfaces
     - ``#fffdf0``
   * - ``--pgn-color-danger-500``
     - Error foregrounds
     - ``#c32d3a``
   * - ``--pgn-color-danger-100``
     - Error surfaces
     - ``#fbf2f3``
   * - ``--pgn-color-info-500``
     - Focus rings
     - ``#006daa``

Overlays use ``color-mix(in srgb, ...)``: white at ``20%`` or ``85%`` with
transparent for foreground overlays, and primary at ``10%`` or ``40%`` with
transparent for primary overlays. Both follow their inherited Paragon colors.

Spacing, radius, shadow, motion
===============================

.. list-table::
   :header-rows: 1
   :widths: 35 40 25

   * - Variable
     - Use
     - Fallback
   * - ``--pgn-spacing-spacer-base``
     - Base spacing unit
     - ``1rem``
   * - ``--pgn-size-border-radius-sm``
     - Small corners
     - ``0.25rem``
   * - ``--pgn-size-border-radius-base``
     - Standard corners; doubled for large cards
     - ``0.375rem``
   * - ``--pgn-size-border-radius-lg``
     - Larger corners
     - ``0.425rem``
   * - ``--pgn-size-rounded-pill``
     - Pill and circular shapes
     - ``50rem``
   * - ``--pgn-elevation-box-shadow-level-1``
     - Small elevation
     - ``0 1px 2px 1px rgba(167, 164, 164, 0.25)``
   * - ``--pgn-elevation-box-shadow-level-4``
     - Large elevation
     - ``0 10px 15px -3px rgba(0, 0, 0, 0.1),
       0 4px 6px -2px rgba(0, 0, 0, 0.05)``
   * - ``--pgn-transition-base-duration``
     - Base transition duration
     - ``250ms``
   * - ``--pgn-transition-base-timing-function``
     - Transition easing
     - ``ease``

Spacing multiplies the base unit by ``0.25``, ``0.5``, ``1``, ``1.5``, ``2``,
``3``, or ``4``. Large card corners use twice the base radius (``0.75rem``
without Paragon).

Transitions combine the timing function with the base duration, multiplied by
``0.6`` for fast transitions and ``1.2`` for slow transitions.

Receipt print colors
====================

Print colors derive only from ``--pgn-color-black`` (fallback ``#000000``) and
``--pgn-color-white`` (fallback ``#ffffff``), not brand or primary colors.
Overriding those anchors intentionally changes printed output; check contrast
and QR readability when doing so.

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Use
     - Formula
     - Without Paragon
   * - Text
     - Paragon black
     - ``#000000``
   * - Background
     - Paragon white
     - ``#ffffff``
   * - Secondary text
     - Black at ``80%`` with white
     - ``#333333``
   * - Muted text
     - Black at ``60%`` with white
     - ``#666666``
   * - Faint text
     - Black at ``40%`` with white
     - ``#999999``
   * - Borders
     - Black at ``13.333333%`` with white
     - Approximately ``#dddddd``
   * - Surfaces
     - Black at ``4%`` with white
     - Approximately ``#f5f5f5``
   * - Strong surfaces
     - Black at ``9%`` with white
     - Approximately ``#e8e8e8``

The six derived shades use ``color-mix(in srgb, ...)`` at each consuming
element. The QR image itself remains black and white; theme anchors do not
recolor it.

Component classes
*****************

Scope class overrides to ``body.zeitlabs-payment`` or a page wrapper to avoid
changing similarly named elements elsewhere in the LMS.

Shared skeleton
===============

* Page wrapper class from ``page_class``: ``.basket``, ``.status-page``,
  ``.receipt-wrapper`` or ``.order-history``; carries ``dir="rtl"`` / ``dir="ltr"`` — target
  ``[dir="rtl"]`` descendants for RTL-specific overrides (the sheet itself
  uses logical properties)
* ``.help-section`` — the shared support region rendered by ``page_support``

Checkout (``.basket``)
======================

Root: ``.basket`` → ``.container`` → ``.basket-card``.

* ``.basket-header``, ``.basket-body``, ``.basket-items``
* ``.basket-item``, ``.basket-item--bundle-header``, ``.basket-item--bundle-course``
* ``.basket-item-image``, ``.basket-item-details``, ``.basket-item-title``,
  ``.basket-item-description``, ``.basket-item-price``, ``.price``,
  ``.course-id``, ``.badge-program``
* ``.basket-totals``, ``.totals-row``, ``.totals-label``, ``.totals-value``
* ``.payment-buttons``, ``.payment-button``
* ``.help-section``
* Empty cart: ``.status-card.status-card--centered`` with ``.status-icon``,
  ``.status-title``, ``.status-message``, ``.status-actions``

Status pages (``.status-page``)
===============================
Used by invalid cart, payment not started, payment in progress, payment error,
payment decline, and payment reconciliation. The invoice-gone (410) page (``invoice_private.html``)
extends ``base.html`` directly and renders only a subset: ``.status-page``,
``.status-card``, ``.status-title``, ``.status-message`` — no icon, reference,
or actions.

* ``.status-page`` → ``.status-card``
* ``.status-icon`` with modifier ``.success``, ``.error``, or ``.warning``
* ``.status-title``, ``.status-message``, ``.status-reference``,
  ``.status-actions``
* Shared buttons: ``.btn``, ``.btn-primary``, ``.btn-outline``, ``.btn-lg``
* ``.go-to-dashboard-button`` (in progress, reconciliation)

Invoice (``.receipt-wrapper``)
==============================

Root: ``.receipt-wrapper`` → ``.receipt`` → ``.receipt-card``.

* ``.receipt-header``, ``.logo``, ``.logo-screen``, ``.logo-print``,
  ``.receipt-company-info``, ``.receipt-body``
* ``.order-summary-grid``, ``.summary-item``
* ``.order-table``, ``.product-name``, ``.product-description``
* ``.order-totals``, ``.order-totals--grow``, ``.totals-row``,
  ``.totals-label``, ``.totals-value``
* ``.qr-section``, ``.qr-code``
* ``.invoice-actions``
* ``.print-receipt-button``, ``.go-to-course-button``,
  ``.go-to-dashboard-button``
* Shared buttons: ``.btn``, ``.btn-primary``

Payment history (``.order-history``)
====================================

Root: ``.order-history`` → ``.oh-hero`` (banner) and ``.oh-card``.

* ``.oh-card-title``
* ``.oh-table``, one row per order: a table on wide screens, a stacked card
  at ``768px`` and below
* ``.oh-col-date``, ``.oh-col-item``, ``.oh-col-amount``, ``.oh-col-status``,
  ``.oh-col-invoice``
* ``.oh-item-type`` (the *Program* label of a bundle)
* ``.oh-status``, with ``data-status`` set to ``paid``, ``confirming`` or
  ``refunded``. A ``refund_requested`` style is also defined, for refund
  requests: a future feature, not implemented yet, so no row carries it today
* ``.oh-pager``
* Empty history: ``.oh-empty``, ``.oh-empty-icon``

Examples
********

Change primary CTAs and links
=============================

.. code-block:: css

    :root {
        --pgn-color-primary-500: #0b5cab;
        --pgn-color-primary-600: #094a8a;
    }

Affects the primary color and hover used by payment pages.

Change error / danger styling
=============================

.. code-block:: css

    :root {
        --pgn-color-danger-100: #fdecee;
        --pgn-color-danger-500: #a61b28;
    }

Affects error color, border, and surface.

Change only the payment error surface
=====================================

Scope a Paragon variable to the payment status wrapper to leave the site-wide
palette unchanged:

.. code-block:: css

    .zeitlabs-payment .status-page {
        --pgn-color-danger-100: #fff5f5;
    }
