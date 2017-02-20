from __future__ import absolute_import
from django.core.management.base import BaseCommand, CommandError
from corehq.apps.userreports import tasks


class Command(BaseCommand):
    help = "Rebuild a user configurable reporting table"
    args = '<indicator_config_id>'
    label = ""

    def handle(self, *args, **options):
        if len(args) < 1:
            raise CommandError('Usage is rebuild_indicator_table %s' % self.args)

        config_id = args[0]
        tasks.rebuild_indicators(config_id)
