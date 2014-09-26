from sqlagg import CountUniqueColumn, CountColumn
from sqlagg.columns import SimpleColumn
from sqlagg.filters import LTE, AND, GTE, GT, EQ, NOTEQ, OR
from corehq.apps.reports.datatables import DataTablesHeader, DataTablesColumn
from corehq.apps.reports.sqlreport import SqlData, DatabaseColumn, DataFormatter, TableDataFormat, calculate_total_row

class BaseSqlData(SqlData):
    show_total = False
    datatables = False
    show_charts = False
    no_value = {'sort_key': 0, 'html': 0}
    fix_left_col = False
    total_row_name = "Total"
    custom_total_calculate = False

    def percent_fn(self, x, y):
        return "%.2f%%" % (100 * float(y or 0) / (x or 1))

    @property
    def filters(self):
        # TODO: add here location and date filter
        return []

    @property
    def headers(self):
        return DataTablesHeader(*[DataTablesColumn('Entity'), DataTablesColumn('Number'), DataTablesColumn('Percentage')])

    @property
    def group_by(self):
        return []

    @property
    def rows(self):
        formatter = DataFormatter(TableDataFormat(self.columns, no_value=self.no_value))
        return list(formatter.format(self.data, keys=self.keys, group_by=self.group_by))

    @property
    def data(self):
        return super(BaseSqlData, self).data

class MotherRegistrationOverview(BaseSqlData):
    table_name = "fluff_WorldVisionMotherFluff"
    slug = 'mother_registration_overview'
    title = 'Mother Registration Overview'

    @property
    def filters(self):
        # TODO: add here location filter
        return []

    @property
    def rows(self):
        result = []
        for column in self.columns:
            result.append([{'sort_key': column.header, 'html': column.header},
                           {'sort_key': self.data[column.slug], 'html': self.data[column.slug]}])
        return result

    @property
    def headers(self):
        return DataTablesHeader(*[DataTablesColumn('Entity'), DataTablesColumn('Number')])

    @property
    def columns(self):
        columns = [
            DatabaseColumn("Total mothers registered ever",
                CountUniqueColumn('doc_id',
                    alias="total",
                    filters=self.filters
                )
            ),
        ]
        #TODO: if date_not_selected:
        if False:
            columns.extend([
                DatabaseColumn("Total open mother cases",
                    CountUniqueColumn('doc_id',
                        alias="opened",
                        filters=self.filters + [EQ('closed_on', 'empty')]
                    )
                ),
                DatabaseColumn("Total closed mother cases",
                    CountUniqueColumn('doc_id',
                        alias="closed",
                        filters=self.filters +  [NOTEQ('closed_on', 'empty')]
                    )
                ),
                DatabaseColumn("New registrations during last 30 days",
                        CountUniqueColumn('doc_id',
                            alias="new_registrations",
                            filters=self.filters + [AND([LTE('opened_on', "last_month"), GTE('opened_on', "today")])]
                        )
                )
            ])
        else:
            columns.extend([
                DatabaseColumn("Mother cases open at end of time period",
                    CountUniqueColumn('doc_id',
                        alias="opened",
                        filters=self.filters + [AND([LTE('opened_on', "stred"), OR([EQ('closed_on', 'empty'), GT('closed_on', "stred")])])]
                    )
                ),
                DatabaseColumn("Mother cases closed during the time period",
                    CountUniqueColumn('doc_id',
                        alias="closed",
                        filters=self.filters + [AND([NOTEQ('closed_on', 'empty'), LTE('opened_on', "stred"), LTE('closed_on', "stred")])]
                    )
                ),
                DatabaseColumn("Total mothers followed during the time period",
                    CountUniqueColumn('doc_id',
                        alias="followed",
                        filters=self.filters + [AND([LTE('opened_on', "stred"), OR([EQ('closed_on', 'empty'), GT('closed_on', "strsd")])])]
                    )
                ),
                DatabaseColumn("New registrations during time period",
                    CountUniqueColumn('doc_id',
                        alias="new_registrations",
                        filters=self.filters + [AND([LTE('opened_on', "stred"), GTE('opened_on', "strsd")])]
                    )
                )
            ])
        return columns

class ClosedMotherCasesBreakdown(BaseSqlData):
    table_name = "fluff_WorldVisionMotherFluff"
    slug = 'closed_mother_cases-breakdown'
    title = 'Closed Mother Cases Breakdown'
    show_total = True
    total_row_name = "Mother cases closed during the time period"
    show_charts = True
    chart_x_label = 'Reason of closure'
    chart_y_label = 'Number'

    @property
    def group_by(self):
        return ['reason_for_mother_closure']

    @property
    def rows(self):
        rows = super(ClosedMotherCasesBreakdown, self).rows
        total = calculate_total_row(rows)[-1]
        for row in rows:
            from custom.world_vision import REASON_FOR_CLOSURE_MAPPING
            row[0] = REASON_FOR_CLOSURE_MAPPING[row[0]]
            percent = self.percent_fn(total, row[1]['html'])
            row.append({'sort_key':percent, 'html': percent})
        return rows

    @property
    def filters(self):
        filter = super(ClosedMotherCasesBreakdown, self).filters
        filter.append(NOTEQ('reason_for_mother_closure', 'empty'))
        return filter

    @property
    def headers(self):
        return DataTablesHeader(*[DataTablesColumn('Reason for closure'), DataTablesColumn('Number'), DataTablesColumn('Percentage')])

    @property
    def columns(self):
        return [
            DatabaseColumn("Reason for closure", SimpleColumn('reason_for_mother_closure')),
            DatabaseColumn("Number", CountUniqueColumn('doc_id'))
        ]

