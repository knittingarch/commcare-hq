# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

from corehq.sql_db.operations import HqRunSQL


TABLE_NAME = 'form_processor_commcarecasesql'
INDEX_NAME = 'form_processor_commcarecasesql_owner_id_3a402c4e_idx'
COLUMNS = ['owner_id', 'server_modified_on']


CREATE_INDEX_SQL = "CREATE INDEX CONCURRENTLY {} ON {} ({})".format(
    INDEX_NAME, TABLE_NAME, ','.join(COLUMNS))
DROP_INDEX_SQL = "DROP INDEX CONCURRENTLY {}".format(INDEX_NAME)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('form_processor', '0065_auto_20170725_1339'),
    ]

    operations = [
        HqRunSQL(
            sql=CREATE_INDEX_SQL,
            reverse_sql=DROP_INDEX_SQL,
            state_operations=[
                migrations.AlterIndexTogether(
                    name='commcarecasesql',
                    index_together=set([
                        ('owner_id', 'server_modified_on'),
                        ('domain', 'owner_id', 'closed'),
                        ('domain', 'external_id', 'type')
                    ]),
                ),
            ]
        ),
    ]
