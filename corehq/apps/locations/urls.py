from __future__ import absolute_import
from django.conf.urls import url

from .views import (
    LocationsListView,
    NewLocationView,
    EditLocationView,
    LocationImportView,
    LocationImportStatusView,
    LocationTypesView,
    LocationFieldsView,
    DowngradeLocationsView,
    default, child_locations_for_select2, location_importer_job_poll,
    location_export, unassign_users, archive_location, unarchive_location,
    delete_location, location_descendants_count,
)

settings_urls = [
    url(r'^$', default, name='default_locations_view'),
    url(r'^child_locations/$', child_locations_for_select2, name='child_locations_for_select2'),
    url(r'^list/$', LocationsListView.as_view(), name=LocationsListView.urlname),
    url(r'^location_types/$', LocationTypesView.as_view(), name=LocationTypesView.urlname),
    url(r'^import/$', LocationImportView.as_view(), name=LocationImportView.urlname),
    url(r'^import_status/(?P<download_id>[0-9a-fA-Z]{25,32})/$', LocationImportStatusView.as_view(),
        name=LocationImportStatusView.urlname),
    url(r'^location_importer_job_poll/(?P<download_id>[0-9a-fA-Z]{25,32})/$',
        location_importer_job_poll, name='location_importer_job_poll'),
    url(r'^export_locations/$', location_export, name='location_export'),
    url(r'^new/$', NewLocationView.as_view(), name=NewLocationView.urlname),
    url(r'^fields/$', LocationFieldsView.as_view(), name=LocationFieldsView.urlname),
    url(r'^downgrade/$', DowngradeLocationsView.as_view(),
        name=DowngradeLocationsView.urlname),
    url(r'^unassign_users/$', unassign_users, name='unassign_users'),
    url(r'^(?P<loc_id>[\w-]+)/archive/$', archive_location, name='archive_location'),
    url(r'^(?P<loc_id>[\w-]+)/unarchive/$', unarchive_location, name='unarchive_location'),
    url(r'^(?P<loc_id>[\w-]+)/delete/$', delete_location, name='delete_location'),
    url(r'^(?P<loc_id>[\w-]+)/descendants/$', location_descendants_count, name='location_descendants_count'),
    url(r'^(?P<loc_id>[\w-]+)/$', EditLocationView.as_view(), name=EditLocationView.urlname),
]
