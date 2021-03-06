import jsonfield
import pytz
import re
from collections import defaultdict

from casexml.apps.case.models import CommCareCase
from casexml.apps.case.xform import get_case_updates
from corehq.apps.app_manager.dbaccessors import get_latest_released_app
from corehq.apps.app_manager.exceptions import FormNotFoundException
from corehq.apps.app_manager.models import AdvancedForm
from corehq.apps.es.cases import CaseES
from corehq.apps.users.util import SYSTEM_USER_ID
from corehq.form_processor.abstract_models import DEFAULT_PARENT_IDENTIFIER
from corehq.form_processor.interfaces.dbaccessors import FormAccessors
from corehq.form_processor.exceptions import CaseNotFound
from corehq.form_processor.models import CommCareCaseSQL
from corehq.messaging.scheduling.const import (
    VISIT_WINDOW_START,
    VISIT_WINDOW_END,
    VISIT_WINDOW_DUE_DATE,
)
from corehq.form_processor.utils.general import should_use_sql_backend
from corehq.messaging.scheduling.models import AlertSchedule, TimedSchedule
from corehq.messaging.scheduling.tasks import (
    refresh_case_alert_schedule_instances,
    refresh_case_timed_schedule_instances,
)
from corehq.messaging.scheduling.scheduling_partitioned.dbaccessors import (
    get_case_alert_schedule_instances_for_schedule_id,
    get_case_timed_schedule_instances_for_schedule_id,
)
from corehq.sql_db.util import run_query_across_partitioned_databases
from corehq.util.log import with_progress_bar
from corehq.util.quickcache import quickcache
from corehq.util.test_utils import unit_testing_only
from couchdbkit.exceptions import ResourceNotFound
from datetime import date, datetime, time, timedelta
from dateutil.parser import parse
from dimagi.utils.chunked import chunked
from dimagi.utils.couch import CriticalSection
from dimagi.utils.decorators.memoized import memoized
from dimagi.utils.logging import notify_exception
from dimagi.utils.modules import to_function
from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from corehq.apps.hqcase.utils import update_case
from corehq.form_processor.models import CommCareCaseSQL, CommCareCaseIndexSQL
from django.utils.translation import ugettext_lazy
from jsonobject.api import JsonObject
from jsonobject.properties import StringProperty, BooleanProperty, IntegerProperty

ALLOWED_DATE_REGEX = re.compile('^\d{4}-\d{2}-\d{2}')
AUTO_UPDATE_XMLNS = 'http://commcarehq.org/hq_case_update_rule'


def _try_date_conversion(date_or_string):
    if (
        isinstance(date_or_string, basestring) and
        ALLOWED_DATE_REGEX.match(date_or_string)
    ):
        date_or_string = parse(date_or_string)

    return date_or_string


