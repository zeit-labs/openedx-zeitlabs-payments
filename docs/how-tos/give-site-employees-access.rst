Give a site's employees access to its payments
##############################################

On a deployment serving several organizations, each from its own site, the LMS admin is for the platform's own staff.
A site's employees -- its finance or support team -- are not made staff. Instead, they are granted access to that
site's payments, one site at a time.

With access, an employee signed in to the LMS reads a learner's payment history through the history API, and opens the
invoices paid on that site. Both are on that site's own address. They see nothing paid on another site, and nothing
from before 1.0 whose site was never recorded (see :doc:`upgrade-from-0x`). Superusers already see everything, and
need no grant.

Grant the access
****************

#. In the LMS admin, open *Zeitlabs_Payments > Payment site permissions* and click **Add**.
#. Pick the employee under **User**, by their user id or through the lookup next to the field.
#. Pick the **Site** they work for.
#. Tick **Users order history**, and save.

An employee working for two sites gets one row for each. A second row for the same user and site is refused.

What the employee can do
************************

* ``GET /api/payment/v1/history/<user id>/`` on the site's address: a learner's orders paid on that site, as JSON. A
  user id naming nobody answers as an empty history. See :doc:`../references/urls-and-apis`.
* ``/payment/v1/invoice/<invoice number>/`` on the site's address: an invoice paid on that site. Any other invoice,
  including one asked for on another site's address, gets the page saying the address is private.

Switch the access off
*********************

Untick **Users order history** and save. The row is kept, so the access can be switched back on later. Delete the row
to remove it for good. Deleting the user or the site deletes their rows too.
