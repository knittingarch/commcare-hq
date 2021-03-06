import datetime
from cStringIO import StringIO
from dimagi.utils.csv import UnicodeWriter
from collections import defaultdict, namedtuple
import pytz

from celery import group
from celery.task import periodic_task, task
from celery.schedules import crontab
from celery.utils.log import get_task_logger
from django.conf import settings
from django.utils.dateparse import parse_datetime, parse_date
from soil import MultipleTaskDownload

from corehq import toggles
from corehq.apps.hqcase.utils import update_case, bulk_update_cases
from corehq.apps.fixtures.models import FixtureDataItem
from corehq.form_processor.interfaces.dbaccessors import CaseAccessors
from corehq.form_processor.models import CommCareCaseSQL
from corehq.sql_db.util import get_db_aliases_for_partitioned_query
from corehq.util.soft_assert import soft_assert
from dimagi.utils.decorators.memoized import memoized
from dimagi.utils.couch.cache.cache_core import get_redis_client
from casexml.apps.case.const import ARCHIVED_CASE_OWNER_ID
from corehq.apps.hqwebapp.tasks import send_html_email_async

from .case_utils import (
    CASE_TYPE_EPISODE,
    get_prescription_vouchers_from_episode,
    get_private_diagnostic_test_cases_from_episode,
    get_prescription_from_voucher,
    get_person_case_from_episode,
)
from custom.enikshay.exceptions import ENikshayCaseNotFound
from .const import (
    DOSE_MISSED,
    DOSE_TAKEN_INDICATORS,
    DATE_FULFILLED,
    DAILY_SCHEDULE_FIXTURE_NAME,
    DAILY_SCHEDULE_ID,
    SCHEDULE_ID_FIXTURE,
    HISTORICAL_CLOSURE_REASON,
    ENIKSHAY_TIMEZONE,
    VALID_ADHERENCE_SOURCES,
    BETS_DATE_PRESCRIPTION_THRESHOLD_MET,
    FDC_PRESCRIPTION_DAYS_THRESHOLD,
    NON_FDC_PRESCRIPTION_DAYS_THRESHOLD,
)
from .exceptions import EnikshayTaskException
from .data_store import AdherenceDatastore


logger = get_task_logger(__name__)

DoseStatus = namedtuple('DoseStatus', 'taken missed unknown source')
BatchStatus = namedtuple('BatchStatus', 'update_count noupdate_count success_count errors case_batches duration')

CACHE_KEY = "reconciliation-task-{}"
cache = get_redis_client()


@periodic_task(
    bind=True,
    run_every=crontab(hour=0, minute=0),  # every day at midnight
    queue=getattr(settings, 'ENIKSHAY_QUEUE', 'celery')
)
def enikshay_task(self):
    # runs adherence and voucher calculations for all domains that have
    # `toggles.UATBC_ADHERENCE_TASK` enabled

    domains = toggles.UATBC_ADHERENCE_TASK.get_enabled_domains()
    for domain in domains:
        if toggles.DATA_MIGRATION.enabled(domain):
            # Don't run this on the india cluster anymore
            continue

        try:
            task_group = EpisodeUpdater(domain).run()
        except Exception as e:
            logger.error("error calculating reconciliation task for domain {}: {}".format(domain, e))

        download = MultipleTaskDownload()
        download.set_task(task_group)
        download.save()
        cache.set(CACHE_KEY.format(domain), download.download_id)

        send_status_email(domain, task_group.get())


@task
def run_task(updater, case_ids):
    return updater.run_batch(case_ids)


class Timer:
    def __enter__(self):
        self.start = datetime.datetime.now()
        self.end = None
        return self

    @property
    def interval(self):
        if self.end is None:
            return datetime.datetime.now() - self.start
        else:
            return self.end - self.start

    def __exit__(self, *args):
        self.end = datetime.datetime.now()