class AutomaticUpdateRule(models.Model):
    # Used when the rule performs case update actions
    WORKFLOW_CASE_UPDATE = 'CASE_UPDATE'

    # Used when the rule spawns schedule instances in the scheduling framework
    WORKFLOW_SCHEDULING = 'SCHEDULING'

    domain = models.CharField(max_length=126, db_index=True)
    name = models.CharField(max_length=126)
    case_type = models.CharField(max_length=126)
    active = models.BooleanField(default=False)
    deleted = models.BooleanField(default=False)
    last_run = models.DateTimeField(null=True)
    filter_on_server_modified = models.BooleanField(default=True)

    # For performance reasons, the server_modified_boundary is a
    # required part of the criteria and should be set to the minimum
    # number of days old that a case's server_modified_on date must be
    # before we run the rule against it.
    server_modified_boundary = models.IntegerField(null=True)
    migrated = models.BooleanField(default=False)

    # One of the WORKFLOW_* constants on this class describing the workflow
    # that this rule belongs to.
    workflow = models.CharField(max_length=126)

    class Meta:
        app_label = "data_interfaces"

    class MigrationError(Exception):
        pass

    class RuleError(Exception):
        pass

    def __unicode__(self):
        return unicode("rule: '{s.name}', id: {s.id}, domain: {s.domain}").format(s=self)

    def migrate(self):
        if not self.pk:
            raise ValueError("Expected model to be saved first")

        with CriticalSection(['migrate-rule-%s' % self.pk]):
            rule = AutomaticUpdateRule.objects.get(pk=self.pk)
            if not rule.migrated:
                with transaction.atomic():
                    # Migrate Criteria
                    for old_criteria in rule.automaticupdaterulecriteria_set.all():
                        property_value = old_criteria.property_value
                        if old_criteria.match_type == AutomaticUpdateRuleCriteria.MATCH_DAYS_BEFORE:
                            property_value = int(property_value) * -1
                            property_value = str(property_value)

                        new_criteria_definition = MatchPropertyDefinition(
                            property_name=old_criteria.property_name,
                            property_value=property_value,
                            match_type=old_criteria.match_type,
                        )
                        new_criteria_definition.save()

                        new_criteria = CaseRuleCriteria(rule=rule)
                        new_criteria.definition = new_criteria_definition
                        new_criteria.save()

                    # Migrate Actions
                    properties_to_update = []
                    close_case = False
                    for old_action in rule.automaticupdateaction_set.all():
                        if old_action.action == AutomaticUpdateAction.ACTION_UPDATE:
                            properties_to_update.append(
                                UpdateCaseDefinition.PropertyDefinition(
                                    name=old_action.property_name,
                                    value_type=old_action.property_value_type,
                                    value=old_action.property_value,
                                )
                            )
                        elif old_action.action == AutomaticUpdateAction.ACTION_CLOSE:
                            close_case = True
                        else:
                            raise ValueError("Unexpected action found: %s" % old_action.action)

                    new_action_definition = UpdateCaseDefinition(close_case=close_case)
                    new_action_definition.set_properties_to_update(properties_to_update)
                    new_action_definition.save()

                    new_action = CaseRuleAction(rule=rule)
                    new_action.definition = new_action_definition
                    new_action.save()

                    rule.migrated = True
                    rule.save()

            return rule

    @classmethod
    def by_domain(cls, domain, workflow, active_only=True):
        additional_filters = {}
        if active_only:
            additional_filters['active'] = True

        return cls.objects.filter(
            domain=domain,
            workflow=workflow,
            deleted=False,
            **additional_filters
        )

    @classmethod
    @quickcache(['domain', 'workflow', 'active_only'], timeout=30 * 60)
    def by_domain_cached(cls, domain, workflow, active_only=True):
        result = cls.by_domain(domain, workflow, active_only=active_only)
        result = list(result)

        for rule in result:
            # Make the criteria and actions be memoized in the cached result
            rule.memoized_criteria
            rule.memoized_actions

        return result

    @classmethod
    def organize_rules_by_case_type(cls, rules):
        rules_by_case_type = {}
        for rule in rules:
            if rule.case_type not in rules_by_case_type:
                rules_by_case_type[rule.case_type] = [rule]
            else:
                rules_by_case_type[rule.case_type].append(rule)
        return rules_by_case_type

    # returns None if any of the rules do not filter on server modified
    @classmethod
    def get_boundary_date(cls, rules, now):
        min_boundary = None
        for rule in rules:
            if not rule.filter_on_server_modified:
                return None
            elif not min_boundary:
                min_boundary = rule.server_modified_boundary
            elif rule.server_modified_boundary < min_boundary:
                min_boundary = rule.server_modified_boundary
        date = now - timedelta(days=min_boundary)
        return date

    @classmethod
    def get_case_ids(cls, domain, case_type, boundary_date=None):
        if should_use_sql_backend(domain):
            return cls._get_case_ids_from_postgres(domain, case_type, boundary_date=boundary_date)
        else:
            return cls._get_case_ids_from_es(domain, case_type, boundary_date=boundary_date)

    @classmethod
    def _get_case_ids_from_postgres(cls, domain, case_type, boundary_date=None):
        q_expression = Q(
            domain=domain,
            type=case_type,
            closed=False,
            deleted=False,
        )

        if boundary_date:
            q_expression = q_expression & Q(server_modified_on__lte=boundary_date)

        return run_query_across_partitioned_databases(CommCareCaseSQL, q_expression, values=['case_id'])

    @classmethod
    def _get_case_ids_from_es(cls, domain, case_type, boundary_date=None):
        query = (CaseES()
                 .domain(domain)
                 .case_type(case_type)
                 .is_closed(closed=False)
                 .exclude_source()
                 .size(100))

        if boundary_date:
            query = query.server_modified_range(lte=boundary_date)

        for case_id in query.scroll():
            if not isinstance(case_id, basestring):
                raise ValueError("Something is wrong with the query, expected ids only")

            yield case_id

    def activate(self, active=True):
        self.active = active
        self.save()

    def soft_delete(self):
        self.deleted = True
        self.save()

    @unit_testing_only
    def hard_delete(self):
        self.delete_criteria()
        self.delete_actions()
        CaseRuleSubmission.objects.filter(rule=self).delete()
        self.delete()

    @property
    @memoized
    def memoized_criteria(self):
        return list(self.caserulecriteria_set.all().select_related(
            'match_property_definition',
            'custom_match_definition',
            'closed_parent_definition',
        ))

    @property
    @memoized
    def memoized_actions(self):
        return list(self.caseruleaction_set.all().select_related(
            'update_case_definition',
            'custom_action_definition',
            'create_schedule_instance_definition',
        ))

    def run_rule(self, case, now):
        """
        :return: CaseRuleActionResult object aggregating the results from all actions.
        """
        if not self.migrated:
            raise self.MigrationError("Attempted to call new method on non-migrated model.")

        if self.deleted:
            raise self.RuleError("Attempted to call run_rule on a deleted rule")

        if not self.active:
            raise self.RuleError("Attempted to call run_rule on an inactive rule")

        if not isinstance(case, (CommCareCase, CommCareCaseSQL)) or case.domain != self.domain:
            raise self.RuleError("Invalid case given")

        if self.criteria_match(case, now):
            return self.run_actions_when_case_matches(case)
        else:
            return self.run_actions_when_case_does_not_match(case)

    def criteria_match(self, case, now):
        if not self.migrated:
            raise self.MigrationError("Attempted to call new method on non-migrated model.")

        if case.is_deleted or case.closed:
            return False

        if case.type != self.case_type:
            return False

        if self.filter_on_server_modified and \
                (case.server_modified_on > (now - timedelta(days=self.server_modified_boundary))):
            return False

        for criteria in self.memoized_criteria:
            try:
                result = criteria.definition.matches(case, now)
            except (CaseNotFound, ResourceNotFound):
                # This might happen if the criteria references a parent case and the
                # parent case is not found
                result = False

            if not result:
                return False

        return True

    def _run_method_on_action_definitions(self, case, method):
        if not self.migrated:
            raise self.MigrationError("Attempted to call new method on non-migrated model.")

        aggregated_result = CaseRuleActionResult()

        for action in self.memoized_actions:
            callable_method = getattr(action.definition, method)
            result = callable_method(case, self)
            if not isinstance(result, CaseRuleActionResult):
                raise TypeError("Expected CaseRuleActionResult")

            aggregated_result.add_result(result)

        return aggregated_result

    def run_actions_when_case_matches(self, case):
        return self._run_method_on_action_definitions(case, 'when_case_matches')

    def run_actions_when_case_does_not_match(self, case):
        return self._run_method_on_action_definitions(case, 'when_case_does_not_match')

    def delete_criteria(self):
        for item in self.caserulecriteria_set.all():
            item.definition.delete()

        self.caserulecriteria_set.all().delete()

    def delete_actions(self):
        for item in self.caseruleaction_set.all():
            item.definition.delete()

        self.caseruleaction_set.all().delete()

    def log_submission(self, form_id):
        CaseRuleSubmission.objects.create(
            domain=self.domain,
            rule=self,
            created_on=datetime.utcnow(),
            form_id=form_id,
        )

    def add_criteria(self, definition_class, **definition_kwargs):
        criteria = CaseRuleCriteria(rule=self)
        definition = definition_class.objects.create(**definition_kwargs)
        criteria.definition = definition
        criteria.save()
        return criteria, definition

    def add_action(self, definition_class, **definition_kwargs):
        action = CaseRuleAction(rule=self)
        definition = definition_class.objects.create(**definition_kwargs)
        action.definition = definition
        action.save()
        return action, definition

    def save(self, *args, **kwargs):
        super(AutomaticUpdateRule, self).save(*args, **kwargs)
        # If we're in a transaction.atomic() block, this gets executed after commit
        # If we're not, this gets executed right away
        transaction.on_commit(lambda: self.clear_caches(self.domain, self.workflow))

    @classmethod
    def clear_caches(cls, domain, workflow):
        # domain and workflow should never change once set
        for active_only in (True, False):
            cls.by_domain_cached.clear(
                AutomaticUpdateRule,
                domain,
                workflow,
                active_only=active_only,
            )


