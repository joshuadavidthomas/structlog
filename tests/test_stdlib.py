# SPDX-License-Identifier: MIT OR Apache-2.0
# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the MIT License.  See the LICENSE file in the root of this
# repository for complete details.

from __future__ import annotations

import json
import logging
import logging.config
import os
import sys

from io import StringIO
from typing import Any, Callable, Collection, Dict
from unittest.mock import patch

import pytest
import pytest_asyncio

from pretend import call_recorder, stub

from structlog import (
    PrintLogger,
    ReturnLogger,
    configure,
    get_context,
    wrap_logger,
)
from structlog._config import _CONFIG
from structlog._log_levels import CRITICAL, NAME_TO_LEVEL, WARN
from structlog.dev import ConsoleRenderer
from structlog.exceptions import DropEvent
from structlog.processors import JSONRenderer, KeyValueRenderer
from structlog.stdlib import (
    AsyncBoundLogger,
    BoundLogger,
    ExtraAdder,
    LoggerFactory,
    PositionalArgumentsFormatter,
    ProcessorFormatter,
    _FixedFindCallerLogger,
    add_log_level,
    add_log_level_number,
    add_logger_name,
    filter_by_level,
    get_logger,
    recreate_defaults,
    render_to_log_args_and_kwargs,
    render_to_log_kwargs,
)
from structlog.testing import CapturedCall
from structlog.typing import BindableLogger, EventDict

from .additional_frame import additional_frame


def build_bl(logger=None, processors=None, context=None):
    """
    Convenience function to build BoundLogger with sane defaults.
    """
    return BoundLogger(logger or ReturnLogger(), processors, {})


def return_method_name(_, method_name, __):
    """
    A final renderer that returns the name of the logging method.
    """
    return method_name


class TestLoggerFactory:
    def setup_method(self, method):
        """
        The stdlib logger factory modifies global state to fix caller
        identification.
        """
        self.original_logger = logging.getLoggerClass()

    def teardown_method(self, method):
        logging.setLoggerClass(self.original_logger)

    def test_deduces_correct_name(self):
        """
        The factory isn't called directly but from structlog._config so
        deducing has to be slightly smarter.
        """
        assert "tests.additional_frame" == (
            additional_frame(LoggerFactory()).name
        )
        assert "tests.test_stdlib" == LoggerFactory()().name

    def test_ignores_frames(self):
        """
        The name guesser walks up the frames until it reaches a frame whose
        name is not from structlog or one of the configurable other names.
        """

        # Compute the names to __main__ so it doesn't get thrown off if people
        # install plugins that alter the frames. E.g. #370
        names = set()
        f = sys._getframe()
        while f.f_globals["__name__"] != "__main__":
            names.add(f.f_globals["__name__"].split(".", 1)[0])
            f = f.f_back

        assert (
            "__main__"
            == additional_frame(
                LoggerFactory(ignore_frame_names=list(names))
            ).name
        )

    def test_deduces_correct_caller(self):
        """
        It will find the correct caller.
        """
        logger = _FixedFindCallerLogger("test")

        file_name, line_number, func_name = logger.findCaller()[:3]

        assert file_name == os.path.realpath(__file__)
        assert func_name == "test_deduces_correct_caller"

    def test_stack_info(self):
        """
        If we ask for stack_info, it will returned.
        """
        logger = _FixedFindCallerLogger("test")
        testing, is_, fun, stack_info = logger.findCaller(stack_info=True)

        assert "testing, is_, fun" in stack_info

    def test_no_stack_info_by_default(self):
        """
        If we don't ask for stack_info, it won't be returned.
        """
        logger = _FixedFindCallerLogger("test")
        testing, is_, fun, stack_info = logger.findCaller()

        assert None is stack_info

    def test_find_caller(self, caplog):
        """
        The caller is found.
        """
        logger = LoggerFactory()()

        logger.error("Test")

        assert caplog.text.startswith(
            "ERROR    tests.test_stdlib:test_stdlib.py"
        )

    def test_sets_correct_logger(self):
        """
        Calling LoggerFactory ensures that Logger.findCaller gets patched.
        """
        LoggerFactory()

        assert logging.getLoggerClass() is _FixedFindCallerLogger

    def test_positional_argument_avoids_guessing(self):
        """
        If a positional argument is passed to the factory, it's used as the
        name instead of guessing.
        """
        lf = LoggerFactory()("foo")

        assert "foo" == lf.name


class TestFilterByLevel:
    def test_filters_lower_levels(self):
        """
        Log entries below the current level raise a DropEvent.
        """
        logger = logging.Logger(__name__)
        logger.setLevel(CRITICAL)

        with pytest.raises(DropEvent):
            filter_by_level(logger, "warn", {})

    def test_passes_higher_levels(self):
        """
        Log entries with higher levels are passed through unchanged.
        """
        logger = logging.Logger(__name__)
        logger.setLevel(WARN)
        event_dict = {"event": "test"}

        assert event_dict is filter_by_level(logger, "warn", event_dict)
        assert event_dict is filter_by_level(logger, "error", event_dict)
        assert event_dict is filter_by_level(logger, "exception", event_dict)


