# Change Log

All notable changes to this project are documented in this file. It follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] – 2026-11-14

### Added

- A payment history page for learners, at `/payment/v1/history/`: every order that reached a gateway, with its status
  and its invoice.
- A payment history API for staff and superusers, at `/api/payment/v1/history/<user id>/`, listing any learner's
  orders.
- A *Payment not started* page for a refused hand-off – an unknown or hidden payment method, or a cart that is not the
  learner's or no longer pending – in place of an error.
- Documentation for engineers who run the plugin or build a payment processor.

### Changed

- **Processors:** a processor must declare `GATEWAY_ORDER_ID_PARAM` and implement `get_gateway_order_id`. One that
  does not is skipped when the LMS starts, and a hand-off that names no gateway order fails.
- **Processors:** a payment is known by its gateway order rather than its transaction id, so each cart is handed to
  one single-use order. Report `DECLINED` or `CANCELLED` only for an answer the gateway treats as final.
- The `expired` cart status is removed: a migration closes every expired cart. A cart handed off with no gateway order
  recorded within ten minutes is closed.

### Fixed

- Checkout refuses a course while any cart blocking it is open, including a bundle that contains it.
- Checkout refuses a bundle with no courses, and one whose every course the learner already holds.
- A payment for an item the learner came to hold while paying is held for staff instead of settled.
- The pages of a payment still in progress for a bundle speak of the bundle, not a course.
- Arabic translations.

## [1.0.0] – 2026-10-26

A rewrite of the 0.x releases.

### Added

- Courses and program bundles sold from a catalogue, in the one currency a deployment trades in.
- A versioned processor contract under the `zeitlabs_payments.v1` entry point, with mixins for a processor's return
  and status views and a waiting page that polls the gateway.
- Cart statuses with guarded transitions, and an audit trail of every step of every cart.
- Payments the plugin cannot settle are held for staff, who retry the invoice and enrollment, accept the payment or
  record its refund from the admin.
- Invoices numbered in a series per prefix, a public receipt behind a QR code, and overridable learner and
  organization details, hidden for retired learners.
- Learner pages styled with Paragon variables, in English and Arabic.

## [0.1.0 – 0.1.6] – 2025-09-11 to 2026-01-19

The first releases, replaced by 1.0.0: a cart, checkout and invoice inside the LMS, with payment processors as
separate plugins.
