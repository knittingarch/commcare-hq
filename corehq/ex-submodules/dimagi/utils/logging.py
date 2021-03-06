from __future__ import absolute_import
import sys
import logging


notify_logger = logging.getLogger('notify')


def notify_error(message, details=None):
    notify_logger.error(message, extra=details)


def notify_exception(request, message=None, details=None, exec_info=None):
    """
    :param request: a Django request object
    :param message: message string
    :param details: dict with additional details to be included in the output
    """
    if request is not None:
        message = message or request.path
    notify_logger.error(
        'Notify Exception: %s' % (message
                                  or "No message provided, fix error handler"),
        exc_info=exec_info or sys.exc_info(),
        extra={
            'status_code': 500,
            'request': request,
            'details': details,
        }
    )


def log_signal_errors(signal_results, message, details):
    for result in signal_results:
        # Second argument is None if there was no error
        return_val = result[1]
        if return_val and isinstance(return_val, Exception):
            notify_exception(
                None,
                message=message % return_val.__class__.__name__,
                details=details,
                exec_info=(type(return_val), return_val, return_val.__traceback__)
            )
