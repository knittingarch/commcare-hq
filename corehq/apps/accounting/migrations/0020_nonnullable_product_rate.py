# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from __future__ import absolute_import
from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0019_remove_softwareplanversion_product_rates'),
    ]

    operations = [
        migrations.AlterField(
            model_name='softwareplanversion',
            name='product_rate',
            field=models.ForeignKey(to='accounting.SoftwareProductRate', on_delete=models.CASCADE),
            preserve_default=True,
        ),
    ]