class EpisodeUpdater(object):
    """
    This iterates over all open 'episode' cases and sets 'adherence' and 'voucher' related properties
    This is applicable to various enikshay domains. The domain can be specified in __init__ method
    """

    def __init__(self, domain, task_id=None):
        self.domain = domain
        self.task_id = task_id
        self.updaters = [
            EpisodeAdherenceUpdate,
            EpisodeVoucherUpdate,
            EpisodeTestUpdate,
        ]

    def run(self):
        """Kicks off multiple tasks with a batch of case_ids for each partition
        """
        tasks = []
        for case_ids in self._get_case_id_batches():
            tasks.append(run_task.s(self, case_ids))
        return group(tasks)()

    def _get_case_id_batches(self):
        dbs = get_db_aliases_for_partitioned_query()
        for db in dbs:
            case_ids = (
                CommCareCaseSQL.objects
                .using(db)
                .filter(domain=self.domain, type=CASE_TYPE_EPISODE, closed=False, deleted=False)
                .values_list('case_id', flat=True)
            )
            yield case_ids

    def run_batch(self, case_ids):
        """Run all case updaters against the case_ids passed in
        """
        device_id = "%s.%s" % (__name__, type(self).__name__)
        update_count = 0
        noupdate_count = 0
        error_count = 0
        success_count = 0
        case_batches = 0

        errors = []
        with Timer() as t:
            batch_size = 100
            updates = []
            for episode in self._get_open_episode_cases(case_ids):
                did_error = False
                update_json = {}
                for updater in self.updaters:
                    try:
                        potential_update = updater(self.domain, episode).update_json()
                        update_json.update(get_updated_fields(episode.dynamic_case_properties(), potential_update))
                    except Exception as e:
                        did_error = True
                        error = [episode.case_id, episode.domain, updater.__name__, e]
                        errors.append(error)
                        logger.error(error)
                if did_error:
                    error_count += 1
                else:
                    success_count += 1

                if update_json:
                    updates.append((episode.case_id, update_json, False))
                    update_count += 1
                else:
                    noupdate_count += 1
                if len(updates) >= batch_size:
                    bulk_update_cases(self.domain, updates, device_id)
                    updates = []
                    case_batches += 1

            if len(updates) > 0:
                bulk_update_cases(self.domain, updates, device_id)

        return BatchStatus(update_count, noupdate_count, success_count, errors, case_batches, t.interval)

    def update_single_case(self, episode_case):
        # updates a single episode_case.
        assert episode_case.domain == self.domain
        update_json = EpisodeAdherenceUpdate(self.domain, episode_case).update_json()
        if update_json:
            update_case(self.domain, episode_case.case_id, update_json,
                        device_id="%s.%s" % (__name__, type(self).__name__))

    def _get_open_episode_cases(self, case_ids):
        case_accessor = CaseAccessors(self.domain)
        episode_cases = case_accessor.iter_cases(case_ids)
        for episode_case in episode_cases:
            # if this episode is part of a deleted or archived person, don't update
            try:
                person_case = get_person_case_from_episode(self.domain, episode_case.case_id)
            except ENikshayCaseNotFound:
                continue

            if person_case.owner_id == ARCHIVED_CASE_OWNER_ID:
                continue

            if person_case.closed:
                continue

            yield episode_case


def send_status_email(domain, async_result):
    errors = []
    duration = datetime.timedelta()
    updates = 0
    noupdates = 0
    for batch_info in async_result:
        errors += batch_info.errors
        duration += batch_info.duration
        updates += batch_info.update_count
        noupdates += batch_info.noupdate_count

    subject = "eNikshay Episode Task results for: {}".format(datetime.date.today())
    recipient = "{}@{}.{}".format('commcarehq-ops+admins', 'dimagi', 'com')
    cc = "{}@{}.{}".format('frener', 'dimagi', 'com')

    csv_file = StringIO()
    writer = UnicodeWriter(csv_file)
    writer.writerow(['Episode ID', 'Domain', 'Updater Class', 'Error'])
    writer.writerows(errors)

    message = (
        "Summary of enikshay_task: domain: {domain}, duration (sec): {duration} "
        "Cases Updated {updates}, cases errored {errors} and {noupdates} "
        "cases didn't need update. ".format(
            domain=domain, duration=duration, updates=updates, errors=len(errors),
            noupdates=noupdates)
    )
    attachment = {
        'title': "failed_episodes_{}.csv".format(datetime.date.today()),
        'mimetype': 'text/csv',
        'file_obj': csv_file,
    }
    send_html_email_async(
        subject, recipient, message, cc=[cc], text_content=message, file_attachments=[attachment]
    )