class CaseRuleCriteria(models.Model):
    rule = models.ForeignKey('AutomaticUpdateRule', on_delete=models.PROTECT)
    match_property_definition = models.ForeignKey('MatchPropertyDefinition', on_delete=models.CASCADE, null=True)
    custom_match_definition = models.ForeignKey('CustomMatchDefinition', on_delete=models.CASCADE, null=True)
    closed_parent_definition = models.ForeignKey('ClosedParentDefinition', on_delete=models.CASCADE, null=True)

    @property
    def definition(self):
        if self.match_property_definition_id:
            return self.match_property_definition
        elif self.custom_match_definition_id:
            return self.custom_match_definition
        elif self.closed_parent_definition_id:
            return self.closed_parent_definition
        else:
            raise ValueError("No available definition found")

    @definition.setter
    def definition(self, value):
        self.match_property_definition = None
        self.custom_match_definition = None
        self.closed_parent_definition = None

        if isinstance(value, MatchPropertyDefinition):
            self.match_property_definition = value
        elif isinstance(value, CustomMatchDefinition):
            self.custom_match_definition = value
        elif isinstance(value, ClosedParentDefinition):
            self.closed_parent_definition = value
        else:
            raise ValueError("Unexpected type found: %s" % type(value))


class CaseRuleCriteriaDefinition(models.Model):

    class Meta:
        abstract = True

    def matches(self, case, now):
        raise NotImplementedError()