class TestBoundLogger:
    @pytest.mark.parametrize(
        ("method_name"),
        ["debug", "info", "warning", "error", "exception", "critical"],
    )
    def test_proxies_to_correct_method(self, method_name):
        """
        The basic proxied methods are proxied to the correct counterparts.
        """
        bl = BoundLogger(ReturnLogger(), [return_method_name], {})

        assert method_name == getattr(bl, method_name)("event")

    def test_proxies_to_correct_method_special_cases(self):
        """
        Fatal maps to critical and warn to warning.
        """
        bl = BoundLogger(ReturnLogger(), [return_method_name], {})

        assert "warning" == bl.warn("event")
        assert "critical" == bl.fatal("event")

    def test_proxies_log(self):
        """
        BoundLogger.exception.log() is proxied to the appropriate method.
        """
        bl = BoundLogger(ReturnLogger(), [return_method_name], {})

        assert "critical" == bl.log(50, "event")
        assert "debug" == bl.log(10, "event")

    def test_positional_args_proxied(self):
        """
        Positional arguments supplied are proxied as kwarg.
        """
        bl = BoundLogger(ReturnLogger(), [], {})
        args, kwargs = bl.debug("event", "foo", bar="baz")

        assert "baz" == kwargs.get("bar")
        assert ("foo",) == kwargs.get("positional_args")

    @pytest.mark.parametrize(
        "attribute_name",
        ["name", "level", "parent", "propagate", "handlers", "disabled"],
    )
    def test_stdlib_passthrough_attributes(self, attribute_name):
        """
        stdlib logger attributes are also available in stdlib BoundLogger.
        """
        stdlib_logger = logging.getLogger("Test")
        stdlib_logger_attribute = getattr(stdlib_logger, attribute_name)
        bl = BoundLogger(stdlib_logger, [], {})
        bound_logger_attribute = getattr(bl, attribute_name)

        assert bound_logger_attribute == stdlib_logger_attribute

    @pytest.mark.parametrize(
        ("method_name", "method_args"),
        [
            ("addHandler", [None]),
            ("removeHandler", [None]),
            ("hasHandlers", None),
            ("callHandlers", [None]),
            ("handle", [None]),
            ("setLevel", [None]),
            ("getEffectiveLevel", None),
            ("isEnabledFor", [None]),
            ("findCaller", None),
            (
                "makeRecord",
                [
                    "name",
                    "debug",
                    "test_func",
                    "1",
                    "test msg",
                    ["foo"],
                    False,
                ],
            ),
            ("getChild", [None]),
        ],
    )
    def test_stdlib_passthrough_methods(self, method_name, method_args):
        """
        stdlib logger methods are also available in stdlib BoundLogger.
        """
        called_stdlib_method = [False]

        def validate(*args, **kw):
            called_stdlib_method[0] = True

        stdlib_logger = logging.getLogger("Test")
        stdlib_logger_method = getattr(stdlib_logger, method_name, None)
        if stdlib_logger_method:
            setattr(stdlib_logger, method_name, validate)
            bl = BoundLogger(stdlib_logger, [], {})
            bound_logger_method = getattr(bl, method_name)

            assert bound_logger_method is not None

            if method_args:
                bound_logger_method(*method_args)
            else:
                bound_logger_method()

            assert called_stdlib_method[0] is True

    def test_exception_exc_info(self):
        """
        BoundLogger.exception sets exc_info=True.
        """
        bl = BoundLogger(ReturnLogger(), [], {})

        assert ((), {"exc_info": True, "event": "event"}) == bl.exception(
            "event"
        )

    def test_exception_exc_info_override(self):
        """
        If *exc_info* is password to exception, it's used.
        """
        bl = BoundLogger(ReturnLogger(), [], {})

        assert ((), {"exc_info": 42, "event": "event"}) == bl.exception(
            "event", exc_info=42
        )

    def test_proxies_bind(self):
        """
        Bind calls the correct bind.
        """
        bl = build_bl().bind(a=42)

        assert {"a": 42} == get_context(bl)

    def test_proxies_new(self):
        """
        Newcalls the correct new.
        """
        bl = build_bl().bind(a=42).new(b=23)

        assert {"b": 23} == get_context(bl)

    def test_proxies_unbind(self):
        """
        Unbind calls the correct unbind.
        """
        bl = build_bl().bind(a=42).unbind("a")

        assert {} == get_context(bl)

    def test_proxies_try_unbind(self):
        """
        try_unbind calls the correct try_unbind.
        """
        bl = build_bl().bind(a=42).try_unbind("a", "b")

        assert {} == get_context(bl)

    @pytest.mark.parametrize(
        "meth", ["debug", "info", "warning", "error", "critical"]
    )
    async def test_async_log_methods(self, meth, cl):
        """
        Async methods log async.
        """
        bl = build_bl(cl, processors=[])

        await getattr(bl, f"a{meth}")("Async!")

        assert [
            CapturedCall(method_name=meth, args=(), kwargs={"event": "Async!"})
        ] == cl.calls

    async def test_async_log_methods_special_cases(self, cl):
        """
        afatal maps to critical.
        """
        bl = build_bl(cl, processors=[])

        await bl.afatal("Async!")

        assert [
            CapturedCall(
                method_name="critical", args=(), kwargs={"event": "Async!"}
            )
        ] == cl.calls

    async def test_alog(self, cl):
        """
        Alog logs async at the correct level.
        """
        bl = build_bl(cl, processors=[])

        await bl.alog(logging.INFO, "foo %s", "bar")

        assert [
            CapturedCall(
                method_name="info",
                args=(),
                kwargs={"positional_args": ("bar",), "event": "foo %s"},
            )
        ] == cl.calls

    async def test_aexception_exc_info_true(self, cl):
        """
        aexception passes current exc_info into dispatch.
        """
        bl = build_bl(cl, processors=[])

        try:
            raise ValueError(42)
        except ValueError as e:
            await bl.aexception("oops")
            exc = e

        (cc,) = cl.calls

        assert isinstance(cc[2]["exc_info"], tuple)
        assert exc == cc[2]["exc_info"][1]

    async def test_aexception_exc_info_explicit(self, cl):
        """
        In aexception, if exc_info isn't missing or True, leave it be.
        """
        bl = build_bl(cl, processors=[])

        obj = object()

        await bl.aexception("ooops", exc_info=obj)

        assert obj is cl.calls[0].kwargs["exc_info"]