@memoized
def get_datastore(domain):
    return AdherenceDatastore(domain)


@memoized
def get_itemlist(domain):
    return FixtureDataItem.get_item_list(domain, DAILY_SCHEDULE_FIXTURE_NAME)


class EpisodeAdherenceUpdate(object):
    """
    Class to capture adherence related calculations specific to an 'episode' case
    per the spec https://docs.google.com/document/d/1FjSdLYOYUCRBuW3aSxvu3Z5kvcN6JKbpDFDToCgead8/edit
    """
    def __init__(self, domain, episode_case):
        self.domain = domain
        self.episode = episode_case
        self.adherence_data_store = get_datastore(self.domain)
        # set purge_date to 30 days back
        self.purge_date = datetime.datetime.now(
            pytz.timezone(ENIKSHAY_TIMEZONE)).date() - datetime.timedelta(days=30)
        self.date_today_in_india = datetime.datetime.now(pytz.timezone(ENIKSHAY_TIMEZONE)).date()

        self._cache_dose_taken_by_date = False

    @memoized
    def get_doses_data(self):
        # return 'doses_per_week' by 'schedule_id' from the Fixture data
        fixtures = get_itemlist(self.domain)
        doses_per_week_by_schedule_id = {}
        for f in fixtures:
            schedule_id = f.fields[SCHEDULE_ID_FIXTURE].field_list[0].field_value
            doses_per_week = int(f.fields["doses_per_week"].field_list[0].field_value)
            doses_per_week_by_schedule_id[schedule_id] = doses_per_week
        return doses_per_week_by_schedule_id

    @memoized
    def get_valid_adherence_cases(self):
        # Returns list of 'adherence' cases of which 'adherence_value' is one of DOSE_KNOWN_INDICATORS
        return self.adherence_data_store.dose_known_adherences(
            self.episode.case_id
        )

    def get_latest_adherence_date(self):
        """
        return open case of type 'adherence' reverse-indexed to episode that
            has the latest 'adherence_date' property of all
        """
        return self.adherence_data_store.latest_adherence_date(
            self.episode.case_id
        )

    def get_adherence_schedule_start_date(self):
        # return property 'adherence_schedule_date_start' of episode case (is expected to be a date object)
        raw_date = self.episode.get_case_property('adherence_schedule_date_start')
        if not raw_date:
            return None
        elif parse_date(raw_date):
            return parse_date(raw_date)
        else:
            raise EnikshayTaskException(
                "Episode case {case_id} has invalid format for 'adherence_schedule_date_start' {date}".format(
                    case_id=self.episode.case_id,
                    date=raw_date
                )
            )
            return None

    @staticmethod
    def count_doses_taken(dose_status_by_date, start_date=None, end_date=None):
        """
        Args:
            dose_status_by_date: result of calculate_dose_status_by_day
        Returns:
            total count of adherence_cases excluding duplicates on a given day. If there are
            two adherence_cases on one day at different time, it will be counted as one
        """
        return EpisodeAdherenceUpdate.count_doses_of_type('taken', dose_status_by_date, start_date, end_date)

    @staticmethod
    def count_doses_of_type(dose_type, dose_status_by_date, start_date=None, end_date=None):
        """dose_type should be 'taken', 'unknown', or 'missed'"""
        if bool(start_date) != bool(end_date):
            raise EnikshayTaskException("Both of start_date and end_date should be specified or niether of them")

        if not start_date:
            return len([status for status in dose_status_by_date.values() if getattr(status, dose_type)])
        else:
            return len([
                status
                for date, status in dose_status_by_date.iteritems()
                if start_date <= date <= end_date and getattr(status, dose_type)
            ])

    @staticmethod
    def count_doses_taken_by_source(dose_status_by_date, start_date=None, end_date=None):
        """Count all sources of adherence and return the count within the desired timeframe

        {'99DOTS': 1, 'MERM': 1, 'treatment_supervisor': 0, ... }
        """
        counts = defaultdict(int)
        for date, status in dose_status_by_date.iteritems():
            if status.source in VALID_ADHERENCE_SOURCES:
                if start_date and end_date and start_date <= date <= end_date:
                    counts[status.source] += 1
                elif not start_date and not end_date:
                    counts[status.source] += 1
        return counts

    def update_json(self):
        debug_data = []
        adherence_schedule_date_start = self.get_adherence_schedule_start_date()
        debug_data.append("adherence_schedule_date_start: {}".format(adherence_schedule_date_start))
        debug_data.append("purge_date: {}".format(self.purge_date))

        if not adherence_schedule_date_start:
            # adherence schedule hasn't been selected, so no update necessary
            return {}

        latest_adherence_date = self.get_latest_adherence_date()
        debug_data.append("latest_adherence_date: {}".format(latest_adherence_date))

        if (adherence_schedule_date_start > self.purge_date) or not latest_adherence_date:
            return self.check_and_return({
                'aggregated_score_date_calculated': adherence_schedule_date_start - datetime.timedelta(days=1),
                'expected_doses_taken': 0,
                'aggregated_score_count_taken': 0,
                'adherence_total_doses_taken': 0,
                'adherence_latest_date_recorded': adherence_schedule_date_start - datetime.timedelta(days=1),
                'one_week_score_count_taken': 0,
                'two_week_score_count_taken': 0,
                'month_score_count_taken': 0,
                'one_week_adherence_score': 0,
                'two_week_adherence_score': 0,
                'month_adherence_score': 0,
            })

        adherence_cases = self.get_valid_adherence_cases()
        dose_status_by_date = calculate_dose_status_by_day(adherence_cases)
        update = self.get_aggregated_scores(
            latest_adherence_date, adherence_schedule_date_start, dose_status_by_date)
        update.update(self.get_adherence_scores(dose_status_by_date))

        return self.check_and_return(update)

    def get_adherence_scores(self, dose_status_by_date):
        """
        https://docs.google.com/document/d/1lTGiz28REKKgAP4yPe7jKHEEd0y8wldjfYONVH_Uli0/edit#
        https://docs.google.com/document/d/1TG9YWSdccgKeKj0mVIAsoq9LthcZfw5_OebSrkYCF3A/edit#
        """
        readable_day_names = {
            3: 'three_day',
            7: 'one_week',
            14: 'two_week',
            30: 'month',
        }
        today = self.date_today_in_india
        start_date = self.get_adherence_schedule_start_date()

        properties = {}
        for num_days, day_name in readable_day_names.iteritems():
            if today - datetime.timedelta(days=num_days) >= start_date:
                start = today - datetime.timedelta(days=num_days)
                end = today
                score_count_taken = self.count_doses_taken(
                    dose_status_by_date,
                    start_date=start,
                    end_date=end,
                )
                doses_taken_by_source = self.count_doses_taken_by_source(
                    dose_status_by_date,
                    start_date=start,
                    end_date=end,
                )
                missed_count = self.count_doses_of_type('missed', dose_status_by_date, start, end)
                unknown_count = num_days - missed_count - score_count_taken
            else:
                score_count_taken = 0
                doses_taken_by_source = {source: 0 for source in VALID_ADHERENCE_SOURCES}
                missed_count = 0
                unknown_count = 0

            properties["{}_score_count_taken".format(day_name)] = score_count_taken
            properties["{}_adherence_score".format(day_name)] = self._percentage_score(score_count_taken, num_days)
            properties["{}_missed_count".format(day_name)] = missed_count
            properties["{}_missed_score".format(day_name)] = self._percentage_score(missed_count, num_days)
            properties["{}_unknown_count".format(day_name)] = unknown_count
            properties["{}_unknown_score".format(day_name)] = self._percentage_score(unknown_count, num_days)
            for source in VALID_ADHERENCE_SOURCES:
                properties["{}_score_count_taken_{}".format(day_name, source)] = doses_taken_by_source[source]
                properties["{}_adherence_score_{}".format(day_name, source)] = self._percentage_score(
                    doses_taken_by_source[source], num_days)

        return properties

    def _percentage_score(self, score, num_days):
        return round(score / float(num_days) * 100, 2)

    def get_aggregated_scores(self, latest_adherence_date, adherence_schedule_date_start, dose_status_by_date):
        """
        Evaluates adherence calculations on the 'episode' case and returns dict of values

        Returns:
            If no update is necessary, empty dict is returned, if not, dict with following
            keys is returned

            {
                'aggregated_score_date_calculated': value,
                'expected_doses_taken': value,
                'aggregated_score_count_taken': value
            }
        """
        update = {}

        update["adherence_latest_date_recorded"] = latest_adherence_date
        if latest_adherence_date < self.purge_date:
            update["aggregated_score_date_calculated"] = latest_adherence_date
        else:
            update["aggregated_score_date_calculated"] = self.purge_date

        # calculate 'adherence_total_doses_taken'
        update["adherence_total_doses_taken"] = self.count_doses_taken(dose_status_by_date)
        # calculate 'aggregated_score_count_taken'
        update["aggregated_score_count_taken"] = self.count_doses_taken(
            dose_status_by_date,
            start_date=adherence_schedule_date_start,
            end_date=update["aggregated_score_date_calculated"]
        )

        # calculate 'expected_doses_taken' score
        dose_data = self.get_doses_data()
        adherence_schedule_id = self.episode.get_case_property('adherence_schedule_id') or DAILY_SCHEDULE_ID
        doses_per_week = dose_data.get(adherence_schedule_id)
        if doses_per_week:
            update['expected_doses_taken'] = int(((
                (update['aggregated_score_date_calculated'] - adherence_schedule_date_start)).days / 7.0
            ) * doses_per_week)
        else:
            update['expected_doses_taken'] = 0
            soft_assert(notify_admins=True)(
                True,
                "No fixture item found with schedule_id {}".format(adherence_schedule_id)
            )
        total_expected_doses_taken = ((datetime.date.today() - adherence_schedule_date_start).days / 7.0)\
                                     * doses_per_week
        update['total_expected_doses_taken'] = total_expected_doses_taken

        return update

    def check_and_return(self, update_dict):
        """
        Args:
            update_dict: dict of case property name to values

        Returns:
            Checks if any one of update_dict is not set on self.episode case,
                if any of them are not returns update_dict after formatting any
                date values to string.
                If all of them are set as expected, returns None
        """
        needs_update = any([
            self.episode.get_case_property(k) != v
            for (k, v) in update_dict.iteritems()
        ])
        if needs_update:
            return update_dict
        else:
            return None