class MatchPropertyDefinition(CaseRuleCriteriaDefinition):
    # True when today < (the date in property_name + property_value days)
    MATCH_DAYS_BEFORE = 'DAYS_BEFORE'

    # True when today >= (the date in property_name + property_value days)
    MATCH_DAYS_AFTER = 'DAYS'

    MATCH_EQUAL = 'EQUAL'
    MATCH_NOT_EQUAL = 'NOT_EQUAL'
    MATCH_HAS_VALUE = 'HAS_VALUE'
    MATCH_HAS_NO_VALUE = 'HAS_NO_VALUE'

    MATCH_CHOICES = (
        MATCH_DAYS_BEFORE,
        MATCH_DAYS_AFTER,
        MATCH_EQUAL,
        MATCH_NOT_EQUAL,
        MATCH_HAS_VALUE,
        MATCH_HAS_NO_VALUE,
    )

    property_name = models.CharField(max_length=126)
    property_value = models.CharField(max_length=126, null=True)
    match_type = models.CharField(max_length=15)

    def get_case_values(self, case):
        values = case.resolve_case_property(self.property_name)
        return [element.value for element in values]

    def clean_datetime(self, timestamp):
        if not isinstance(timestamp, datetime):
            timestamp = datetime.combine(timestamp, time(0, 0))

        if timestamp.tzinfo:
            # Convert to UTC and make it a naive datetime for comparison to datetime.utcnow()
            timestamp = timestamp.astimezone(pytz.utc).replace(tzinfo=None)

        return timestamp

    def check_days_before(self, case, now):
        values = self.get_case_values(case)
        for date_to_check in values:
            date_to_check = _try_date_conversion(date_to_check)

            if not isinstance(date_to_check, date):
                continue

            date_to_check = self.clean_datetime(date_to_check)

            days = int(self.property_value)
            if now < (date_to_check + timedelta(days=days)):
                return True

        return False

    def check_days_after(self, case, now):
        values = self.get_case_values(case)
        for date_to_check in values:
            date_to_check = _try_date_conversion(date_to_check)

            if not isinstance(date_to_check, date):
                continue

            date_to_check = self.clean_datetime(date_to_check)

            days = int(self.property_value)
            if now >= (date_to_check + timedelta(days=days)):
                return True

        return False

    def check_equal(self, case, now):
        return any([
            value == self.property_value for value in self.get_case_values(case)
        ])

    def check_not_equal(self, case, now):
        return any([
            value != self.property_value for value in self.get_case_values(case)
        ])

    def check_has_value(self, case, now):
        values = self.get_case_values(case)
        for value in values:
            if value is None:
                continue
            if isinstance(value, basestring) and not value.strip():
                continue
            return True

        return False

    def check_has_no_value(self, case, now):
        return not self.check_has_value(case, now)

    def matches(self, case, now):
        return {
            self.MATCH_DAYS_BEFORE: self.check_days_before,
            self.MATCH_DAYS_AFTER: self.check_days_after,
            self.MATCH_EQUAL: self.check_equal,
            self.MATCH_NOT_EQUAL: self.check_not_equal,
            self.MATCH_HAS_VALUE: self.check_has_value,
            self.MATCH_HAS_NO_VALUE: self.check_has_no_value,
        }.get(self.match_type)(case, now)


class CustomMatchDefinition(CaseRuleCriteriaDefinition):
    name = models.CharField(max_length=126)

    def matches(self, case, now):
        if self.name not in settings.AVAILABLE_CUSTOM_RULE_CRITERIA:
            raise ValueError("%s not found in AVAILABLE_CUSTOM_RULE_CRITERIA" % self.name)

        custom_function_path = settings.AVAILABLE_CUSTOM_RULE_CRITERIA[self.name]
        try:
            custom_function = to_function(custom_function_path)
        except:
            raise ValueError("Unable to resolve '%s'" % custom_function_path)

        return custom_function(case, now)


class ClosedParentDefinition(CaseRuleCriteriaDefinition):
    # This matches up to the identifier attribute in a CommCareCaseIndex
    # (couch backend) or CommCareCaseIndexSQL (postgres backend) record.
    identifier = models.CharField(max_length=126, default=DEFAULT_PARENT_IDENTIFIER)

    # This matches up to the CommCareCaseIndexSQL.relationship_id field.
    # The framework will automatically convert it to the string used in
    # the CommCareCaseIndex (couch backend) model for domains that use
    # the couch backend.
    relationship_id = models.PositiveSmallIntegerField(default=CommCareCaseIndexSQL.CHILD)

    def matches(self, case, now):
        if isinstance(case, CommCareCase):
            relationship = CommCareCase.convert_sql_relationship_id_to_couch_relationship(self.relationship_id)
        else:
            relationship = self.relationship_id

        for parent in case.get_parent(identifier=self.identifier, relationship=relationship):
            if parent.closed:
                return True

        return False


class AutomaticUpdateRuleCriteria(models.Model):
    # True when today < (the date in property_name - property_value days)
    MATCH_DAYS_BEFORE = 'DAYS_BEFORE'
    # True when today >= (the date in property_name + property_value days)
    MATCH_DAYS_AFTER = 'DAYS'
    MATCH_EQUAL = 'EQUAL'
    MATCH_NOT_EQUAL = 'NOT_EQUAL'
    MATCH_HAS_VALUE = 'HAS_VALUE'

    MATCH_TYPE_CHOICES = (
        (MATCH_DAYS_BEFORE, MATCH_DAYS_BEFORE),
        (MATCH_DAYS_AFTER, MATCH_DAYS_AFTER),
        (MATCH_EQUAL, MATCH_EQUAL),
        (MATCH_NOT_EQUAL, MATCH_NOT_EQUAL),
        (MATCH_HAS_VALUE, MATCH_HAS_VALUE),
    )

    rule = models.ForeignKey('AutomaticUpdateRule', on_delete=models.PROTECT)
    property_name = models.CharField(max_length=126)
    property_value = models.CharField(max_length=126, null=True)
    match_type = models.CharField(max_length=15)

    class Meta:
        app_label = "data_interfaces"

    def get_case_values(self, case):
        values = case.resolve_case_property(self.property_name)
        return [element.value for element in values]

    def clean_datetime(self, timestamp):
        if not isinstance(timestamp, datetime):
            timestamp = datetime.combine(timestamp, time(0, 0))

        if timestamp.tzinfo:
            # Convert to UTC and make it a naive datetime for comparison to datetime.utcnow()
            timestamp = timestamp.astimezone(pytz.utc).replace(tzinfo=None)

        return timestamp

    def check_days_before(self, case, now):
        values = self.get_case_values(case)
        for date_to_check in values:
            date_to_check = _try_date_conversion(date_to_check)

            if not isinstance(date_to_check, date):
                continue

            date_to_check = self.clean_datetime(date_to_check)

            days = int(self.property_value)
            if now < (date_to_check - timedelta(days=days)):
                return True

        return False

    def check_days_after(self, case, now):
        values = self.get_case_values(case)
        for date_to_check in values:
            date_to_check = _try_date_conversion(date_to_check)

            if not isinstance(date_to_check, date):
                continue

            date_to_check = self.clean_datetime(date_to_check)

            days = int(self.property_value)
            if now >= (date_to_check + timedelta(days=days)):
                return True

        return False

    def check_equal(self, case, now):
        return any([
            value == self.property_value for value in self.get_case_values(case)
        ])

    def check_not_equal(self, case, now):
        return any([
            value != self.property_value for value in self.get_case_values(case)
        ])

    def check_has_value(self, case, now):
        values = self.get_case_values(case)
        for value in values:
            if value is None:
                continue
            if isinstance(value, basestring) and not value.strip():
                continue
            return True

        return False

    def matches(self, case, now):
        return {
            self.MATCH_DAYS_BEFORE: self.check_days_before,
            self.MATCH_DAYS_AFTER: self.check_days_after,
            self.MATCH_EQUAL: self.check_equal,
            self.MATCH_NOT_EQUAL: self.check_not_equal,
            self.MATCH_HAS_VALUE: self.check_has_value,
        }.get(self.match_type)(case, now)


