import os
import re
import stat
import time
import json
import errno
import logging
import datetime
import traceback

from functools import wraps
from inspect import istraceback


class SituRotatingFileHandler(logging.FileHandler, object):
    """
    这个类提供了一种多进程环境下的rotating filehandler。保证多进程间切日志不会丢失。

    所有日志的后缀都是'%Y-%m-%d'，比如：situ_log_name.log.2020-03-14。

    其原理是，在每天半夜进行rotating的时候，不像TimeRotatingFileHandler一样进行rename操作，
    而是直接用当前时间生成新的日期后缀，创建新的日志文件。
    """

    def __init__(self, filename):
        self._filename = filename
        self._rotate_at = self._next_rotate_at()
        super(SituRotatingFileHandler, self).__init__(filename, mode='a')

    @classmethod
    def _next_rotate_at(cls):
        now = datetime.datetime.now()
        next_day = (now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)).timetuple()
        return time.mktime(next_day)

    def _open(self):
        now = datetime.datetime.now()
        log_today = "{0}.{1}".format(self._filename,
                                     now.strftime('%Y-%m-%d'))
        try:
            fd = os.open(log_today, os.O_CREAT | os.O_EXCL,
                         stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP |
                         stat.S_IROTH)
            os.close(fd)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        self.baseFilename = log_today
        return super(SituRotatingFileHandler, self)._open()

    def emit(self, record):
        if time.time() > self._rotate_at:
            self._rotate_at = self._next_rotate_at()
            self.close()
        super(SituRotatingFileHandler, self).emit(record)


'''
This library is provided to allow standard python logging
to output log data as JSON formatted strings
'''
# Support order in python 2.7 and 3
try:
    from collections import OrderedDict
except ImportError:
    pass

# skip natural LogRecord attributes
# http://docs.python.org/library/logging.html#logrecord-attributes
RESERVED_ATTRS = (
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
    'funcName', 'levelname', 'levelno', 'lineno', 'module',
    'msecs', 'message', 'msg', 'name', 'pathname', 'process',
    'processName', 'relativeCreated', 'stack_info', 'thread', 'threadName')

RESERVED_ATTR_HASH = dict(zip(RESERVED_ATTRS, RESERVED_ATTRS))


def merge_record_extra(record, target, reserved=RESERVED_ATTR_HASH):
    """
    Merges extra attributes from LogRecord object into target dictionary

    :param record: logging.LogRecord
    :param target: dict to update
    :param reserved: dict or list with reserved keys to skip
    """
    for key, value in record.__dict__.items():
        # this allows to have numeric keys
        if (key not in reserved
                and not (hasattr(key, "startswith")
                         and key.startswith('_'))):
            target[key] = value
    return target


def situ_log_request(a_logger):
    """"django view装饰器，用来记录日志"""

    def a_log(view_func):
        @wraps(view_func)
        def returned_wrapper(request, *args, **kwargs):
            try:
                request.ALOG_DATA = {}
                return view_func(request, *args, **kwargs)
            finally:
                a_logger.info(request.ALOG_DATA)

        return returned_wrapper

    return a_log


class JsonFormatter(logging.Formatter, object):
    """
    A custom formatter to format logging records as json strings.
    extra values will be formatted as str() if nor supported by
    json default encoder
    """

    def __init__(self, *args, **kwargs):
        """
        :param json_default: a function for encoding non-standard objects
            as outlined in http://docs.python.org/2/library/json.html
        :param json_encoder: optional custom encoder
        :param json_serializer: a :meth:`json.dumps`-compatible callable
            that will be used to serialize the log record.
        :param prefix: an optional string prefix added at the beginning of
            the formatted string
        """
        self.json_default = kwargs.pop("json_default", None)
        self.json_encoder = kwargs.pop("json_encoder", None)
        self.json_serializer = json.dumps
        self.prefix = kwargs.pop("prefix", "")
        # super(JsonFormatter, self).__init__(*args, **kwargs)
        logging.Formatter.__init__(self, *args, **kwargs)
        if not self.json_encoder and not self.json_default:
            def _default_json_handler(obj):
                '''Prints dates in ISO format'''
                if isinstance(obj, (datetime.date, datetime.time)):
                    return obj.isoformat()
                elif istraceback(obj):
                    tb = ''.join(traceback.format_tb(obj))
                    return tb.strip()
                elif isinstance(obj, Exception):
                    return "Exception: %s" % str(obj)
                return str(obj)

            self.json_default = _default_json_handler
        self._required_fields = self.parse()
        self._skip_fields = dict(zip(self._required_fields,
                                     self._required_fields))
        self._skip_fields.update(RESERVED_ATTR_HASH)

    def parse(self):
        """Parses format string looking for substitutions"""
        standard_formatters = re.compile(r'\((.+?)\)', re.IGNORECASE)
        return standard_formatters.findall(self._fmt)

    def add_fields(self, log_record, record, message_dict):
        """
        Override this method to implement custom logic for adding fields.
        """
        for field in self._required_fields:
            log_record[field] = record.__dict__.get(field)
        log_record.update(message_dict)
        merge_record_extra(record, log_record, reserved=self._skip_fields)

    def process_log_record(self, log_record):
        """
        Override this method to implement custom logic
        on the possibly ordered dictionary.
        """
        return log_record

    def jsonify_log_record(self, log_record):
        """Returns a json string of the log record."""
        return self.json_serializer(log_record)

    def format(self, record):
        """Formats a log record and serializes to json"""
        message_dict = {}
        if isinstance(record.msg, dict):
            message_dict = record.msg
            record.message = None
        else:
            record.message = record.getMessage()
        # only format time if needed
        if "asctime" in self._required_fields:
            record.asctime = self.formatTime(record, self.datefmt)

        # Display formatted exception, but allow overriding it in the
        # user-supplied dict.
        if record.exc_info and not message_dict.get('exc_info'):
            message_dict['exc_info'] = self.formatException(record.exc_info)
        if not message_dict.get('exc_info') and record.exc_text:
            message_dict['exc_info'] = record.exc_text

        try:
            log_record = OrderedDict()
        except NameError:
            log_record = {}

        self.add_fields(log_record, record, message_dict)
        log_record = self.process_log_record(log_record)

        return "%s%s" % (self.prefix, self.jsonify_log_record(log_record))