class EpisodeVoucherUpdate(object):
    """
    Class to capture voucher related calculations specific to an 'episode' case
    """
    def __init__(self, domain, episode_case):
        """
        Args:
            episode_case: An 'episode' case object
        """
        self.domain = domain
        self.episode = episode_case

    @staticmethod
    def _get_fulfilled_voucher_date(voucher):
        return voucher.get_case_property('date_fulfilled')

    @memoized
    def _get_all_vouchers(self):
        return get_prescription_vouchers_from_episode(self.domain, self.episode.case_id)

    def _get_fulfilled_vouchers(self):
        relevant_vouchers = [
            voucher for voucher in self._get_all_vouchers()
            if (voucher.get_case_property('voucher_type') == 'prescription'
                and voucher.get_case_property(DATE_FULFILLED))
        ]
        return sorted(relevant_vouchers, key=self._get_fulfilled_voucher_date)

    def _get_fulfilled_available_vouchers(self):
        relevant_vouchers = [
            voucher for voucher in self._get_all_vouchers()
            if (voucher.get_case_property('voucher_type') == 'prescription'
                and voucher.get_case_property('state') in
                ['fulfilled', 'available', 'paid', 'approved', 'rejected'])
        ]
        return sorted(relevant_vouchers, key=lambda v: v.get_case_property('date_issued'))

    def update_json(self):
        output_json = {}
        output_json.update(self.get_prescription_total_days())
        output_json.update(self.get_prescription_refill_due_dates())
        output_json.update(self.get_first_voucher_details())
        return get_updated_fields(self.episode.dynamic_case_properties(), output_json)

    def get_prescription_total_days(self):
        prescription_json = {}
        threshold = (FDC_PRESCRIPTION_DAYS_THRESHOLD
                     if self.episode.get_case_property("treatment_options") == "fdc"
                     else NON_FDC_PRESCRIPTION_DAYS_THRESHOLD)
        threshold_already_met = False
        total_days = 0
        for voucher in self._get_fulfilled_vouchers():
            raw_days_value = voucher.get_case_property('final_prescription_num_days')
            total_days += int(raw_days_value) if raw_days_value else 0

            for num_days in (30, 60, 90, 120,):
                prop = "prescription_total_days_threshold_{}".format(num_days)
                if total_days >= num_days and prop not in prescription_json:
                    prescription_json[prop] = self._get_fulfilled_voucher_date(voucher)

            if total_days >= threshold and not threshold_already_met:
                prescription_json[BETS_DATE_PRESCRIPTION_THRESHOLD_MET] = \
                    self._get_fulfilled_voucher_date(voucher)
                threshold_already_met = True

        prescription_json['prescription_total_days'] = total_days
        return prescription_json

    def get_prescription_refill_due_dates(self):
        """The dates on which the app predicts that the episode should be eligible for prescription refills

        https://docs.google.com/document/d/1s1-MHKS5I8cvf0_7NvcqeTsYGsXLy9YGArw2L7cZSRM/edit#
        """
        fulfilled_available_vouchers = self._get_fulfilled_available_vouchers()
        if not fulfilled_available_vouchers:
            return {}

        latest_voucher = fulfilled_available_vouchers[-1]

        date_last_refill = parse_date(latest_voucher.get_case_property('date_issued'))
        if date_last_refill is None:
            return {}

        voucher_length = (
            latest_voucher.get_case_property('final_prescription_num_days')
            or latest_voucher.get_case_property('prescription_num_days')
        )

        try:
            refill_due_date = date_last_refill + datetime.timedelta(days=int(voucher_length))
        except (TypeError, ValueError):
            return {}

        return {
            u'date_last_refill': date_last_refill.strftime("%Y-%m-%d"),
            u'voucher_length': voucher_length,
            u'refill_due_date': refill_due_date.strftime("%Y-%m-%d"),
        }

    def get_first_voucher_details(self):
        all_voucher_cases = sorted(self._get_all_vouchers(), key=lambda c: c.get_case_property('date_issued'))
        fulfilled_voucher_cases = sorted(
            self._get_fulfilled_vouchers(),
            key=lambda c: c.get_case_property('date_fulfilled')
        )

        try:
            first_voucher_generated = all_voucher_cases[0]
        except IndexError:
            return {}

        try:
            first_prescription = get_prescription_from_voucher(self.domain, first_voucher_generated.case_id)
        except ENikshayCaseNotFound:
            return {}

        return {
            u'first_voucher_generation_date': first_voucher_generated.get_case_property('date_issued'),
            u'first_voucher_drugs': first_prescription.get_case_property('drugs_ordered_readable'),
            u'first_voucher_validation_date': (fulfilled_voucher_cases[0].get_case_property('date_fulfilled')
                                              if fulfilled_voucher_cases else '')
        }