class TestPositionalArgumentsFormatter:
    def test_formats_tuple(self):
        """
        Positional arguments as simple types are rendered.
        """
        formatter = PositionalArgumentsFormatter()
        event_dict = formatter(
            None,
            None,
            {"event": "%d %d %s", "positional_args": (1, 2, "test")},
        )

        assert "1 2 test" == event_dict["event"]
        assert "positional_args" not in event_dict

    def test_formats_dict(self):
        """
        Positional arguments as dict are rendered.
        """
        formatter = PositionalArgumentsFormatter()
        event_dict = formatter(
            None,
            None,
            {"event": "%(foo)s bar", "positional_args": ({"foo": "bar"},)},
        )

        assert "bar bar" == event_dict["event"]
        assert "positional_args" not in event_dict

    def test_positional_args_retained(self):
        """
        Positional arguments are retained if remove_positional_args
        argument is set to False.
        """
        formatter = PositionalArgumentsFormatter(remove_positional_args=False)
        positional_args = (1, 2, "test")
        event_dict = formatter(
            None,
            None,
            {"event": "%d %d %s", "positional_args": positional_args},
        )

        assert "positional_args" in event_dict
        assert positional_args == event_dict["positional_args"]

    def test_nop_no_args(self):
        """
        If no positional args are passed, nothing happens.
        """
        formatter = PositionalArgumentsFormatter()

        assert {} == formatter(None, None, {})

    def test_args_removed_if_empty(self):
        """
        If remove_positional_args is True and positional_args is (), still
        remove them.

        Regression test for https://github.com/hynek/structlog/issues/82.
        """
        formatter = PositionalArgumentsFormatter()

        assert {} == formatter(None, None, {"positional_args": ()})


class TestAddLogLevelNumber:
    @pytest.mark.parametrize(("level", "number"), NAME_TO_LEVEL.items())
    def test_log_level_number_added(self, level, number):
        """
        The log level number is added to the event dict.
        """
        event_dict = add_log_level_number(None, level, {})

        assert number == event_dict["level_number"]


class TestAddLogLevel:
    def test_log_level_added(self):
        """
        The log level is added to the event dict.
        """
        event_dict = add_log_level(None, "error", {})

        assert "error" == event_dict["level"]

    @pytest.mark.parametrize(
        ("alias", "normalized"), [("warn", "warning"), ("exception", "error")]
    )
    def test_log_level_alias_normalized(self, alias, normalized):
        """
        The normalized name of the log level is added to the event dict.
        """
        event_dict = add_log_level(None, alias, {})

        assert normalized == event_dict["level"]


@pytest.fixture(name="make_log_record")
def _make_log_record():
    """
    A LogRecord factory.
    """

    def create_log_record(**kwargs):
        defaults = {
            "name": "sample-name",
            "level": logging.INFO,
            "pathname": None,
            "lineno": None,
            "msg": "sample-message",
            "args": [],
            "exc_info": None,
        }
        defaults.update(kwargs)
        return logging.LogRecord(**defaults)

    return create_log_record


class TestAddLoggerName:
    def test_logger_name_added(self):
        """
        The logger name is added to the event dict.
        """
        name = "sample-name"
        logger = logging.getLogger(name)
        event_dict = add_logger_name(logger, None, {})

        assert name == event_dict["logger"]

    def test_logger_name_added_with_record(self, make_log_record):
        """
        The logger name is deduced from the LogRecord if provided.
        """
        name = "sample-name"
        record = make_log_record(name=name)
        event_dict = add_logger_name(None, None, {"_record": record})

        assert name == event_dict["logger"]


def extra_dict() -> dict[str, Any]:
    """
    A dict to be passed in the `extra` parameter of the `logging` module's log
    methods.
    """
    return {
        "this": "is",
        "some": "extra values",
        "x_int": 4,
        "x_bool": True,
    }


@pytest.fixture(name="extra_dict")
def extra_dict_fixture():
    return extra_dict()


