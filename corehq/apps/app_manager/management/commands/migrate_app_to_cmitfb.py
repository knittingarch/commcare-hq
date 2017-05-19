import logging
from collections import defaultdict
from datetime import datetime, timedelta
from lxml import etree as ET

from couchdbkit import ResourceNotFound
from django.core.management import BaseCommand

from corehq.apps.app_manager.dbaccessors import get_app_ids_in_domain
from corehq.apps.app_manager.models import Application, PreloadAction, CaseReferences
from corehq.apps.app_manager.util import save_xform
from corehq.apps.app_manager.xform import (
    XForm, SESSION_USERCASE_ID,
    get_add_case_preloads_case_id_xpath,
    get_case_parent_id_xpath,
)
from corehq.toggles import NAMESPACE_DOMAIN, USER_PROPERTY_EASY_REFS
from dimagi.utils.parsing import json_format_datetime


logger = logging.getLogger('app_migration')
logger.setLevel('DEBUG')


class Command(BaseCommand):
    help = '''
        Migrate apps from case management in the app builder to form builder.
        Pass either domain name(s) (to migrate all apps in the domain) or
        individual app id(s). Will skip any apps that have already been
        migrated.
    '''

    def add_arguments(self, parser):
        parser.add_argument('app_id_or_domain', nargs='+',
            help="App ID or domain name. Must be a domain name "
                 "with --usercase option.")
        parser.add_argument('--usercase', action='store_true',
            help='Migrate user properties.')
        parser.add_argument('--fix-user-properties', action='store_true',
            help='Fix bad user property references.')
        parser.add_argument('--force', action='store_true',
            help='Migrate even if app.vellum_case_management is already true.')
        parser.add_argument('-n', '--dry-run', action='store_true',
            help='Do not save updated apps, just print log output.')

    def handle(self, **options):
        app_ids_by_domain = defaultdict(set)
        self.force = options["force"]
        self.dry = "DRY RUN " if options["dry_run"] else ""
        self.fix_user_props = options["fix_user_properties"]
        self.migrate_usercase = options["usercase"]
        if not self.fix_user_props:
            raise Exception(
                "This is currently broken and needs to be fixed. "
                "Problems:\n"
                "1. wrong xpath used for user property references\n"
                "2. case references in form.case_references.load are overwritten\n"
                "3. log detaileds about what changed in case of problems.\n"
            )
        for ident in options["app_id_or_domain"]:
            if not (self.migrate_usercase or self.fix_user_props):
                try:
                    app = Application.get(ident)
                    app_ids_by_domain[app.domain].add(ident)
                    continue
                except ResourceNotFound:
                    pass
            app_ids_by_domain[ident].update(get_app_ids_in_domain(ident))

        for domain, app_ids in sorted(app_ids_by_domain.items()):
            logger.info('migrating %s: %s apps', domain, len(app_ids))
            for app_id in app_ids:
                try:
                    if self.fix_user_props:
                        self.fix_user_properties(app_id)
                    else:
                        self.migrate_app(app_id)
                except SkipApp as err:
                    logger.error("skipping app %s: %s", app_id, err)
                except Exception:
                    logger.exception("skipping app %s", app_id)
            if self.migrate_usercase and not USER_PROPERTY_EASY_REFS.enabled(domain):
                if not self.dry:
                    USER_PROPERTY_EASY_REFS.set(domain, True, NAMESPACE_DOMAIN)
                logger.info("%senabled USER_PROPERTY_EASY_REFS for domain: %s",
                    self.dry, domain)

        logger.info('done with migrate_app_to_cmitfb %s', self.dry)

    def migrate_app(self, app_id):
        app = Application.get(app_id)
        migrate_usercase = should_migrate_usercase(app, self.migrate_usercase)
        if self.migrate_usercase and not migrate_usercase:
            return False
        if app.vellum_case_management and not migrate_usercase and not self.force:
            logger.info('already migrated app {}'.format(app_id))
            return False
        logger.info('%smigrating app %s', self.dry, app_id)

        modules = [m for m in app.modules if m.module_type == 'basic']
        for module in modules:
            forms = [f for f in module.forms if f.doc_type == 'Form']
            for form in forms:
                preloads = []
                preload = form.actions.case_preload.preload
                if preload:
                    if form.requires == 'case':
                        preloads.append({
                            "hashtag": "#case/",
                            "preloads": preload,
                        })
                    form.actions.case_preload = PreloadAction()
                usercase_preload = form.actions.usercase_preload.preload
                if migrate_usercase and usercase_preload:
                    preloads.append({
                        "hashtag": "#user/",
                        "preloads": usercase_preload,
                        "case_id_xpath": SESSION_USERCASE_ID,
                    })
                    form.actions.usercase_preload = PreloadAction()
                if preloads:
                    migrate_preloads(app, module, form, preloads)

        if not self.dry:
            app.vellum_case_management = True
            app.save()
        return True

    def fix_user_properties(self, app_id):
        try:
            app = Application.get(app_id)
        except Exception as err:
            raise SkipApp(type(err).__name__)
        copy = get_pre_migration_copy(app)
        if copy is None:
            logger.warn("%scopy not found %s/%s version %s",
                self.dry, app.domain, app_id, app.version)
            return
        logger.info("%smigrating %s/%s: (%s) version diff=%s",
            self.dry, app.domain, app_id, copy.version, app.version - copy.version)
        updated = False
        for modi, module, formi, new_form, old_form in iter_forms(app, copy):
            preloads = old_form.actions.usercase_preload.preload
            if preloads:
                updated = fix_user_props(
                    app, module, new_form, modi, formi, preloads, self.dry
                ) or updated
        if updated:
            if not self.dry:
                app.save()
            logger.info("%ssaved app %s", self.dry, app_id)