class CaseRuleAction(models.Model):
    rule = models.ForeignKey('AutomaticUpdateRule', on_delete=models.PROTECT)
    update_case_definition = models.ForeignKey('UpdateCaseDefinition', on_delete=models.CASCADE, null=True)
    custom_action_definition = models.ForeignKey('CustomActionDefinition', on_delete=models.CASCADE, null=True)
    create_schedule_instance_definition = models.ForeignKey('CreateScheduleInstanceActionDefinition',
        on_delete=models.CASCADE, null=True)

    @property
    def definition(self):
        if self.update_case_definition_id:
            return self.update_case_definition
        elif self.custom_action_definition_id:
            return self.custom_action_definition
        elif self.create_schedule_instance_definition_id:
            return self.create_schedule_instance_definition
        else:
            raise ValueError("No available definition found")

    @definition.setter
    def definition(self, value):
        self.update_case_definition = None
        self.custom_action_definition = None
        self.create_schedule_instance_definition = None

        if isinstance(value, UpdateCaseDefinition):
            self.update_case_definition = value
        elif isinstance(value, CustomActionDefinition):
            self.custom_action_definition = value
        elif isinstance(value, CreateScheduleInstanceActionDefinition):
            self.create_schedule_instance_definition = value
        else:
            raise ValueError("Unexpected type found: %s" % type(value))


class CaseRuleActionResult(object):

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def _validate_int(self, value):
        if not isinstance(value, int):
            raise ValueError("Expected int")

    def __init__(self, num_updates=0, num_closes=0, num_related_updates=0, num_related_closes=0, num_creates=0):
        self._validate_int(num_updates)
        self._validate_int(num_closes)
        self._validate_int(num_related_updates)
        self._validate_int(num_related_closes)
        self._validate_int(num_creates)

        self.num_updates = num_updates
        self.num_closes = num_closes
        self.num_related_updates = num_related_updates
        self.num_related_closes = num_related_closes
        self.num_creates = num_creates

    def add_result(self, result):
        self.num_updates += result.num_updates
        self.num_closes += result.num_closes
        self.num_related_updates += result.num_related_updates
        self.num_related_closes += result.num_related_closes
        self.num_creates += result.num_creates

    @property
    def total_updates(self):
        return (
            self.num_updates +
            self.num_closes +
            self.num_related_updates +
            self.num_related_closes +
            self.num_creates
        )


class CaseRuleActionDefinition(models.Model):

    class Meta:
        abstract = True

    def when_case_matches(self, case, rule):
        """
        Defines the actions to be taken when the case matches the rule.
        Should return an instance of CaseRuleActionResult
        """
        raise NotImplementedError()

    def when_case_does_not_match(self, case, rule):
        """
        Defines the actions to be taken when the case does not match the rule.
        This method can be optionally overriden, but by default does nothing.
        Should return an instance of CaseRuleActionResult
        """
        return CaseRuleActionResult()


