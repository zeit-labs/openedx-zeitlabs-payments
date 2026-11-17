"""Tests for the plugin's data migrations."""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

LAST_PUBLISHED = ('zeitlabs_payments', '0003_add_bundlecourseitem_and_program_bundle_type')
RELEASE_1_0 = ('zeitlabs_payments', '0004_release_1_0')
NO_EXPIRED_STATUS = ('zeitlabs_payments', '0005_remove_expired_cart_status')


def migrate_to(target):
    """Migrate the plugin to the given migration, and return the app registry as it stands there."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def is_applied(migration):
    """Return whether the migration is recorded as applied."""
    return migration in MigrationRecorder(connection).applied_migrations()


def paid_cart(apps, username):
    """Create a learner's paid cart, as the last published migration stores one."""
    user = apps.get_model('auth', 'User').objects.create(username=username)
    return apps.get_model('zeitlabs_payments', 'Cart').objects.create(user=user, status='paid')


def record_payment(apps, cart, gateway_transaction_id, gateway='dummy'):
    """Record a payment of the cart, as the last published migration stores one."""
    return apps.get_model('zeitlabs_payments', 'Transaction').objects.create(
        cart=cart, amount=100, gateway=gateway, gateway_transaction_id=gateway_transaction_id,
    )


def issue_invoice(apps, cart, invoice_number, payment):
    """Issue an invoice of the cart for the payment, as the last published migration stores one."""
    return apps.get_model('zeitlabs_payments', 'Invoice').objects.create(
        invoice_number=invoice_number, status='paid', total=100, gross_total=100, currency='SAR', cart=cart,
        related_transaction=payment,
    )


@pytest.mark.django_db(transaction=True)
def test_published_invoices_each_get_their_own_token_and_empty_info():
    """
    Verify that every invoice issued under the last published migration is given a public token of its own, and empty
    learner and organization info, and that one transaction id recorded by two gateways does not stop the migration.
    """
    apps = migrate_to(LAST_PUBLISHED)
    cart = paid_cart(apps, 'learner')
    first = issue_invoice(apps, cart, 'TEST-100001', record_payment(apps, cart, 'TX-1'))
    second = issue_invoice(apps, cart, 'TEST-100002', record_payment(apps, cart, 'TX-1', gateway='other'))

    try:
        apps = migrate_to(RELEASE_1_0)
        invoices = apps.get_model('zeitlabs_payments', 'Invoice').objects
        tokens = {invoices.get(pk=first.pk).public_token, invoices.get(pk=second.pk).public_token}
        assert None not in tokens and len(tokens) == 2, 'The invoices were not each given a token of their own'
        assert list(invoices.values_list('learner_info', 'organization_info')) == [({}, {}), ({}, {})], \
            'The invoices were not given empty info'
    finally:
        migrate_to(RELEASE_1_0)


@pytest.mark.django_db(transaction=True)
def test_an_invoice_without_a_transaction_stops_the_migration():
    """
    Verify that an invoice with no transaction behind it stops the migration, which is not applied, until it is
    linked to the payment it bills.
    """
    apps = migrate_to(LAST_PUBLISHED)
    cart = paid_cart(apps, 'learner')
    orphan = issue_invoice(apps, cart, 'TEST-100001', None)

    try:
        with pytest.raises(RuntimeError, match='1 invoice\\(s\\) have no transaction behind them'):
            migrate_to(RELEASE_1_0)
        assert not is_applied(RELEASE_1_0), 'The refused migration was applied'
    finally:
        apps.get_model('zeitlabs_payments', 'Invoice').objects.filter(pk=orphan.pk).update(
            related_transaction=record_payment(apps, cart, 'TX-1'),
        )
        migrate_to(RELEASE_1_0)


@pytest.mark.django_db(transaction=True)
def test_a_transaction_id_shared_within_a_gateway_stops_the_migration():
    """
    Verify that two transactions of one gateway sharing a transaction id stop the migration, which is not applied, until
    someone decides what each records.
    """
    apps = migrate_to(LAST_PUBLISHED)
    cart = paid_cart(apps, 'learner')
    record_payment(apps, cart, 'TX-1')
    duplicate = record_payment(apps, cart, 'TX-1')

    try:
        with pytest.raises(RuntimeError, match='1 id\\(s\\) are recorded on more than one transaction'):
            migrate_to(RELEASE_1_0)
        assert not is_applied(RELEASE_1_0), 'The refused migration was applied'
    finally:
        apps.get_model('zeitlabs_payments', 'Transaction').objects.filter(pk=duplicate.pk).update(
            gateway_transaction_id='TX-2',
        )
        migrate_to(RELEASE_1_0)


@pytest.mark.django_db(transaction=True)
def test_expired_carts_are_closed_and_others_left_as_they_were():
    """
    Verify that every expired cart is closed when the expired status is removed, and no cart in another status moves.

    An expired cart was given up on with its payment unconfirmed, as a closed one is; nothing settles either any more.
    """
    apps = migrate_to(RELEASE_1_0)
    user = apps.get_model('auth', 'User').objects.create(username='learner')
    carts = apps.get_model('zeitlabs_payments', 'Cart').objects
    expired = carts.create(user=user, status='expired')
    processing = carts.create(user=user, status='processing')

    try:
        apps = migrate_to(NO_EXPIRED_STATUS)
        carts = apps.get_model('zeitlabs_payments', 'Cart').objects
        assert carts.get(pk=expired.pk).status == 'closed', 'An expired cart was not closed'
        assert carts.get(pk=processing.pk).status == 'processing', 'A cart in another status moved'
    finally:
        migrate_to(NO_EXPIRED_STATUS)
