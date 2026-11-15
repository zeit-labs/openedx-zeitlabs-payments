"""Add the permission granting a site's employees access to its payments."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sites', '0002_alter_domain_unique'),
        ('zeitlabs_payments', '0005_remove_expired_cart_status'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentSitePermission',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('users_order_history', models.BooleanField(default=False, help_text="May read a learner's payment history on this site through the history API, and open the invoices paid on this site.")),
                ('site', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payment_site_permissions', to='sites.site')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payment_site_permissions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('user', 'site')},
            },
        ),
    ]