class UpdateCaseDefinition(CaseRuleActionDefinition):
    # Expected to be a list of PropertyDefinition objects representing the
    # case properties to update
    properties_to_update = jsonfield.JSONField(default=list)

    # True to close the case, otherwise False
    close_case = models.BooleanField()

    VALUE_TYPE_EXACT = "EXACT"
    VALUE_TYPE_CASE_PROPERTY = "CASE_PROPERTY"

    VALUE_TYPE_CHOICES = (
        VALUE_TYPE_EXACT,
        VALUE_TYPE_CASE_PROPERTY,
    )

    class PropertyDefinition(JsonObject):
        # The case property name
        name = StringProperty()

        # The type of the value property:
        #   VALUE_TYPE_EXACT means `value` is the exact value to set to the case property referred to by `name`.
        #   VALUE_TYPE_CASE_PROPERTY means `value` is a case property to resolve first and then set to the case
        #   property referred to by `name`.
        value_type = StringProperty()

        # Meaning depends on value_type, see above
        value = StringProperty()

    def get_properties_to_update(self):
        return [self.PropertyDefinition(**fields) for fields in self.properties_to_update]

    def set_properties_to_update(self, properties):
        if not isinstance(properties, (list, tuple)):
            raise ValueError("Expected list or tuple")

        result = []
        for p in properties:
            if not isinstance(p, self.PropertyDefinition):
                raise ValueError("Expected UpdateCaseDefinition.PropertyDefinition")

            result.append(p.to_json())

        self.properties_to_update = result

    def when_case_matches(self, case, rule):
        cases_to_update = defaultdict(dict)

        def _get_case_property_value(current_case, name):
            result = current_case.resolve_case_property(name)
            if result:
                return result[0].value

            return None

        def _add_update_property(name, value, current_case):
            while True:
                if name.lower().startswith('parent/'):
                    name = name[7:]
                    # uses first parent if there are multiple
                    parent_cases = current_case.get_parent(identifier=DEFAULT_PARENT_IDENTIFIER)
                    if parent_cases:
                        current_case = parent_cases[0]
                    else:
                        return
                elif name.lower().startswith('host/'):
                    name = name[5:]
                    current_case = current_case.host
                    if not current_case:
                        return
                else:
                    break

            cases_to_update[current_case.case_id][name] = value

        for prop in self.get_properties_to_update():
            if prop.value_type == self.VALUE_TYPE_CASE_PROPERTY:
                value = _get_case_property_value(case, prop.value)
                if value is None:
                    continue
            elif prop.value_type == self.VALUE_TYPE_EXACT:
                value = prop.value
            else:
                raise ValueError("Unexpected value_type found: %s" % prop.value_type)

            if value != _get_case_property_value(case, prop.name):
                _add_update_property(prop.name, value, case)

        num_updates = 0
        num_closes = 0
        num_related_updates = 0

        # Update any referenced parent cases
        for case_id, properties in cases_to_update.items():
            if case_id == case.case_id:
                continue
            result = update_case(case.domain, case_id, case_properties=properties, close=False,
                xmlns=AUTO_UPDATE_XMLNS)

            rule.log_submission(result[0].form_id)
            num_related_updates += 1

        # Update / close the case
        properties = cases_to_update[case.case_id]
        if self.close_case or properties:
            result = update_case(case.domain, case.case_id, case_properties=properties, close=self.close_case,
                xmlns=AUTO_UPDATE_XMLNS)

            rule.log_submission(result[0].form_id)

            if properties:
                num_updates += 1

            if self.close_case:
                num_closes += 1

        return CaseRuleActionResult(
            num_updates=num_updates,
            num_closes=num_closes,
            num_related_updates=num_related_updates,
        )


class CustomActionDefinition(CaseRuleActionDefinition):
    name = models.CharField(max_length=126)

    def when_case_matches(self, case, rule):
        if self.name not in settings.AVAILABLE_CUSTOM_RULE_ACTIONS:
            raise ValueError("%s not found in AVAILABLE_CUSTOM_RULE_ACTIONS" % self.name)

        custom_function_path = settings.AVAILABLE_CUSTOM_RULE_ACTIONS[self.name]
        try:
            custom_function = to_function(custom_function_path)
        except:
            raise ValueError("Unable to resolve '%s'" % custom_function_path)

        return custom_function(case, rule)


class VisitSchedulerIntegrationHelper(object):

    class VisitSchedulerIntegrationException(Exception):
        pass

    def __init__(self, case, scheduler_module_info):
        self.case = case
        self.scheduler_module_info = scheduler_module_info

    @classmethod
    @quickcache(['domain', 'app_id', 'form_unique_id'], timeout=60 * 60)
    def get_visit_scheduler_module_and_form(cls, domain, app_id, form_unique_id):
        app = get_latest_released_app(domain, app_id)
        if app is None:
            raise cls.VisitSchedulerIntegrationException("App not found")

        try:
            form = app.get_form(form_unique_id)
        except FormNotFoundException:
            raise cls.VisitSchedulerIntegrationException("Form not found")

        if not isinstance(form, AdvancedForm):
            raise cls.VisitSchedulerIntegrationException("Expected AdvancedForm")

        if not form.schedule:
            raise cls.VisitSchedulerIntegrationException("Expected form.schedule")

        if not form.schedule.enabled:
            raise cls.VisitSchedulerIntegrationException("Expected form.schedule.enabled")

        return form.get_module(), form

    def get_visit_scheduler_form_phase(self, module):
        for i, phase in enumerate(module.schedule_phases):
            for form_reference in phase.forms:
                if form_reference.form_id == self.scheduler_module_info.form_unique_id:
                    # The indexes are 0-based, but the visit scheduler refers to them as being 1-based
                    return i + 1, phase

        raise self.VisitSchedulerIntegrationException("Schedule phase not found")

    def calculate_window_date(self, visit, visit_due_date):
        if self.scheduler_module_info.window_position == VISIT_WINDOW_START:
            return visit_due_date + timedelta(days=visit.starts)
        elif self.scheduler_module_info.window_position == VISIT_WINDOW_END:
            if not isinstance(visit.expires, int):
                raise self.VisitSchedulerIntegrationException("Cannot schedule end date of visit that does not expire")

            return visit_due_date + timedelta(days=visit.expires)
        elif self.scheduler_module_info.window_position == VISIT_WINDOW_DUE_DATE:
            return visit_due_date
        else:
            raise self.VisitSchedulerIntegrationException("Unrecognized value for window_position")

    def get_case_current_schedule_phase(self):
        phase_num = self.case.get_case_property('current_schedule_phase')
        try:
            return int(phase_num)
        except:
            return None

    def get_visit(self, form):
        try:
            visit = form.schedule.visits[self.scheduler_module_info.visit_number]
        except IndexError:
            raise self.VisitSchedulerIntegrationException("Visit not found")

        if visit.repeats:
            raise self.VisitSchedulerIntegrationException("Repeat visits are not supported")

        return visit

    def get_anchor_date(self, anchor_case_property):
        anchor_date = self.case.get_case_property(anchor_case_property)
        anchor_date = _try_date_conversion(anchor_date)
        if isinstance(anchor_date, datetime):
            anchor_date = anchor_date.date()

        if not isinstance(anchor_date, date):
            raise self.VisitSchedulerIntegrationException("Unable to get anchor date")

        return anchor_date

    def get_result(self):
        module, form = self.get_visit_scheduler_module_and_form(
            self.case.domain,
            self.scheduler_module_info.app_id,
            self.scheduler_module_info.form_unique_id
        )

        form_phase_num, phase = self.get_visit_scheduler_form_phase(module)
        if form_phase_num != self.get_case_current_schedule_phase():
            return False, None

        anchor_date = self.get_anchor_date(phase.anchor)
        visit = self.get_visit(form)
        visit_due_date = anchor_date + timedelta(days=visit.due)
        return True, self.calculate_window_date(visit, visit_due_date)