class TestExtraAdder:
    @pytest.mark.parametrize(
        ("allow", "misses"),
        [
            (None, None),
            ({}, None),
            *[({key}, None) for key in extra_dict()],
            ({"missing"}, {"missing"}),
            ({"missing", "keys"}, {"missing"}),
            ({"this", "x_int"}, None),
        ],
    )
    def test_add_extra(
        self,
        make_log_record: Callable[[], logging.LogRecord],
        extra_dict: dict[str, Any],
        allow: Collection[str] | None,
        misses: set[str] | None,
    ):
        """
        Extra attributes of a LogRecord object are added to the event dict.
        """
        record: logging.LogRecord = make_log_record()
        record.__dict__.update(extra_dict)
        event_dict = {"_record": record, "ed_key": "ed_value"}
        expected = self._copy_allowed(event_dict, extra_dict, allow)

        if allow is None:
            actual = ExtraAdder()(None, None, event_dict)
            assert expected == actual
        actual = ExtraAdder(allow)(None, None, event_dict)
        assert expected == actual
        if misses:
            assert misses.isdisjoint(expected.keys())

    def test_no_record(self):
        """
        If the event_dict has no LogRecord, do nothing.
        """
        actual = ExtraAdder()(None, None, {})

        assert {} == actual

    @pytest.mark.parametrize(
        ("allow", "misses"),
        [
            (None, None),
            ({}, None),
            *[({key}, None) for key in extra_dict()],
            ({"missing"}, {"missing"}),
            ({"missing", "keys"}, {"missing"}),
            ({"this", "x_int"}, None),
        ],
    )
    def test_add_extra_e2e(
        self,
        extra_dict: dict[str, Any],
        allow: Collection[str] | None,
        misses: set[str] | None,
    ):
        """
        Values passed in the `extra` parameter of the `logging` module's log
        methods pass through to log output.
        """
        logger = logging.Logger(sys._getframe().f_code.co_name)
        string_io = StringIO()
        handler = logging.StreamHandler(string_io)
        formatter = ProcessorFormatter(
            foreign_pre_chain=[ExtraAdder(allow)],
            processors=[JSONRenderer()],
        )
        handler.setFormatter(formatter)
        handler.setLevel(0)
        logger.addHandler(handler)
        logger.setLevel(0)
        logging.warning("allow = %s", allow)  # noqa: LOG015

        event_dict = {"event": "Some text"}
        expected = self._copy_allowed(event_dict, extra_dict, allow)

        logger.info("Some %s", "text", extra=extra_dict)
        actual = {
            key: value
            for key, value in json.loads(string_io.getvalue()).items()
            if not key.startswith("_")
        }

        assert expected == actual
        if misses:
            assert misses.isdisjoint(expected.keys())

    @classmethod
    def _copy_allowed(
        cls,
        event_dict: EventDict,
        extra_dict: dict[str, Any],
        allow: Collection[str] | None,
    ) -> EventDict:
        if allow is None:
            return {**event_dict, **extra_dict}

        return {
            **event_dict,
            **{
                key: value for key, value in extra_dict.items() if key in allow
            },
        }


@pytest.fixture(name="stdlib_logger")
def _stdlib_logger():
    logger = logging.getLogger("test_logger")
    logger.setLevel(logging.DEBUG)

    yield logger

    logging.basicConfig()


class TestRenderToLogArgsAndKwargs:
    def test_default(self, stdlib_logger: logging.Logger):
        """
        Passes `event` key from `event_dict` in the first positional argument
        and handles otherwise empty `event_dict`.
        """
        method_name = "debug"
        event = "message"
        args, kwargs = render_to_log_args_and_kwargs(
            stdlib_logger, method_name, {"event": event}
        )

        assert (event,) == args
        assert {} == kwargs

        with patch.object(stdlib_logger, "_log") as mock_log:
            getattr(stdlib_logger, method_name)(*args, **kwargs)

        mock_log.assert_called_once_with(logging.DEBUG, event, ())

    def test_pass_remaining_event_dict_as_extra(
        self, stdlib_logger: logging.Logger, event_dict: dict[str, Any]
    ):
        """
        Passes remaining `event_dict` as `extra`.
        """
        expected_extra = event_dict.copy()

        method_name = "info"
        event = "message"
        event_dict["event"] = event

        args, kwargs = render_to_log_args_and_kwargs(
            stdlib_logger, method_name, event_dict
        )

        assert (event,) == args
        assert {"extra": expected_extra} == kwargs

        with patch.object(stdlib_logger, "_log") as mock_log:
            getattr(stdlib_logger, method_name)(*args, **kwargs)

        mock_log.assert_called_once_with(
            logging.INFO, event, (), extra=expected_extra
        )

    def test_pass_positional_args_from_event_dict_as_args(
        self, stdlib_logger: logging.Logger, event_dict: dict[str, Any]
    ):
        """
        Passes items from "positional_args" key from `event_dict` as positional
        arguments.
        """
        expected_extra = event_dict.copy()

        method_name = "warning"
        event = "message: a = %s, b = %d"
        positional_args = ("foo", 123)
        event_dict["event"] = event
        event_dict["positional_args"] = positional_args

        args, kwargs = render_to_log_args_and_kwargs(
            stdlib_logger, method_name, event_dict
        )

        assert (event, *(positional_args)) == args
        assert {"extra": expected_extra} == kwargs

        with patch.object(stdlib_logger, "_log") as mock_log:
            getattr(stdlib_logger, method_name)(*args, **kwargs)

        mock_log.assert_called_once_with(
            logging.WARNING, event, positional_args, extra=expected_extra
        )

    def test_pass_kwargs_from_event_dict_as_kwargs(
        self, stdlib_logger: logging.Logger, event_dict: dict[str, Any]
    ):
        """
        Passes "exc_info", "stack_info", and "stacklevel" keys from `event_dict`
        as keyword arguments.
        """
        expected_extra = event_dict.copy()

        method_name = "info"
        event = "message"
        exc_info = True
        stack_info = False
        stacklevel = 2
        event_dict["event"] = event
        event_dict["exc_info"] = exc_info
        event_dict["stack_info"] = stack_info
        event_dict["stacklevel"] = stacklevel

        args, kwargs = render_to_log_args_and_kwargs(
            stdlib_logger, method_name, event_dict
        )

        assert (event,) == args
        assert {
            "exc_info": exc_info,
            "stack_info": stack_info,
            "stacklevel": stacklevel,
            "extra": expected_extra,
        } == kwargs

        with patch.object(stdlib_logger, "_log") as mock_log:
            getattr(stdlib_logger, method_name)(*args, **kwargs)

        mock_log.assert_called_once_with(
            logging.INFO,
            event,
            (),
            exc_info=exc_info,
            stack_info=stack_info,
            stacklevel=stacklevel,
            extra=expected_extra,
        )

    def test_integration(
        self, stdlib_logger: logging.Logger, event_dict: dict[str, Any]
    ):
        """
        `render_to_log_args_and_kwargs` with a wrapped logger calls the stdlib
        logger correctly.

        Reserved stdlib keyword arguments are in `logging.Logger._log`.
        https://github.com/python/cpython/blob/60403a5409ff2c3f3b07dd2ca91a7a3e096839c7/Lib/logging/__init__.py#L1640
        """
        event = "message: a = %s, b = %d"
        arg_1 = "foo"
        arg_2 = 123
        exc_info = False
        stack_info = True
        stacklevel = 3

        struct_logger = wrap_logger(
            stdlib_logger,
            processors=[render_to_log_args_and_kwargs],
            wrapper_class=BoundLogger,
        )

        with patch.object(stdlib_logger, "_log") as mock_log:
            struct_logger.info(
                event,
                arg_1,
                arg_2,
                exc_info=exc_info,
                stack_info=stack_info,
                stacklevel=stacklevel,
                **event_dict,
            )

        mock_log.assert_called_once_with(
            logging.INFO,
            event,
            (arg_1, arg_2),
            exc_info=exc_info,
            stack_info=stack_info,
            stacklevel=stacklevel,
            extra=event_dict,
        )