ORIGINAL_MIGRATION_DATE = datetime(2017, 5, 17, 15, 25)
USERPROP_PREFIX = (
    "instance('casedb')/casedb/case[@case_type='commcare-user']"
    "[hq_user_id=instance('commcaresession')/session/context/userid]/"
)


def iter_forms(app, copy):
    old_forms = {form.unique_id: form
        for module in copy.modules if module.module_type == 'basic'
        for form in module.forms if form.doc_type == 'Form'}
    modules = [m for m in enumerate(app.modules) if m[1].module_type == 'basic']
    for modi, module in modules:
        forms = [f for f in enumerate(module.forms) if f[1].doc_type == 'Form']
        for formi, form in forms:
            if form.unique_id in old_forms:
                yield modi, module, formi, form, old_forms[form.unique_id]
            else:
                logger.warn("form not in pre-migration copy: %s/%s", modi, formi)


def fix_user_props(app, module, form, modi, formi, preloads, dry):
    updated = False
    xform = XForm(form.source)
    refs = {xform.resolve_path(ref): prop for ref, prop in preloads.iteritems()}
    for node in xform.model_node.findall("{f}setvalue"):
        if (node.attrib.get('ref') in refs
                and node.attrib.get('event') == "xforms-ready"):
            ref = node.attrib.get('ref')
            value = (node.attrib.get('value') or "").replace(" ", "")
            prop = refs[ref]
            userprop = "#user/" + prop
            if value == get_bad_usercase_path(module, form, prop):
                logger.info("%s/%s setvalue %s -> %s", modi, formi, userprop, ref)
                node.attrib["value"] = USERPROP_PREFIX + prop
                updated = True
            elif value != USERPROP_PREFIX + prop:
                logger.warn("%s/%s %s has unexpected value: %r (not %s)",
                    modi, formi, ref, value, userprop)
    if updated:
        if dry:
            logger.info("updated setvalues in XML:\n%s", "\n".join(line
                for line in ET.tostring(xform.xml).split("\n")
                if "setvalue" in line))
        else:
            save_xform(app, form, ET.tostring(xform.xml))
    return updated


def get_bad_usercase_path(module, form, property_):
    from corehq.apps.app_manager.util import split_path
    case_id_xpath = get_add_case_preloads_case_id_xpath(module, form)
    parent_path, property_ = split_path(property_)
    property_xpath = case_property(property_)
    id_xpath = get_case_parent_id_xpath(parent_path, case_id_xpath=case_id_xpath)
    return id_xpath.case().property(property_xpath)


def case_property(property_):
    return {
        'name': 'case_name',
        'owner_id': '@owner_id'
    }.get(property_, property_)


def get_pre_migration_copy(app):
    from corehq.apps.app_manager.util import get_correct_app_class

    def date_key(doc):
        return doc.get("built_on") or mindate

    mindate = json_format_datetime(datetime(1980, 1, 1))
    migrate_date = json_format_datetime(ORIGINAL_MIGRATION_DATE)
    skip = 0
    docs = None

    while docs is None or date_key(docs[-1]) > migrate_date:
        docs = saved_apps = [row['doc'] for row in Application.get_db().view(
            'app_manager/saved_app',
            startkey=[app.domain, app._id, {}],
            endkey=[app.domain, app._id],
            descending=True,
            skip=skip,
            limit=5,
            include_docs=True,
        )]
        if not docs:
            break
        skip += len(docs)
        docs = sorted(saved_apps, key=date_key, reverse=True)
        for doc in docs:
            if date_key(doc) < migrate_date:
                copy = get_correct_app_class(doc).wrap(doc)
                if copy.version < app.version:
                    return copy
    return None


class SkipApp(Exception):
    pass


def migrate_preloads(app, module, form, preloads):
    xform = XForm(form.source)
    case_id_xpath = get_add_case_preloads_case_id_xpath(module, form)
    for kwargs in preloads:
        hashtag = kwargs.pop("hashtag")
        kwargs['case_id_xpath'] = case_id_xpath
        xform.add_case_preloads(**kwargs)
        refs = {path: [hashtag + case_property]
                for path, case_property in kwargs["preloads"].iteritems()}
        if form.case_references:
            form.case_references.load.update(refs)
        else:
            form.case_references = CaseReferences(load=refs)
    save_xform(app, form, ET.tostring(xform.xml))


def should_migrate_usercase(app, migrate_usercase):
    return migrate_usercase and any(form.actions.usercase_preload.preload
        for module in app.modules if module.module_type == 'basic'
        for form in module.forms if form.doc_type == 'Form')
