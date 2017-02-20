from __future__ import absolute_import
from django.conf.urls import url
from corehq.apps.analytics.views import HubspotClickDeployView

urlpatterns = [
    url(r'^hubspot/click-deploy/$', HubspotClickDeployView.as_view(), name=HubspotClickDeployView.urlname),
]
