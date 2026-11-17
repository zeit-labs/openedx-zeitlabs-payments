"""Remove the expired cart status, closing every expired cart."""
from typing import Any

from django.db import migrations, models


def close_expired_carts(apps: Any, schema_editor: Any) -> None:  # pylint: disable=unused-argument
    """
    Close every expired cart.

    An expired cart was handed off and given up on with no payment confirmed, as a closed one is. Nothing settles either
    any more, so the learner keeps the same outcome under the status that remains: ended without payment, item freed.
    """
    apps.get_model('zeitlabs_payments', 'Cart').objects.filter(status='expired').update(status='closed')


class Migration(migrations.Migration):

    dependencies = [
        ('zeitlabs_payments', '0004_release_1_0'),
    ]

    operations = [
        migrations.RunPython(close_expired_carts, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='cart',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('processing', 'Processing'),
                    ('paid', 'Paid'),
                    ('cancelled', 'Cancelled'),
                    ('closed', 'Closed'),
                    ('payment_pending', 'Payment Pending'),
                    ('unreconciled', 'Unreconciled'),
                    ('refund_requested', 'Refund Requested'),
                    ('refunded', 'Refunded'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