class TestRenderToLogKwargs:
    def test_default(self, stdlib_logger):
        """
        Translates `event` to `msg` and handles otherwise empty `event_dict`s.
        """
        d = render_to_log_kwargs(None, None, {"event": "message"})

        assert {"msg": "message", "extra": {}} == d

        # now check stdlib logger likes those kwargs
        with patch.object(stdlib_logger, "_log") as mock_log:
            stdlib_logger.info(**d)

        mock_log.assert_called_once_with(logging.INFO, "message", (), extra={})

    def test_add_extra_event_dict(self, event_dict, stdlib_logger):
        """
        Adds all remaining data from `event_dict` into `extra`.
        """
        event_dict["event"] = "message"
        d = render_to_log_kwargs(None, None, event_dict)

        assert {"msg": "message", "extra": event_dict} == d

        # now check stdlib logger likes those kwargs
        with patch.object(stdlib_logger, "_log") as mock_log:
            stdlib_logger.info(**d)

        mock_log.assert_called_once_with(
            logging.INFO, "message", (), extra=event_dict
        )

    def test_handles_special_kw(self, event_dict, stdlib_logger):
        """
        "exc_info", "stack_info", and "stacklevel" aren't passed as extras.

        Cf. https://github.com/hynek/structlog/issues/424
        """
        del event_dict["a"]  # needs a repr
        event_dict["event"] = "message"

        event_dict["exc_info"] = True
        event_dict["stack_info"] = False
        event_dict["stacklevel"] = 1
        event_dict["stackLevel"] = 1  # not a reserved kw

        d = render_to_log_kwargs(None, None, event_dict)
        expected = {
            "msg": "message",
            "exc_info": True,
            "stack_info": False,
            "stacklevel": 1,
            "extra": {
                "b": [3, 4],
                "x": 7,
                "y": "test",
                "z": (1, 2),
                "stackLevel": 1,
            },
        }

        assert expected == d

        # now check stdlib logger likes those kwargs
        with patch.object(stdlib_logger, "_log") as mock_log:
            stdlib_logger.info(**d)

        expected.pop("msg")
        mock_log.assert_called_once_with(
            logging.INFO, "message", (), **expected
        )

    def test_integration_special_kw(self, event_dict, stdlib_logger):
        """
        render_to_log_kwargs with a wrapped logger calls the stdlib logger
        correctly

        reserved stdlib keywords are in logging.Logger._log
        https://github.com/python/cpython/blob/ae7b17673f29efe17b416cbcfbf43b5b3ff5977c/Lib/logging/__init__.py#L1632
        """
        expected = {
            "msg": "message",
            "exc_info": True,
            "stack_info": False,
            "stacklevel": 1,
            "extra": {**event_dict},
        }

        event_dict["exc_info"] = True
        event_dict["stack_info"] = False
        event_dict["stacklevel"] = 1

        struct_logger = wrap_logger(
            stdlib_logger,
            processors=[render_to_log_kwargs],
        )

        # now check struct logger passes those kwargs to stdlib
        with patch.object(stdlib_logger, "_log") as mock_log:
            struct_logger.info("message", **event_dict)

        expected.pop("msg")
        mock_log.assert_called_once_with(
            logging.INFO, "message", (), **expected
        )


@pytest.fixture(name="configure_for_processor_formatter")
def _configure_for_processor_formatter():
    """
    Configure structlog to use ProcessorFormatter.

    Reset logging setting after the test (structlog is reset automatically
    before all tests).
    """
    configure(
        processors=[add_log_level, ProcessorFormatter.wrap_for_formatter],
        logger_factory=LoggerFactory(),
        wrapper_class=BoundLogger,
    )

    yield

    logging.basicConfig()


def configure_logging(
    pre_chain,
    logger=None,
    pass_foreign_args=False,
    renderer=ConsoleRenderer(colors=False),  # noqa: B008
):
    """
    Configure logging to use ProcessorFormatter.

    Return a list that is filled with event dicts form calls.
    """
    event_dicts = []

    def capture(_, __, ed):
        event_dicts.append(ed.copy())

        return ed

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {
                    "()": ProcessorFormatter,
                    "processors": [
                        capture,
                        ProcessorFormatter.remove_processors_meta,
                        renderer,
                    ],
                    "foreign_pre_chain": pre_chain,
                    "format": "%(message)s [in %(funcName)s]",
                    "logger": logger,
                    "pass_foreign_args": pass_foreign_args,
                }
            },
            "handlers": {
                "default": {
                    "level": "DEBUG",
                    "class": "logging.StreamHandler",
                    "formatter": "plain",
                }
            },
            "loggers": {
                "": {
                    "handlers": ["default"],
                    "level": "DEBUG",
                    "propagate": True,
                }
            },
        }
    )

    return event_dicts


