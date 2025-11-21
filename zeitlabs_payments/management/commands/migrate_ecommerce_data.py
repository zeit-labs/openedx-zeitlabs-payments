# myplugin/management/commands/migrate_ecommerce_data.py
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.utils import timezone
from zeitlabs_payments.models import (
    CatalogueItem, Cart, CartItem, Invoice, InvoiceItem,
    AuditLog, Transaction, MigrationMap
)
from datetime import datetime
from django.db import connections
from django.db.utils import OperationalError

User = get_user_model()
BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Migrate Ecommerce data into zeitlabs payments data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=BATCH_SIZE,
            help=f'Batch size for migration (default: {BATCH_SIZE})'
        )

        parser.add_argument("--db-name", type=str, default="ecommerce")
        parser.add_argument("--db-engine", type=str, default="django.db.backends.mysql")
        parser.add_argument("--db-user", type=str, default="root")
        parser.add_argument("--db-password", type=str, default="")
        parser.add_argument("--db-host", type=str, default="mysql")
        parser.add_argument("--db-port", type=str, default="3306")

        parser.add_argument("--no-dry-run", action="store_true", help="Execute the migration (default is dry-run).")

        parser.add_argument(
            "--test-connection",
            action="store_true",
            help="Only test the DB connection and print sample data."
        )

        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help="Retry migration for previously failed entries."
        )

    def handle(self, *args, **options):
        self.no_dry_run = not options["no_dry_run"]
        if self.no_dry_run:
            self.stdout.write(self.style.WARNING("⚠️  Running in EXECUTION mode (not dry-run)!"))
        else:
            self.stdout.write(self.style.WARNING("ℹ️  Running in DRY-RUN mode (no data will be written)."))

        self.batch_size = options["batch_size"]
        self.retry_failed = options["retry_failed"]
        connections.databases['ecommerce'] = {
            'ENGINE': options['db_engine'],
            'NAME': options['db_name'],
            'USER': options['db_user'],
            'PASSWORD': options['db_password'],
            'HOST': options['db_host'],
            'PORT': options['db_port'],
        }

        if options["test_connection"]:
            if 'ecommerce' in connections:
                connections['ecommerce'].close()
            self.test_db_connection()
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting Ecommerce data migration with batch size {self.batch_size}..."
            )
        )
        self.migrate_catalogue_items()
        self.migrate_carts()
        self.migrate_audit_logs()
        self.migrate_transactions()
        self.migrate_invoices()

    def test_db_connection(self):
        """Simple and reliable test for the ecommerce database."""
        self.stdout.write(self.style.WARNING("🔍 Testing Ecommerce DB connection..."))
        try:
            with connections['ecommerce'].cursor() as cursor:
                cursor.execute("SELECT 1;")
                row = cursor.fetchone()
                if row and row[0] == 1:
                    self.stdout.write(self.style.SUCCESS("✅ Connection successful!"))
                else:
                    self.stdout.write(self.style.ERROR("⚠️ Connection test query failed."))
        except Exception as e:
            raise CommandError(f"❌ Could not connect to database: {e}")

    def should_skip(self, table: str, source_id: int) -> bool:
        """
        Determine whether to skip migrating a record based on retry mode.

        When --retry-failed is ON:
            Skip ones that succeeded already.
        When --retry-failed is OFF:
            Skip ones that already had ANY attempt.
        """
        if self.retry_failed:
            return MigrationMap.has_succeeded(source_table=table, source_id=source_id)
        else:
            return MigrationMap.last_attempt(source_table=table, source_id=source_id)

    def log_progress(self, label, attempts: int, successes: int, failures: int, skipped: int, final_summary=False):
        """
        Logs periodic progress with timestamp.
        """
        if not final_summary:
            self.stdout.write(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{label} Processed {attempts} items... (✔ {successes}, ❌ {failures}, ↩ {skipped})"
            )
        else:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n🎉 {label} Complete"))
            self.stdout.write(
                self.style.SUCCESS(
                    f"TOTAL processed: {attempts} | "
                    f"✅ success: {successes} | "
                    f"❌ failed: {failures} | "
                    f"↩ skipped: {skipped}"
                )
            )

    def migrate_catalogue_items(self):
        self.stdout.write(self.style.NOTICE("\n==========> Migrating Catalogue Items"))
        source_table = "partner_stockrecord"
        target_model = "CatalogueItem"
        successes = 0
        failures = 0
        skipped = 0
        attempts = 0
        query = """
            SELECT
                s.id,
                s.partner_sku,
                s.price,
                s.price_currency,
                p.id AS product_id,
                p.title,
                p.description,
                p.course_id,
                p.date_created,
                p.date_updated
            FROM partner_stockrecord s
            INNER JOIN catalogue_product p ON s.product_id = p.id
            WHERE p.structure = 'child';
        """

        with connections['ecommerce'].cursor() as cursor:
            cursor.execute(query)

            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break

                for (
                    stock_rec_id,
                    partner_sku,
                    price,
                    price_currency,
                    product_id,
                    title,
                    description,
                    course_id,
                    date_created,
                    date_updated,
                ) in rows:

                    attempts += 1

                    if self.should_skip(source_table, stock_rec_id):
                        skipped += 1
                        continue

                    try:
                        if not self.no_dry_run:
                            with db_transaction.atomic():
                                item = CatalogueItem.objects.create(
                                    sku=partner_sku,
                                    type=CatalogueItem.ItemType.PAID_COURSE,
                                    title=title,
                                    description=description or "",
                                    item_ref_id=course_id,
                                    price=price or 0,
                                    currency=price_currency or "SAR",
                                    created_at=date_created,
                                )

                                MigrationMap.record_success(
                                    source_table=source_table,
                                    source_id=stock_rec_id,
                                    target_model=target_model,
                                    target_id=item.id,
                                )

                        successes += 1

                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {e}"
                        self.stderr.write(f"❌ ERROR stock_id={stock_rec_id} ({error_msg})")
                        if not self.no_dry_run:
                            MigrationMap.record_failure(
                                source_table=source_table,
                                source_id=stock_rec_id,
                                target_model=target_model,
                                error_msg=error_msg,
                            )

                        failures += 1
                        continue

                self.log_progress(
                    label="Catalogue Migration",
                    attempts=attempts,
                    successes=successes,
                    failures=failures,
                    skipped=skipped
                )

        self.log_progress(
            label="Catalogue Migration",
            attempts=attempts,
            successes=successes,
            failures=failures,
            skipped=skipped,
            final_summary=True
        )

    def migrate_carts(self):
        self.stdout.write(self.style.NOTICE("\n==========> Migrating Carts and Cart Items"))
        source_table_cart = "basket_basket"
        source_table_cart_item = "basket_line"
        target_cart_model = "Cart"
        target_item_model = "CartItem"

        cart_successes = cart_failures = cart_skipped = 0
        item_attempts = item_successes = item_failures = item_skipped = 0

        query = """
            SELECT
                b.id AS basket_id,
                b.date_created,
                b.owner_id,
                u.username,
                CASE
                    WHEN o.status = 'Complete' THEN 'paid'
                    WHEN b.status = 'Frozen' THEN 'processing'
                    ELSE 'pending'
                END AS cart_status,
                l.id AS line_id,
                l.quantity,
                l.price_excl_tax,
                l.price_incl_tax,
                s.partner_sku
            FROM basket_basket b
            LEFT JOIN order_order o ON b.id = o.basket_id
            LEFT JOIN basket_line l ON b.id = l.basket_id
            LEFT JOIN partner_stockrecord s ON l.stockrecord_id = s.id
            LEFT JOIN ecommerce_user u ON b.owner_id = u.id
            ORDER BY b.id;
        """

        with connections['ecommerce'].cursor() as cursor:
            cursor.execute(query)
            seen_carts = {}

            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break

                # preload SKUs + usernames
                skus = {row[9] for row in rows if row[9]}
                usernames = {row[3] for row in rows if row[3]}
                sku_to_item = {
                    item.sku: item
                    for item in CatalogueItem.objects.filter(sku__in=skus)
                }
                user_map = {
                    u.username: u
                    for u in User.objects.filter(username__in=usernames)
                }
                basket_ids = {row[0] for row in rows if row[0]}
                existing_carts = Cart.objects.filter(id__in=basket_ids)

                for (
                    basket_id,
                    date_created,
                    owner_id,
                    owner_username,
                    cart_status,
                    line_id,
                    quantity,
                    price_excl_tax,
                    price_incl_tax,
                    partner_sku,
                ) in rows:
                    is_cart_processing_failed = False
                    if (
                        self.should_skip(source_table_cart, basket_id) and
                        self.should_skip(source_table_cart_item, line_id)
                    ):
                        cart_skipped += 1
                        item_skipped += 1
                        continue

                    if basket_id not in seen_carts:
                        if not self.should_skip(source_table_cart, basket_id):
                            try:
                                if not self.no_dry_run:
                                    with db_transaction.atomic():
                                        cart = Cart.objects.create(
                                            id=basket_id,
                                            user=user_map.get(owner_username),
                                            status=cart_status,
                                            created_at=date_created,
                                        )
                                        MigrationMap.record_success(
                                            source_table=source_table_cart,
                                            source_id=basket_id,
                                            target_model=target_cart_model,
                                            target_id=cart.id,
                                        )
                                        seen_carts[basket_id] = cart
                                cart_successes += 1

                            except Exception as e:
                                error_msg = f"{type(e).__name__}: {e}"
                                self.stderr.write(f"❌ CART ERROR id={basket_id} ({error_msg})")
                                if not self.no_dry_run:
                                    MigrationMap.record_failure(
                                        source_table=source_table_cart,
                                        source_id=basket_id,
                                        target_model=target_cart_model,
                                        error_msg=error_msg,
                                    )
                                cart_failures += 1
                                is_cart_processing_failed = True
                        else:
                            cart_skipped += 1
                            try:
                                seen_carts[basket_id] = existing_carts.get(id=basket_id)
                            except Cart.DoesNotExist:
                                error_msg = (
                                    "ERROR - occurred while processing cart. "
                                    "It appears as migrated in CartMigrationMap but cannot be found "
                                    "in the existing Cart records"
                                )
                                self.stderr.write(f"❌ CART ERROR id={basket_id} ({error_msg})")
                                is_cart_processing_failed = True

                    item_attempts += 1
                    if is_cart_processing_failed:
                        if not self.no_dry_run:
                            MigrationMap.record_failure(
                                source_table=source_table_cart_item,
                                source_id=line_id,
                                target_model=target_item_model,
                                error_msg='Unable to process cart item due to cart creation failure.',
                            )
                        item_failures += 1
                        continue

                    # Skip if basket row has no line (LEFT JOIN produced None)
                    if line_id is None:
                        error_msg = (
                            "ERROR - occurred while processing cart."
                            f"{source_table_cart} is not linked with any {source_table_cart_item}"
                        )
                        self.stderr.write(f"❌ CART ERROR id={basket_id} ({error_msg})")
                        continue

                    if self.should_skip(source_table_cart_item, line_id):
                        item_skipped += 1
                    else:
                        try:
                            if not self.no_dry_run:
                                with db_transaction.atomic():
                                    tax_amount = (price_incl_tax or 0) - (price_excl_tax or 0)
                                    item = CartItem.objects.create(
                                        cart=seen_carts[basket_id],
                                        original_price=price_excl_tax or 0,
                                        final_price=price_incl_tax or 0,
                                        tax_amount=tax_amount,
                                        catalogue_item=sku_to_item.get(partner_sku),
                                    )
                                    MigrationMap.record_success(
                                        source_table=source_table_cart_item,
                                        source_id=line_id,
                                        target_model=target_item_model,
                                        target_id=item.id,
                                    )
                            item_successes += 1
                        except Exception as e:
                            error_msg = f"{type(e).__name__}: {e}"
                            self.stderr.write(f"❌ CARTITEM ERROR id={line_id} ({error_msg})")
                            if not self.no_dry_run:
                                MigrationMap.record_failure(
                                    source_table=source_table_cart_item,
                                    source_id=line_id,
                                    target_model=target_item_model,
                                    error_msg=error_msg,
                                )
                            item_failures += 1

                self.log_progress(
                    attempts=len(seen_carts),
                    successes=cart_successes,
                    failures=cart_failures,
                    skipped=cart_skipped,
                    label="Cart"
                )

                self.log_progress(
                    attempts=item_attempts,
                    successes=item_successes,
                    failures=item_failures,
                    skipped=item_skipped,
                    label="CartItem"
                )

        self.log_progress(
            label="Cart Migration",
            attempts=len(seen_carts),
            successes=cart_successes,
            failures=cart_failures,
            skipped=cart_skipped,
            final_summary=True
        )

        self.log_progress(
            label="CartItem Migration",
            attempts=item_attempts,
            successes=item_successes,
            failures=item_failures,
            skipped=item_skipped,
            final_summary=True
        )

    def migrate_audit_logs(self):
        self.stdout.write(self.style.NOTICE("\n==========> Migrating Audit Logs"))
        source_table = "payment_paymentprocessorresponse"
        target_model = "AuditLog"
        successes = 0
        failures = 0
        skipped = 0
        attempts = 0
        query = """
            SELECT
                id,
                processor_name,
                response,
                basket_id
            FROM
                payment_paymentprocessorresponse
            ORDER BY
                id
        """

        with connections['ecommerce'].cursor() as cursor:
            cursor.execute(query)

            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break

                for resp_id, processor_name, response, basket_id in rows:
                    attempts += 1

                    if self.should_skip(source_table, resp_id):
                        skipped += 1
                        continue

                    try:
                        if not self.no_dry_run:
                            with db_transaction.atomic():
                                log = AuditLog.objects.create(
                                    cart_id=basket_id,
                                    gateway=processor_name,
                                    action="received_gateway_response",
                                    details=response,
                                )

                                MigrationMap.record_success(
                                    source_table=source_table,
                                    source_id=resp_id,
                                    target_model=target_model,
                                    target_id=log.id,
                                )
                        successes += 1

                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {e}"
                        self.stderr.write(f"❌ ERROR payment response id={resp_id} ({error_msg})")

                        if not self.no_dry_run:
                            MigrationMap.record_failure(
                                source_table=source_table,
                                source_id=resp_id,
                                target_model=target_model,
                                error_msg=error_msg,
                            )
                        failures += 1
                        continue

                self.log_progress(
                    label="Audit logs Migration",
                    attempts=attempts,
                    successes=successes,
                    failures=failures,
                    skipped=skipped
                )
        self.log_progress(
            label="Audit logs Migration",
            attempts=attempts,
            successes=successes,
            failures=failures,
            skipped=skipped,
            final_summary=True
        )

    def migrate_transactions(self):
        self.stdout.write(self.style.NOTICE("\n==========> Migrating Transactions"))
        source_table = "order_paymentevent"
        target_model = "Transaction"
        successes = 0
        failures = 0
        skipped = 0
        attempts = 0
        query = """
            SELECT
                pe.id,
                o.basket_id,
                pe.processor_name,
                pe.reference,
                pe.amount,
                o.currency,
                pe.date_created,
                et.name
            FROM
                order_paymentevent AS pe
            JOIN
                order_order AS o
                ON pe.order_id = o.id
            JOIN
                order_paymenteventtype AS et
                ON pe.event_type_id = et.id;


        """

        with connections['ecommerce'].cursor() as cursor:
            cursor.execute(query)

            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break

                for event_id, basket_id, processor_name, reference, amount, currency, date_created, event_name in rows:
                    attempts += 1

                    if self.should_skip(source_table, event_id):
                        skipped += 1
                        continue

                    try:
                        if not self.no_dry_run:
                            with db_transaction.atomic():
                                log = Transaction.objects.create(
                                    cart_id=basket_id,
                                    gateway=processor_name,
                                    gateway_transaction_id=reference,
                                    type="payment",
                                    status=event_name,
                                    amount=amount,
                                    currency=currency,
                                    created_at=date_created,
                                )
                                MigrationMap.record_success(
                                    source_table=source_table,
                                    source_id=event_id,
                                    target_model=target_model,
                                    target_id=log.id,
                                )
                        successes += 1

                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {e}"
                        self.stderr.write(f"❌ ERROR payment event id={event_id} ({error_msg})")
                        if not self.no_dry_run:
                            MigrationMap.record_failure(
                                source_table=source_table,
                                source_id=event_id,
                                target_model=target_model,
                                error_msg=error_msg,
                            )
                        failures += 1
                        continue

                self.log_progress(
                    label="Transactions Migration",
                    attempts=attempts,
                    successes=successes,
                    failures=failures,
                    skipped=skipped
                )
        self.log_progress(
            label="Transactions Migration",
            attempts=attempts,
            successes=successes,
            failures=failures,
            skipped=skipped,
            final_summary=True
        )

    def migrate_invoices(self):
        self.stdout.write(self.style.NOTICE("\n==========> Migrating Invoice and Invoice Items"))
        source_table_invoice = "order_order"
        source_table_invoice_item = "order_line"
        target_invoice_model = "Invoice"
        target_item_model = "InvoiceItem"

        invoice_successes = invoice_failures = invoice_skipped = 0
        item_attempts = item_successes = item_failures = item_skipped = 0

        query = """
            SELECT
                o.id AS order_id,
                b.id AS basket_id,
                o.number,
                CASE
                    WHEN o.status = 'Complete' THEN 'paid'
                    ELSE 'draft'
                END AS order_status,
                o.total_excl_tax,
                o.total_incl_tax,
                o.currency,
                o.date_placed,
                l.id,
                l.line_price_excl_tax,
                l.line_price_incl_tax,
                l.unit_price_incl_tax,
                l.quantity,
                l.effective_contract_discounted_price,
                l.partner_sku,
                pe.reference AS payment_reference,
                pe.processor_name AS payment_processor
            FROM
                basket_basket AS b
            LEFT JOIN
                order_order AS o
                ON b.id = o.basket_id
            INNER JOIN
                order_line AS l
                ON o.id = l.order_id
            LEFT JOIN
                order_paymentevent AS pe
                ON o.id = pe.order_id
            ORDER BY
                b.id;
        """

        with connections['ecommerce'].cursor() as cursor:
            cursor.execute(query)
            seen_invoices = {}

            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break

                transactions = Transaction.objects.filter(cart_id__in={row[1] for row in rows})
                cart_items = CartItem.objects.filter(cart_id__in={row[1] for row in rows})
                existing_invoices = Invoice.objects.filter(id__in={row[0] for row in rows})

                for (
                    order_id,
                    basket_id,
                    order_number,
                    order_status,
                    order_total_excl_tax,
                    order_total_incl_tax,
                    currency,
                    order_date,
                    line_id,
                    line_price_excl_tax,
                    line_price_incl_tax,
                    unit_price_incl_tax,
                    quantity,
                    discount,
                    sku,
                    payment_reference,
                    payment_processor,
                ) in rows:
                    is_invoice_processing_failed = False
                    if (
                        self.should_skip(source_table_invoice, order_id) and
                        self.should_skip(source_table_invoice_item, line_id)
                    ):
                        invoice_skipped += 1
                        item_skipped += 1
                        continue

                    if order_id not in seen_invoices:
                        if not self.should_skip(source_table_invoice, order_id):
                            try:
                                if not self.no_dry_run:
                                    with db_transaction.atomic():
                                        invoice = Invoice.objects.create(
                                            id=order_id,
                                            invoice_number=order_number,
                                            cart_id=basket_id,
                                            status=order_status,
                                            gross_total=order_total_excl_tax,
                                            total=order_total_incl_tax,
                                            tax_total=order_total_incl_tax - order_total_excl_tax,
                                            currency=currency,
                                            paid_at=order_date,
                                            related_transaction=transactions.filter(
                                                gateway_transaction_id=payment_reference,
                                                gateway=payment_processor,
                                                cart_id=basket_id
                                            ).first()
                                        )
                                        MigrationMap.record_success(
                                            source_table=source_table_invoice,
                                            source_id=order_id,
                                            target_model=target_invoice_model,
                                            target_id=invoice.id,
                                        )
                                        seen_invoices[order_id] = invoice
                                invoice_successes += 1

                            except Exception as e:
                                error_msg = f"{type(e).__name__}: {e}"
                                self.stderr.write(f"❌ INVOICE ERROR id={order_id} ({error_msg})")
                                if not self.no_dry_run:
                                    MigrationMap.record_failure(
                                        source_table=source_table_invoice,
                                        source_id=order_id,
                                        target_model=target_invoice_model,
                                        error_msg=error_msg,
                                    )
                                invoice_failures += 1
                                is_invoice_processing_failed = True
                        else:
                            invoice_skipped += 1
                            try:
                                seen_invoices[order_id] = existing_invoices.get(id=order_id)
                            except Invoice.DoesNotExist:
                                error_msg = (
                                    "ERROR - occurred while processing invoice. "
                                    "It appears as migrated in MigrationMap but cannot be found "
                                    "in the existing Invoice records"
                                )
                                self.stderr.write(f"❌ INVOICE ERROR id={order_id} ({error_msg})")
                                is_invoice_processing_failed = True

                    item_attempts += 1
                    if is_invoice_processing_failed:
                        if not self.no_dry_run:
                            MigrationMap.record_failure(
                                source_table=source_table_invoice_item,
                                source_id=line_id,
                                target_model=target_item_model,
                                error_msg='Unable to process invoice item due to invoice creation failure.',
                            )
                        item_failures += 1
                        continue

                    if self.should_skip(source_table_invoice_item, line_id):
                        item_skipped += 1
                    else:
                        try:
                            if not self.no_dry_run:
                                with db_transaction.atomic():
                                    item = InvoiceItem.objects.create(
                                        invoice=seen_invoices[order_id],
                                        cart_item=cart_items.filter(cart_id=basket_id, catalogue_item__sku=sku).first(),
                                        original_price=line_price_excl_tax,
                                        discount_amount=discount or 0,
                                        tax_amount=line_price_incl_tax - line_price_excl_tax,
                                        price=unit_price_incl_tax,
                                        quantity=quantity,
                                    )
                                    MigrationMap.record_success(
                                        source_table=source_table_invoice_item,
                                        source_id=line_id,
                                        target_model=target_item_model,
                                        target_id=item.id,
                                    )
                            item_successes += 1
                        except Exception as e:
                            error_msg = f"{type(e).__name__}: {e}"
                            self.stderr.write(f"❌ INVOICE ITEM ERROR id={line_id} ({error_msg})")
                            if not self.no_dry_run:
                                MigrationMap.record_failure(
                                    source_table=source_table_invoice_item,
                                    source_id=line_id,
                                    target_model=target_item_model,
                                    error_msg=error_msg,
                                )
                            item_failures += 1
                self.log_progress(
                    attempts=len(seen_invoices),
                    successes=invoice_successes,
                    failures=invoice_failures,
                    skipped=invoice_skipped,
                    label="Invoice"
                )

                self.log_progress(
                    attempts=item_attempts,
                    successes=item_successes,
                    failures=item_failures,
                    skipped=item_skipped,
                    label="InvoiceItem"
                )
        self.log_progress(
            attempts=len(seen_invoices),
            successes=invoice_successes,
            failures=invoice_failures,
            skipped=invoice_skipped,
            label="Invoice",
            final_summary=True
        )

        self.log_progress(
            attempts=item_attempts,
            successes=item_successes,
            failures=item_failures,
            skipped=item_skipped,
            label="InvoiceItem",
            final_summary=True
        )