class CreateScheduleInstanceActionDefinition(CaseRuleActionDefinition):
    alert_schedule = models.ForeignKey('scheduling.AlertSchedule', null=True, on_delete=models.PROTECT)
    timed_schedule = models.ForeignKey('scheduling.TimedSchedule', null=True, on_delete=models.PROTECT)

    # A List of [recipient_type, recipient_id]
    recipients = jsonfield.JSONField(default=list)

    # (Optional, ignored if None) The name of a case property whose value will be tracked
    # over time on the schedule instance as last_reset_case_property_value.
    # Every time the case property's value changes, the schedule's start date is
    # reset to the current date.
    reset_case_property_name = models.CharField(max_length=126, null=True)

    # A dict with the structure represented by SchedulerModuleInfo;
    # when enabled=True in this dict, the framework uses info related to the
    # specified visit number to set the start date for any schedule instances
    # created from this CreateScheduleInstanceActionDefinition.
    scheduler_module_info = jsonfield.JSONField(default=dict)

    class SchedulerModuleInfo(JsonObject):
        # Set to True to enable setting the start date of any schedule instances
        # based on the visit scheduler info details below
        enabled = BooleanProperty(default=False)

        # The app that contains the visit scheduler form being referenced
        app_id = StringProperty()

        # The unique_id of the visit scheduler form in the above app
        form_unique_id = StringProperty()

        # The visit number from which to pull the start date for any schedule
        # instances; this should be the 0-based index in the FormSchedule.visits list
        visit_number = IntegerProperty()

        # VISIT_WINDOW_START - the start date used will be the first date in the window
        # VISIT_WINDOW_END - the start date used will be the last date in the window
        # VISIT_WINDOW_DUE_DATE - the start date used will be the due date of the visit
        window_position = StringProperty(choices=[VISIT_WINDOW_START, VISIT_WINDOW_END, VISIT_WINDOW_DUE_DATE])

    @property
    def schedule(self):
        if self.alert_schedule_id:
            return self.alert_schedule
        elif self.timed_schedule_id:
            return self.timed_schedule

        raise ValueError("Expected a schedule")

    @schedule.setter
    def schedule(self, value):
        from corehq.messaging.scheduling.models import AlertSchedule, TimedSchedule

        self.alert_schedule = None
        self.timed_schedule = None

        if isinstance(value, AlertSchedule):
            self.alert_schedule = value
        elif isinstance(value, TimedSchedule):
            self.timed_schedule = value
        else:
            raise TypeError("Expected an instance of AlertSchedule or TimedSchedule")

    def notify_scheduler_integration_exception(self, case, scheduler_module_info):
        details = scheduler_module_info.to_json()
        details.update({
            'domain': case.domain,
            'case_id': case.case_id,
        })
        notify_exception(
            None,
            message="Error in messaging / visit scheduler integration",
            details=details
        )

    def when_case_matches(self, case, rule):
        schedule = self.schedule
        if isinstance(schedule, AlertSchedule):
            refresh_case_alert_schedule_instances(case, schedule, self, rule)
        elif isinstance(schedule, TimedSchedule):
            kwargs = {}
            scheduler_module_info = self.get_scheduler_module_info()
            if scheduler_module_info.enabled:
                try:
                    case_phase_matches, schedule_instance_start_date = VisitSchedulerIntegrationHelper(case,
                        scheduler_module_info).get_result()
                except VisitSchedulerIntegrationHelper.VisitSchedulerIntegrationException:
                    self.notify_scheduler_integration_exception(case, scheduler_module_info)
                    return CaseRuleActionResult()

                if not case_phase_matches:
                    self.delete_schedule_instances(case)
                    return CaseRuleActionResult()
                else:
                    kwargs['start_date'] = schedule_instance_start_date

            refresh_case_timed_schedule_instances(case, schedule, self, rule, **kwargs)

        return CaseRuleActionResult()

    def when_case_does_not_match(self, case, rule):
        self.delete_schedule_instances(case)
        return CaseRuleActionResult()

    def delete_schedule_instances(self, case):
        if self.alert_schedule_id:
            get_case_alert_schedule_instances_for_schedule_id(case.case_id, self.alert_schedule_id).delete()

        if self.timed_schedule_id:
            get_case_timed_schedule_instances_for_schedule_id(case.case_id, self.timed_schedule_id).delete()

    def get_scheduler_module_info(self):
        return self.SchedulerModuleInfo(**self.scheduler_module_info)

    def set_scheduler_module_info(self, info):
        if not isinstance(info, self.SchedulerModuleInfo):
            raise ValueError("Expected CreateScheduleInstanceActionDefinition.SchedulerModuleInfo")

        self.scheduler_module_info = info.to_json()