@pytest.mark.usefixtures("configure_for_processor_formatter")
class TestProcessorFormatter:
    """
    These are all integration tests because they're all about integration.
    """

    def test_foreign_delegate(self, capsys):
        """
        If foreign_pre_chain is None, non-structlog log entries are delegated
        to logging. The processor chain's event dict is invoked with
        `_from_structlog=False`
        """
        calls = configure_logging(None)

        logging.getLogger().warning("foo")

        assert ("", "foo [in test_foreign_delegate]\n") == capsys.readouterr()
        assert calls[0]["_from_structlog"] is False
        assert isinstance(calls[0]["_record"], logging.LogRecord)

    def test_clears_args(self, capsys):
        """
        We render our log records before sending it back to logging.  Therefore
        we must clear `LogRecord.args` otherwise the user gets an
        `TypeError: not all arguments converted during string formatting.` if
        they use positional formatting in stdlib logging.
        """
        configure_logging(None)

        logging.getLogger().warning("hello %s.", "world")

        assert (
            "",
            "hello world. [in test_clears_args]\n",
        ) == capsys.readouterr()

    def test_pass_foreign_args_true_sets_positional_args_key(self):
        """
        If `pass_foreign_args` is `True` we set the `positional_args` key in
        the `event_dict` before clearing args.
        """
        test_processor = call_recorder(lambda _, __, event_dict: event_dict)
        configure_logging((test_processor,), pass_foreign_args=True)

        positional_args = {"foo": "bar"}
        logging.getLogger().info("okay %(foo)s", positional_args)

        event_dict = test_processor.calls[0].args[2]

        assert "positional_args" in event_dict
        assert positional_args == event_dict["positional_args"]

    def test_log_dict(self, capsys):
        """
        dicts can be logged with std library loggers.
        """
        configure_logging(None)

        logging.getLogger().warning({"foo": "bar"})

        assert (
            "",
            "{'foo': 'bar'} [in test_log_dict]\n",
        ) == capsys.readouterr()

    def test_foreign_pre_chain(self, capsys):
        """
        If foreign_pre_chain is an iterable, it's used to pre-process
        non-structlog log entries.
        """
        configure_logging([add_log_level])

        logging.getLogger().warning("foo")

        assert (
            "",
            "[warning  ] foo [in test_foreign_pre_chain]\n",
        ) == capsys.readouterr()

    def test_foreign_pre_chain_add_logger_name(self, capsys):
        """
        foreign_pre_chain works with add_logger_name processor.
        """
        configure_logging((add_logger_name,))

        logging.getLogger("sample-name").warning("foo")

        assert (
            "",
            "foo                            [sample-name] [in test_foreign_pr"
            "e_chain_add_logger_name]\n",
        ) == capsys.readouterr()

    def test_foreign_chain_can_pass_dictionaries_without_excepting(
        self, capsys
    ):
        """
        If a foreign logger passes a dictionary to a logging function,
        check we correctly identify that it did not come from structlog.
        """
        configure_logging(None)
        configure(
            processors=[ProcessorFormatter.wrap_for_formatter],
            logger_factory=LoggerFactory(),
            wrapper_class=BoundLogger,
        )

        logging.getLogger().warning({"foo": "bar"})

        assert (
            "",
            "{'foo': 'bar'} [in "
            "test_foreign_chain_can_pass_dictionaries_without_excepting]\n",
        ) == capsys.readouterr()

    def test_foreign_pre_chain_gets_exc_info(self):
        """
        If non-structlog record contains exc_info, foreign_pre_chain functions
        have access to it.
        """
        test_processor = call_recorder(lambda _, __, event_dict: event_dict)
        configure_logging((test_processor,), renderer=KeyValueRenderer())

        try:
            raise RuntimeError("oh no")
        except Exception:
            logging.getLogger().exception("okay")

        event_dict = test_processor.calls[0].args[2]

        assert "exc_info" in event_dict
        assert isinstance(event_dict["exc_info"], tuple)

    def test_foreign_pre_chain_sys_exc_info(self):
        """
        If a foreign_pre_chain function accesses sys.exc_info(),
        ProcessorFormatter should not have changed it.
        """

        class MyError(Exception):
            pass

        def add_excinfo(logger, log_method, event_dict):
            event_dict["exc_info"] = sys.exc_info()
            return event_dict

        test_processor = call_recorder(lambda _, __, event_dict: event_dict)
        configure_logging(
            (add_excinfo, test_processor), renderer=KeyValueRenderer()
        )

        try:
            raise MyError("oh no")
        except Exception:
            logging.getLogger().error("okay")

        event_dict = test_processor.calls[0].args[2]

        assert MyError is event_dict["exc_info"][0]

    def test_other_handlers_get_original_record(self):
        """
        Logging handlers that come after the handler with ProcessorFormatter
        should receive original, unmodified record.
        """
        configure_logging(None)

        handler1 = logging.StreamHandler()
        handler1.setFormatter(ProcessorFormatter(JSONRenderer()))
        handler2 = stub(
            handle=call_recorder(lambda record: None),
            level=logging.INFO,
        )
        logger = logging.getLogger()
        logger.addHandler(handler1)
        logger.addHandler(handler2)

        logger.info("meh")

        assert 1 == len(handler2.handle.calls)

        handler2_record = handler2.handle.calls[0].args[0]

        assert "meh" == handler2_record.msg

    @pytest.mark.parametrize("keep", [True, False])
    def test_formatter_unsets_exc_info(self, capsys, keep):
        """
        Stack traces doesn't get printed outside of the json document when
        keep_exc_info are set to False but preserved if set to True.
        """
        configure_logging(None)
        logger = logging.getLogger()

        def format_exc_info_fake(logger, name, event_dict):
            del event_dict["exc_info"]
            event_dict["exception"] = "Exception!"
            return event_dict

        formatter = ProcessorFormatter(
            processor=JSONRenderer(),
            keep_stack_info=keep,
            keep_exc_info=keep,
            foreign_pre_chain=[format_exc_info_fake],
        )
        logger.handlers[0].setFormatter(formatter)

        try:
            raise RuntimeError("oh no")
        except Exception:
            logging.getLogger().exception("seen worse")

        out, err = capsys.readouterr()

        assert "" == out

        if keep is False:
            assert (
                '{"event": "seen worse", "exception": "Exception!"}\n'
            ) == err
        else:
            assert "Traceback (most recent call last):" in err

    @pytest.mark.parametrize("keep", [True, False])
    def test_formatter_unsets_stack_info(self, capsys, keep):
        """
        Stack traces doesn't get printed outside of the json document when
        keep_stack_info are set to False but preserved if set to True.
        """
        configure_logging(None)
        logger = logging.getLogger()

        formatter = ProcessorFormatter(
            processor=JSONRenderer(),
            keep_stack_info=keep,
            keep_exc_info=keep,
            foreign_pre_chain=[],
        )
        logger.handlers[0].setFormatter(formatter)

        logging.getLogger().warning("have a stack trace", stack_info=True)

        out, err = capsys.readouterr()

        assert "" == out

        if keep is False:
            assert 1 == err.count("Stack (most recent call last):")
        else:
            assert 2 == err.count("Stack (most recent call last):")

    def test_native(self, capsys):
        """
        If the log entry comes from structlog, it's unpackaged and processed.
        """
        eds = configure_logging(None)

        get_logger().warning("foo")

        assert (
            "",
            "[warning  ] foo [in test_native]\n",
        ) == capsys.readouterr()
        assert eds[0]["_from_structlog"] is True
        assert isinstance(eds[0]["_record"], logging.LogRecord)

    def test_native_logger(self, capsys):
        """
        If the log entry comes from structlog, it's unpackaged and processed.
        """
        logger = logging.getLogger()
        eds = configure_logging(None, logger=logger)

        get_logger().warning("foo")

        assert (
            "",
            "[warning  ] foo [in test_native_logger]\n",
        ) == capsys.readouterr()
        assert eds[0]["_from_structlog"] is True
        assert isinstance(eds[0]["_record"], logging.LogRecord)

    def test_foreign_pre_chain_filter_by_level(self, capsys):
        """
        foreign_pre_chain works with filter_by_level processor.
        """
        logger = logging.getLogger()
        configure_logging([filter_by_level], logger=logger)
        configure(
            processors=[ProcessorFormatter.wrap_for_formatter],
            logger_factory=LoggerFactory(),
            wrapper_class=BoundLogger,
        )

        logger.warning("foo")

        assert (
            "",
            "foo [in test_foreign_pre_chain_filter_by_level]\n",
        ) == capsys.readouterr()

    def test_processor_and_processors(self):
        """
        Passing both processor and processors raises a TypeError.
        """
        with pytest.raises(TypeError, match="mutually exclusive"):
            ProcessorFormatter(processor=1, processors=[1])

    def test_no_renderer(self):
        """
        Passing neither processor nor processors raises a TypeError.
        """
        with pytest.raises(TypeError, match="must be passed"):
            ProcessorFormatter()

    def test_remove_processors_meta(self):
        """
        remove_processors_meta removes _record and _from_structlog. And only
        them.
        """
        assert {"foo": "bar"} == ProcessorFormatter.remove_processors_meta(
            None,
            None,
            {"foo": "bar", "_record": "foo", "_from_structlog": True},
        )

    def test_non_string_message_warning(self):
        """
        A warning is raised if the last processor in
        ProcessorFormatter.processors doesn't return a string.
        """
        configure_logging(None)
        logger = logging.getLogger()

        formatter = ProcessorFormatter(
            processors=[lambda *args, **kwargs: {"foo": "bar"}],
        )
        logger.handlers[0].setFormatter(formatter)

        with pytest.warns(
            RuntimeWarning,
            match="The last processor in ProcessorFormatter.processors must return a string",
        ):
            logger.info("baz")

    def test_logrecord_exc_info(self):
        """
        LogRecord.exc_info is set consistently for structlog and non-structlog
        log records.
        """
        configure_logging(None)

        # This doesn't test ProcessorFormatter itself directly, but it's
        # relevant to setups where ProcessorFormatter is used, i.e. where
        # handlers will receive LogRecord objects that come from both structlog
        # and non-structlog loggers.

        records: Dict[  # noqa: UP006 - dict isn't generic until Python 3.9
            str, logging.LogRecord
        ] = {}

        class DummyHandler(logging.Handler):
            def emit(self, record):
                # Don't do anything; just store the record in the records dict
                # by its message, so we can assert things about it.
                if isinstance(record.msg, dict):
                    records[record.msg["event"]] = record
                else:
                    records[record.msg] = record

        stdlib_logger = logging.getLogger()
        structlog_logger = get_logger()

        # It doesn't matter which logger we add the handler to here.
        stdlib_logger.addHandler(DummyHandler())

        try:
            raise Exception("foo")
        except Exception:
            stdlib_logger.exception("bar")
            structlog_logger.exception("baz")

        stdlib_record = records.pop("bar")

        assert "bar" == stdlib_record.msg
        assert stdlib_record.exc_info
        assert Exception is stdlib_record.exc_info[0]
        assert ("foo",) == stdlib_record.exc_info[1].args

        structlog_record = records.pop("baz")

        assert "baz" == structlog_record.msg["event"]
        assert True is structlog_record.msg["exc_info"]
        assert structlog_record.exc_info
        assert Exception is structlog_record.exc_info[0]
        assert ("foo",) == structlog_record.exc_info[1].args

        assert not records

    def test_use_get_message_false(self):
        """
        If use_get_message_is False, the event is obtained using
        str(record.msg) instead of calling record.getMessage. That means
        positional formatting is not performed.
        """
        event_dicts = []

        def capture(_, __, ed):
            event_dicts.append(ed.copy())

            return str(ed)

        proc = ProcessorFormatter(processors=[capture], use_get_message=False)

        record = logging.LogRecord(
            "foo",
            logging.INFO,
            "path.py",
            42,
            "le msg: %s",
            ("keep separate",),
            None,
        )

        assert proc.format(record)
        assert "le msg: %s" == event_dicts[0]["event"]