class EpisodeTestUpdate(object):

    def __init__(self, domain, episode_case):
        self.domain = domain
        self.episode = episode_case

    @property
    @memoized
    def diagnostic_tests(self):
        try:
            return get_private_diagnostic_test_cases_from_episode(self.domain, self.episode.case_id)
        except ENikshayCaseNotFound:
            return None

    def update_json(self):
        if self.diagnostic_tests:
            return {
                u'diagnostic_tests': ", ".join([self._get_diagnostic_test_name(diagnostic_test)
                                               for diagnostic_test in self.diagnostic_tests]),
                u'diagnostic_test_results': ", ".join([diagnostic_test.get_case_property('result_grade')
                                                      for diagnostic_test in self.diagnostic_tests])
            }
        else:
            return {}

    def _get_diagnostic_test_name(self, diagnostic_test):
        site_specimen_name = diagnostic_test.get_case_property('site_specimen_name')
        if site_specimen_name:
            return u"{}: {}".format(
                diagnostic_test.get_case_property('investigation_type_name'), site_specimen_name)
        else:
            return diagnostic_test.get_case_property('investigation_type_name')


def calculate_dose_status_by_day(adherence_cases):
    """
    adherence_cases: list of 'adherence' case dicts that come from elasticsearch
    Returns: {day: DoseStatus(taken, missed, unknown, source)}
    """

    adherence_cases_by_date = defaultdict(list)
    for case in adherence_cases:
        adherence_date = parse_date(case['adherence_date']) or parse_datetime(case['adherence_date']).date()
        adherence_cases_by_date[adherence_date].append(case)

    status_by_day = defaultdict(lambda: DoseStatus(taken=False, missed=False, unknown=True, source=False))
    for day, cases in adherence_cases_by_date.iteritems():
        case = _get_relevent_case(cases)
        if not case:
            pass  # unknown
        elif case.get('adherence_value') in DOSE_TAKEN_INDICATORS:
            source = case.get('adherence_report_source') or case.get('adherence_source')
            status_by_day[day] = DoseStatus(taken=True, missed=False, unknown=False, source=source)
        elif case.get('adherence_value') == DOSE_MISSED:
            status_by_day[day] = DoseStatus(taken=False, missed=True, unknown=False, source=False)
        else:
            pass  # unknown
    return status_by_day


