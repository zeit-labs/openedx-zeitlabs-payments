## Change Log

All enhancements and patches to zeitlabs_payments will be documented
in this file. It adheres to the structure of https://keepachangelog.com/ ,
but in reStructuredText instead of Markdown (for ease of incorporation into
Sphinx documentation and the PyPI description).

This project adheres to Semantic Versioning (https://semver.org/).

There should always be an "Unreleased" section for changes pending release.

## [Unreleased]

-

[0.1.5] – 2026-01-03
**********************************************

### Fixed

- Django admin error
- Minor bug fixes

### Changes

- BREAKING CHANGE: Better settings: combined in one dictionary. Settings are now under `ZEITLABS_PAYMENTS_SETTINGS` dictionary.
  - `INVOICE_PREFIX` -> `ZEITLABS_PAYMENTS_SETTINGS['invoice_prefix']`
  - `ORGANIZATION` -> `ZEITLABS_PAYMENTS_SETTINGS['organization']`
  - `CUSTOMER_NUMBER` -> `ZEITLABS_PAYMENTS_SETTINGS['customer_number']`
  - `VALID_CURRENCY` -> `ZEITLABS_PAYMENTS_SETTINGS['valid_currency']`
  - `IS_ZEITLABS_PAYMENTS_ENABLED` -> kept as is for easier toggling of the app.

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
