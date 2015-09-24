from couchforms.models import all_known_formlike_doc_types


CASE = 'case'
FORM = 'form'


def get_topic(document_type):
    if document_type in ('CommCareCase', 'CommCareCase-Deleted'):
        return CASE
    elif document_type in all_known_formlike_doc_types():
        return FORM
