"""Incremental decoder for top-level JSON content string.

Used by completion adapters to stream assistant text from structured envelopes
without buffering the entire CLI response.
"""

from __future__ import annotations

from enum import Enum, auto

_ESCAPE_MAP = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class _State(Enum):
    SEEK_ROOT = auto()
    ROOT_KEY_OR_CLOSE = auto()
    READ_KEY = auto()
    EXPECT_COLON = auto()
    EXPECT_CONTENT_VALUE = auto()
    STREAM_CONTENT = auto()
    SKIP_VALUE = auto()


class IncrementalJsonContentDecoder:
    """Incrementally extracts the top-level 'content' string from JSON chunks."""

    def __init__(self) -> None:
        self._state = _State.SEEK_ROOT
        self._depth = 0
        self._key_chars: list[str] = []
        self._current_key = ""
        self._done = False
        self._escape_buf = ""
        self._pending_high_surrogate: int | None = None
        self._skip_in_str = False
        self._skip_str_esc = False
        self._key_esc = False

    @property
    def is_done(self) -> bool:
        return self._done

    def feed(self, chunk: str) -> str:
        if self._done or not chunk:
            return ""

        output: list[str] = []
        for c in chunk:
            if self._done:
                break

            if self._state is _State.SEEK_ROOT:
                if c == "{":
                    self._depth = 1
                    self._state = _State.ROOT_KEY_OR_CLOSE
                continue

            if self._state is _State.ROOT_KEY_OR_CLOSE:
                if c == "}":
                    self._depth = 0
                    self._done = True
                    break
                if c == '"':
                    self._key_chars = []
                    self._key_esc = False
                    self._state = _State.READ_KEY
                continue

            if self._state is _State.READ_KEY:
                if self._key_esc:
                    self._key_chars.append(c)
                    self._key_esc = False
                elif c == "\\":
                    self._key_esc = True
                elif c == '"':
                    self._current_key = "".join(self._key_chars)
                    self._state = _State.EXPECT_COLON
                else:
                    self._key_chars.append(c)
                continue

            if self._state is _State.EXPECT_COLON:
                if c == ":":
                    if self._current_key == "content":
                        self._state = _State.EXPECT_CONTENT_VALUE
                    else:
                        self._state = _State.SKIP_VALUE
                        self._skip_in_str = False
                        self._skip_str_esc = False
                continue

            if self._state is _State.EXPECT_CONTENT_VALUE:
                if c.isspace():
                    continue
                if c == '"':
                    self._state = _State.STREAM_CONTENT
                    self._escape_buf = ""
                    self._pending_high_surrogate = None
                elif c == "n":
                    # {"content": null}
                    self._done = True
                    break
                else:
                    # Non-string content (e.g. array, object, number)
                    self._done = True
                    break
                continue

            if self._state is _State.STREAM_CONTENT:
                if self._escape_buf:
                    if self._escape_buf == "\\":
                        if c == "u":
                            self._escape_buf = "\\u"
                        elif c in _ESCAPE_MAP:
                            if self._pending_high_surrogate is not None:
                                output.append("\ufffd")
                                self._pending_high_surrogate = None
                            output.append(_ESCAPE_MAP[c])
                            self._escape_buf = ""
                        else:
                            if self._pending_high_surrogate is not None:
                                output.append("\ufffd")
                                self._pending_high_surrogate = None
                            output.append(c)
                            self._escape_buf = ""
                        continue

                    if self._escape_buf.startswith("\\u"):
                        if c in HEX_DIGITS:
                            self._escape_buf += c
                            if len(self._escape_buf) == 6:
                                code = int(self._escape_buf[2:], 16)
                                self._escape_buf = ""
                                if 0xD800 <= code <= 0xDBFF:
                                    if self._pending_high_surrogate is not None:
                                        output.append("\ufffd")
                                    self._pending_high_surrogate = code
                                elif 0xDC00 <= code <= 0xDFFF:
                                    if self._pending_high_surrogate is not None:
                                        combined = (
                                            0x10000
                                            + ((self._pending_high_surrogate - 0xD800) << 10)
                                            + (code - 0xDC00)
                                        )
                                        output.append(chr(combined))
                                        self._pending_high_surrogate = None
                                    else:
                                        output.append("\ufffd")
                                else:
                                    if self._pending_high_surrogate is not None:
                                        output.append("\ufffd")
                                        self._pending_high_surrogate = None
                                    output.append(chr(code))
                            continue
                        # Non-hex character encountered during \uXXXX
                        if self._pending_high_surrogate is not None:
                            output.append("\ufffd")
                            self._pending_high_surrogate = None
                        output.append("\ufffd")
                        self._escape_buf = ""
                        # Fall through to process c normally

                if c == "\\":
                    self._escape_buf = "\\"
                    continue
                if c == '"':
                    if self._pending_high_surrogate is not None:
                        output.append("\ufffd")
                        self._pending_high_surrogate = None
                    self._done = True
                    break
                if self._pending_high_surrogate is not None:
                    output.append("\ufffd")
                    self._pending_high_surrogate = None
                output.append(c)
                continue

            if self._state is _State.SKIP_VALUE:
                if self._skip_in_str:
                    if self._skip_str_esc:
                        self._skip_str_esc = False
                    elif c == "\\":
                        self._skip_str_esc = True
                    elif c == '"':
                        self._skip_in_str = False
                else:
                    if c == '"':
                        self._skip_in_str = True
                        self._skip_str_esc = False
                    elif c in "{[":
                        self._depth += 1
                    elif c in "}]":
                        self._depth -= 1
                        if self._depth == 0:
                            self._done = True
                            break
                    elif c == "," and self._depth == 1:
                        self._state = _State.ROOT_KEY_OR_CLOSE
                continue

        return "".join(output)
