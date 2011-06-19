import types
from datetime import date, datetime
from django import template
from django.utils.html import escape
from casexml.apps.case.models import CommCareCase
from django.template.loader import render_to_string

register = template.Library()

@register.simple_tag
def render_case(case):
    if isinstance(case, basestring):
        # we were given an ID, fetch the case
        case = CommCareCase.get(case)
    
    return render_to_string("case/partials/single_case.html", {"case": case})
    
    
        