def _get_relevent_case(cases):
    """
    If there are multiple cases on one day filter to a single case as per below
    1. Find most relevant case
        if only non-enikshay source cases
            consider the case with latest_modified - irrespective of case is closed/open

        if only enikshay source cases or if mix of enikshay and non-enikshay source cases
            consider only enikshay cases
            filter by '(closed and closure_reason == HISTORICAL_CLOSURE_REASON) or open'
            consider the case with latest modified after above filter
    """
    sources = {case["adherence_source"] for case in cases}
    if 'enikshay' not in sources:
        valid_cases = cases
    else:
        valid_cases = filter(
            lambda case: (
                case.get('adherence_source') == 'enikshay' and
                (not case['closed'] or (case['closed'] and
                    case.get('adherence_closure_reason') == HISTORICAL_CLOSURE_REASON))
            ),
            cases
        )
    if valid_cases:
        by_modified_on = sorted(valid_cases, key=lambda case: case['modified_on'])
        latest_case = by_modified_on[-1]
        return latest_case
    return None


def get_updated_fields(existing_properties, new_properties):
    updated_fields = {}
    for prop, value in new_properties.items():
        existing_value = unicode(existing_properties.get(prop, '--'))
        new_value = unicode(value) if value is not None else u""
        if existing_value != new_value:
            updated_fields[prop] = value
    return updated_fields