@pytest_asyncio.fixture(name="abl")
async def _abl(cl):
    return AsyncBoundLogger(cl, context={}, processors=[])


class TestAsyncBoundLogger:
    def test_sync_bl(self, abl, cl):
        """
        AsyncBoungLogger.sync_bl works outside of loops.
        """
        abl.sync_bl.info("test")

        assert [
            CapturedCall(method_name="info", args=(), kwargs={"event": "test"})
        ] == cl.calls

    @pytest.mark.asyncio
    async def test_protocol(self, abl):
        """
        AsyncBoundLogger is a proper BindableLogger.
        """
        assert isinstance(abl, BindableLogger)

    @pytest.mark.asyncio
    async def test_correct_levels(self, abl, cl, stdlib_log_method):
        """
        The proxy methods call the correct upstream methods.
        """
        await getattr(abl.bind(foo="bar"), stdlib_log_method)("42")

        aliases = {"warn": "warning"}

        expect = aliases.get(stdlib_log_method, stdlib_log_method)

        assert expect == cl.calls[0].method_name

    @pytest.mark.asyncio
    async def test_correct_level_fatal(self, abl, cl):
        """
        fatal, that I have no idea why we support, maps to critical.
        """
        await abl.bind(foo="bar").fatal("42")

        assert "critical" == cl.calls[0].method_name

    @pytest.mark.asyncio
    async def test_log_method(self, abl, cl):
        """
        The `log` method is proxied too.
        """
        await abl.bind(foo="bar").log(logging.ERROR, "42")

        assert "error" == cl.calls[0].method_name

    @pytest.mark.asyncio
    async def test_exception(self, abl, cl):
        """
        `exception` makes sure 'exc_info" is set, if it's not set already.
        """
        try:
            raise ValueError("omg")
        except ValueError:
            await abl.exception("oops")

        ei = cl.calls[0].kwargs["exc_info"]

        assert ValueError is ei[0]
        assert ("omg",) == ei[1].args

    @pytest.mark.asyncio
    async def test_exception_do_not_overwrite(self, abl, cl):
        """
        `exception` leaves exc_info be, if it's set.
        """
        o1 = object()
        o2 = object()
        o3 = object()

        try:
            raise ValueError("omg")
        except ValueError:
            await abl.exception("oops", exc_info=(o1, o2, o3))

        ei = cl.calls[0].kwargs["exc_info"]
        assert (o1, o2, o3) == ei

    @pytest.mark.asyncio
    async def test_bind_unbind(self, cl):
        """
        new/bind/unbind/try_unbind are correctly propagated.
        """
        l1 = AsyncBoundLogger(cl, context={}, processors=[])

        l2 = l1.bind(x=42)

        assert l1 is not l2
        assert l1.sync_bl is not l2.sync_bl
        assert {} == l1._context
        assert {"x": 42} == l2._context

        l3 = l2.new(y=23)

        assert l2 is not l3
        assert l2.sync_bl is not l3.sync_bl
        assert {"y": 23} == l3._context

        l4 = l3.unbind("y")

        assert {} == l4._context
        assert l3 is not l4

        # N.B. x isn't bound anymore.
        l5 = l4.try_unbind("x")

        assert {} == l5._context
        assert l4 is not l5

    @pytest.mark.asyncio
    async def test_integration(self, capsys):
        """
        Configure and log an actual entry.
        """

        configure(
            processors=[add_log_level, JSONRenderer()],
            logger_factory=PrintLogger,
            wrapper_class=AsyncBoundLogger,
            cache_logger_on_first_use=True,
        )

        logger = get_logger()

        await logger.bind(foo="bar").info("baz", x="42")

        assert {
            "foo": "bar",
            "x": "42",
            "event": "baz",
            "level": "info",
        } == json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("log_level", [None, 45])
def test_recreate_defaults(log_level):
    """
    Recreate defaults configures structlog and -- if asked -- logging.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=1,
        force=True,
    )

    recreate_defaults(log_level=log_level)

    assert BoundLogger is _CONFIG.default_wrapper_class
    assert dict is _CONFIG.default_context_class
    assert isinstance(_CONFIG.logger_factory, LoggerFactory)

    log = get_logger().bind()
    if log_level is not None:
        assert log_level == log.getEffectiveLevel()
    else:
        assert 1 == log.getEffectiveLevel()