class AutomaticUpdateAction(models.Model):
    ACTION_UPDATE = 'UPDATE'
    ACTION_CLOSE = 'CLOSE'

    ACTION_CHOICES = (
        (ACTION_UPDATE, ACTION_UPDATE),
        (ACTION_CLOSE, ACTION_CLOSE),
    )

    EXACT = "EXACT"
    CASE_PROPERTY = "CASE_PROPERTY"

    PROPERTY_TYPE_CHOICES = (
        (EXACT, ugettext_lazy("Exact value")),
        (CASE_PROPERTY, ugettext_lazy("Case property")),
    )


    rule = models.ForeignKey('AutomaticUpdateRule', on_delete=models.PROTECT)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)

    # property_name and property_value are ignored unless action is UPDATE
    property_name = models.CharField(max_length=126, null=True)
    property_value = models.CharField(max_length=126, null=True)

    property_value_type = models.CharField(max_length=15,
                                           default=EXACT)

    class Meta:
        app_label = "data_interfaces"


class CaseRuleSubmission(models.Model):
    domain = models.CharField(max_length=126)
    rule = models.ForeignKey('AutomaticUpdateRule', on_delete=models.PROTECT)

    # The timestamp that this record was created on
    created_on = models.DateTimeField(db_index=True)

    # Reference to XFormInstance.form_id or XFormInstanceSQL.form_id
    form_id = models.CharField(max_length=255, unique=True, db_index=True)

    # A shortcut to keep track of which forms get archived
    archived = models.BooleanField(default=False)

    class Meta:
        index_together = (
            ('domain', 'created_on'),
            ('domain', 'rule', 'created_on'),
        )


class CaseRuleUndoer(object):

    def __init__(self, domain, rule_id=None, since=None):
        self.domain = domain
        self.rule_id = rule_id
        self.since = since

    def get_submission_queryset(self):
        qs = CaseRuleSubmission.objects.filter(
            domain=self.domain,
            archived=False,
        )

        if self.rule_id is not None:
            qs = qs.filter(rule_id=self.rule_id)

        if self.since:
            qs = qs.filter(created_on__gte=self.since)

        return qs

    def bulk_undo(self, progress_bar=False):
        chunk_size = 100
        result = {
            'processed': 0,
            'skipped': 0,
            'archived': 0,
        }

        form_ids = list(self.get_submission_queryset().values_list('form_id', flat=True))
        form_id_chunks = chunked(form_ids, chunk_size)
        if progress_bar:
            length = len(form_ids) / chunk_size
            if len(form_ids) % chunk_size > 0:
                length += 1
            form_id_chunks = with_progress_bar(form_id_chunks, length=length)

        for form_id_chunk in form_id_chunks:
            archived_form_ids = []
            for form in FormAccessors(self.domain).iter_forms(form_id_chunk):
                result['processed'] += 1

                if not form.is_normal or any([u.creates_case() for u in get_case_updates(form)]):
                    result['skipped'] += 1
                    continue

                if not form.is_archived:
                    form.archive(user_id=SYSTEM_USER_ID)
                result['archived'] += 1
                archived_form_ids.append(form.form_id)

            CaseRuleSubmission.objects.filter(form_id__in=archived_form_ids).update(archived=True)

        return result


class DomainCaseRuleRun(models.Model):
    STATUS_RUNNING = 'R'
    STATUS_FINISHED = 'F'
    STATUS_HALTED = 'H'

    domain = models.CharField(max_length=126)
    started_on = models.DateTimeField(db_index=True)
    finished_on = models.DateTimeField(null=True)
    status = models.CharField(max_length=1)

    cases_checked = models.IntegerField(null=True)
    num_updates = models.IntegerField(null=True)
    num_closes = models.IntegerField(null=True)
    num_related_updates = models.IntegerField(null=True)
    num_related_closes = models.IntegerField(null=True)
    num_creates = models.IntegerField(null=True)

    class Meta:
        index_together = (
            ('domain', 'started_on'),
        )

    def done(self, status, cases_checked, result):
        if not isinstance(result, CaseRuleActionResult):
            raise TypeError("Expected an instance of CaseRuleActionResult")

        self.status = status
        self.cases_checked = cases_checked
        self.num_updates = result.num_updates
        self.num_closes = result.num_closes
        self.num_related_updates = result.num_related_updates
        self.num_related_closes = result.num_related_closes
        self.num_creates = result.num_creates
        self.finished_on = datetime.utcnow()
        self.save()
