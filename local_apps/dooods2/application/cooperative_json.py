"""Cooperative JSON decoding for large, already size-limited requests."""

import asyncio
import json
import re
from typing import Any

_WHITESPACE = re.compile(r"[ \t\r\n]*")


async def loads(data: bytes | bytearray, *, yield_every: int = 2048) -> Any:
    """Decode JSON like json.loads while yielding between groups of tokens.

    The input is already fully buffered and size-limited by the caller. String
    and scalar parsing use the standard library decoder; containers are built
    iteratively so a large array does not monopolize the event loop.
    """
    text = data.decode("utf-8")
    if text.startswith("\ufeff"):
        raise ValueError("Unexpected UTF-8 BOM")
    decoder = json.JSONDecoder()
    size = len(text)
    position = 0
    steps = 0
    stack = []  # [container, state, pending_key]
    root = None
    has_root = False

    def whitespace(index):
        return _WHITESPACE.match(text, index).end()

    def add_value(value):
        nonlocal root, has_root
        if not stack:
            if has_root:
                raise ValueError("Extra data")
            root = value
            has_root = True
            return
        frame = stack[-1]
        if isinstance(frame[0], list):
            frame[0].append(value)
        else:
            frame[0][frame[2]] = value
            frame[2] = None
        frame[1] = "comma_or_end"

    while True:
        steps += 1
        if steps >= yield_every:
            steps = 0
            await asyncio.sleep(0)
        position = whitespace(position)
        if not stack and has_root:
            if position != size:
                raise ValueError("Extra data")
            return root
        if position >= size:
            raise ValueError("Incomplete JSON")
        if stack:
            frame = stack[-1]
            container, state = frame[0], frame[1]
            if state == "comma_or_end":
                closing = "]" if isinstance(container, list) else "}"
                if text[position] == closing:
                    position += 1
                    stack.pop()
                    continue
                if text[position] != ",":
                    raise ValueError("Expected comma")
                position += 1
                frame[1] = "value" if isinstance(container, list) else "key"
                continue
            if state in ("key", "key_or_end"):
                if state == "key_or_end" and text[position] == "}":
                    position += 1
                    stack.pop()
                    continue
                if text[position] != '"':
                    raise ValueError("Expected object key")
                key, position = decoder.raw_decode(text, position)
                position = whitespace(position)
                if position >= size or text[position] != ":":
                    raise ValueError("Expected colon")
                position += 1
                frame[2] = key
                frame[1] = "value"
                continue
            if state == "value_or_end" and text[position] == "]":
                position += 1
                stack.pop()
                continue
        character = text[position]
        if character in "[{":
            if len(stack) >= 900:
                raise ValueError("JSON nesting too deep")
            container = [] if character == "[" else {}
            add_value(container)
            stack.append([container, "value_or_end" if character == "[" else "key_or_end", None])
            position += 1
        elif character in '\"-0123456789tfnNI':
            value, position = decoder.raw_decode(text, position)
            add_value(value)
        else:
            raise ValueError("Invalid JSON value")


async def dumps(value: Any, *, yield_every: int = 2048) -> bytes:
    """Match JSONResponse encoding while yielding between encoded tokens."""
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")
    )
    result = bytearray()
    for index, part in enumerate(encoder.iterencode(value), 1):
        result.extend(part.encode("utf-8"))
        if index % yield_every == 0:
            await asyncio.sleep(0)
    return bytes(result)
