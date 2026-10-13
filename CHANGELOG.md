## Change Log

All enhancements and patches to zeitlabs_payments will be documented
in this file. It adheres to the structure of https://keepachangelog.com/ ,
but in reStructuredText instead of Markdown (for ease of incorporation into
Sphinx documentation and the PyPI description).

This project adheres to Semantic Versioning (https://semver.org/).

There should always be an "Unreleased" section for changes pending release.

## [Unreleased]

### Fixed

- Invoice numbers are allocated from a per-prefix `InvoiceSequence` counter locked inside the invoice transaction, so concurrent payments get distinct, sequential numbers and a number is never consumed without an invoice. The counter compares numerically, so the sequence survives gaining a digit (999999 -> 1000000). Existing deployments are seeded from their current invoices by migration `0003`.
- A cart that is paid but whose invoice or fulfillment failed is now flagged with the `paid_cart_not_fulfilled` audit log action instead of only being logged.

[0.1.4] – 2025-11-30
**********************************************

### Fixed

- General bug fixes
- Enhanced invoice styling
- Some refactoring for better code maintainability

### Added

- SKU field set as unique in the database
- tox command for running makemigrations: `tox -e makemigrations`

[0.1.1] – 2025-09-15
**********************************************

### Fixed

- Set the correct license (AGPL v3)

[0.1.0] – 2025-09-11
**********************************************

### Added

- First release