class PregnantMotherBreakdownByTrimester(BaseSqlData):
    table_name = "fluff_WorldVisionMotherFluff"
    slug = 'pregnant_mother_by_trimester'
    title = 'Pregnant Woman Breakdown by Trimester'
    show_total = True
    total_row_name = "Total pregnant "
    show_charts = True
    chart_x_label = ''
    chart_y_label = ''


    def percent_fn(self, y):
        x = self.data['trimester_1'] + self.data['trimester_2'] + self.data['trimester_3']
        return "%(p).2f%%" % \
            {
                "p": (100 * int(y or 0) / (x or 1))
            }

    @property
    def filters(self):
        filter = super(PregnantMotherBreakdownByTrimester, self).filters
        filter.append(EQ('mother_state', 'pregnant_mother_type'))
        return filter

    @property
    def rows(self):
        result = []
        for column in self.columns:
            percent = self.percent_fn(self.data[column.slug])
            result.append([{'sort_key': column.header, 'html': column.header},
                           {'sort_key': self.data[column.slug], 'html': self.data[column.slug]},
                           {'sort_key': 'percentage', 'html': percent}]
            )
        return result

    @property
    def columns(self):
        return [
            DatabaseColumn("Trimester 1",
                CountUniqueColumn('doc_id',
                    alias="trimester_1",
                    filters=self.filters + [AND([LTE('lmp', "today"), GTE('lmp', "first_trimester_start_date"), NOTEQ('lmp', 'empty')])]
                )
            ),
            DatabaseColumn("Trimester 2",
                CountUniqueColumn('doc_id',
                    alias="trimester_2",
                    filters=self.filters + [AND([LTE('lmp', "second_trimester_start_date"), GTE('lmp', "second_trimester_end_date"), NOTEQ('lmp', 'empty')])]
                )
            ),
            DatabaseColumn("Trimester 3",
                CountUniqueColumn('doc_id',
                    alias="trimester_3",
                    filters=self.filters + [AND([LTE('lmp', 'third_trimester_start_date'), NOTEQ('lmp', 'empty')])]
                )
            )
        ]

class AnteNatalCareServiceOverview(BaseSqlData):
    table_name = "fluff_WorldVisionMotherFluff"
    slug = 'ante_natal_care_service_overview'
    title = 'Ante Natal Care Service Overview'

    @property
    def filters(self):
        filter = super(AnteNatalCareServiceOverview, self).filters
        filter.append(EQ('mother_state', 'pregnant_mother_type'))
        return filter

    @property
    def headers(self):
        return DataTablesHeader(*[DataTablesColumn('Entity'), DataTablesColumn('Number'),
                                  DataTablesColumn('Total Eligible'), DataTablesColumn('Percentage')])

    @property
    def rows(self):
        result = [[{'sort_key': self.columns[0].header, 'html': self.columns[0].header},
                  {'sort_key': self.data[self.columns[0].slug], 'html': self.data[self.columns[0].slug]},
                  {'sort_key': 'n/a', 'html': 'n/a'},
                  {'sort_key': 'n/a', 'html': 'n/a'}]]
        for i in range(1,5):
            result.append([{'sort_key': self.columns[i].header, 'html': self.columns[i].header},
                           {'sort_key': self.data[self.columns[i].slug], 'html': self.data[self.columns[i].slug]},
                           {'sort_key': self.data[self.columns[i + 4].slug], 'html': self.data[self.columns[i + 4].slug]},
                           {'sort_key': self.percent_fn(self.data[self.columns[i + 4].slug], self.data[self.columns[i].slug]),
                            'html': self.percent_fn(self.data[self.columns[i + 4].slug], self.data[self.columns[i].slug])}])
        return result

    @property
    def columns(self):
        return [

            DatabaseColumn("Total pregnant",
                CountUniqueColumn('doc_id', alias="total_pregnant"),
            ),
            DatabaseColumn("ANC3",
                CountUniqueColumn('doc_id', alias="anc_3", filters=self.filters + [EQ('anc_3', 'yes')]),
            ),
            DatabaseColumn("TT Completed (TT2 or Booster)",
                CountUniqueColumn('doc_id', alias="tt_completed",
                                  filters=self.filters + [OR([EQ('tt_2', 'yes'), EQ('tt_booster', 'yes')])]),
            ),
            DatabaseColumn("Taking IFA tablets",
                CountUniqueColumn('doc_id', alias="ifa_tablets", filters=self.filters + [EQ('iron_folic', 'yes')]),
            ),
            DatabaseColumn("Completed 100 IFA tablets",
                CountUniqueColumn('doc_id', alias="100_tablets", filters=self.filters + [EQ('completed_100_ifa', 'yes')]),
            ),
            DatabaseColumn("ANC3 Total Eligible",
                CountUniqueColumn('doc_id', alias="anc_3_eligible",
                                  filters=self.filters + [AND([EQ('anc_2', 'yes'), LTE('lmp', 'days_224')])]),
            ),
            DatabaseColumn("TT Completed (TT2 or Booster) Total Eligible",
                CountUniqueColumn('doc_id', alias="tt_completed_eligible",
                                  filters=self.filters + [OR([EQ('tt_1', 'yes'), EQ('previous_tetanus', 'yes')])]),
            ),
            DatabaseColumn("Taking IFA tablets Total Eligible",
                CountUniqueColumn('doc_id', alias="ifa_tablets_eligible", filters=self.filters + [EQ('iron_folic', 'yes')]),
            ),
            DatabaseColumn("Completed 100 IFA tablets Total Eligible",
                CountUniqueColumn('doc_id', alias="100_tablets_eligible", filters=self.filters + [LTE('lmp', 'days_195')]),
            )
        ]
