Sell a course or a bundle
#########################

What the plugin sells is a **catalogue item**: a course, or a bundle of courses, with the price the learner pays. You
manage catalogue items in the LMS admin, under *Zeitlabs_Payments > Catalogue items*.

A deployment trades in one currency, its ``valid_currency`` setting. Every catalogue item is priced in it, and the
admin refuses to save an item priced in another, or with more decimal places than the currency has (two for ``USD``,
three for ``KWD``, none for ``JPY``). See :doc:`../references/settings`.

Sell a course
*************

A course is sold through one of its paid course modes: *verified*, *professional* or *no-id-professional*. The course
mode and the catalogue item are joined by their **SKU**.

#. In the LMS admin, under *Course Modes*, give the paid mode a SKU, for example ``DATA101-VERIFIED``. The SKU is any
   text unique across your catalogue.
#. Under *Zeitlabs_Payments > Catalogue items*, add an item:

   .. list-table::
      :header-rows: 1
      :widths: 25 75

      * - Field
        - Value
      * - SKU
        - The course mode's SKU, exactly.
      * - Type
        - *paid course*
      * - Title, Description
        - What checkout and the invoice show for the course.
      * - Item ref id
        - The course id, for example ``course-v1:Acme+DATA101+2026``. It must be the course the mode belongs to.
      * - Price
        - What the learner pays. The course mode's own price is not read.
      * - Currency
        - The deployment's ``valid_currency``.

Open edX's buttons for the paid track -- on the track selection page, the course about page or the dashboard's
*Upgrade* -- now lead to the plugin's checkout. A learner enrolled in the audit or honor track buys an upgrade: they
keep their progress and move to the paid mode.

Sell a bundle
*************

A bundle sells several courses at one price. Each of its courses is a catalogue item of its own, set up as above.

#. Under *Catalogue items*, add an item of type *program bundle*, with a SKU and item ref id of your choice (a
   program's UUID, for example), a title, a description and the bundle's price.
#. Save it. A **Bundle course items** section appears below the form: add each course's catalogue item there.

Open edX has no button for a bundle, so link to its checkout yourself, from a marketing page or an email:
``https://<your LMS>/checkout/v1/checkout/?sku=<bundle SKU>``.

A learner who already holds some of the bundle's courses -- bought before, or enrolled in a paid mode -- buys the
bundle as usual, and is enrolled only in the courses they do not hold. A learner who holds every one of them is
refused: they would pay for nothing.

When a learner cannot buy
*************************

Checkout refuses the item with one plain sentence when:

* the learner has already bought it, or a payment for it is still open -- in that case, if the payment can still go
  through, the learner is sent back to it instead, so they are never charged twice;
* they are already enrolled in the course in a paid mode, or hold every course of a bundle;
* the course mode has expired, the course's enrollment is closed, or the course is full;
* the catalogue item does not match a course mode: wrong SKU, or an item ref id naming another course;
* a bundle has no courses;
* the checkout link names more than one SKU: each purchase is one item.

A course and a bundle containing it count as the same purchase in one direction: a learner who bought a bundle cannot
buy one of its courses again on its own, but a learner who bought a course can still buy a bundle that contains it.
