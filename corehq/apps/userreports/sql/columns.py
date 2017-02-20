from __future__ import absolute_import
import sqlalchemy
from sqlagg import SumWhen
from fluff import TYPE_STRING
from fluff.util import get_column_type

from corehq.apps.userreports.columns import UCRExpandDatabaseSubcolumn


def column_to_sql(column):
    # we have to explicitly truncate the column IDs otherwise postgres will do it
    # and will choke on them if there are duplicates: http://manage.dimagi.com/default.asp?175495
    return sqlalchemy.Column(
        column.database_column_name,
        _get_column_type(column.datatype),
        nullable=column.is_nullable,
        primary_key=column.is_primary_key,
    )


def expand_column(report_column, distinct_values, lang):
    columns = []
    for index, val in enumerate(distinct_values):
        alias = u"{}-{}".format(report_column.column_id, index)
        if val is None:
            sql_agg_col = SumWhen(
                whens={'"{}" is NULL'.format(report_column.field): 1}, else_=0, alias=alias
            )
        else:
            sql_agg_col = SumWhen(report_column.field, whens={val: 1}, else_=0, alias=alias)

        columns.append(UCRExpandDatabaseSubcolumn(
            u"{}-{}".format(report_column.get_header(lang), val),
            agg_column=sql_agg_col,
            expand_value=val,
            sortable=False,
            data_slug=u"{}-{}".format(report_column.column_id, index),
            format_fn=report_column.get_format_fn(),
            help_text=report_column.description
        ))
    return columns


def _get_column_type(data_type):
    if data_type == TYPE_STRING:
        return sqlalchemy.UnicodeText
    return get_column_type(data_type)
