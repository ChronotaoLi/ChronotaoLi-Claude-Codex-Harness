"""Bounded, read-only file evidence; deliberately independent of Git and processes."""
from decimal import Decimal
import difflib
import hashlib
import json
import os
from pathlib import Path
import stat

from .util import HarnessError

MAX_HASH_BYTES = 1024 ** 3
MAX_TEXT_BYTES = 8 * 1024 ** 2
MAX_DIFF_CHARS = 24000


def _read(path, base, limit, collect=True):
    if not isinstance(path, str) or not path or len(path) > 4096:
        raise HarnessError("path must be a nonempty string of at most 4096 characters")
    target = Path(path)
    if not target.is_absolute():
        target = Path(base) / target
    try:
        # Resolve links explicitly; nonblocking open also avoids blocking on a FIFO
        # substituted between resolution and the descriptor's regular-file check.
        target = target.resolve(strict=True)
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise HarnessError(f"Not a regular file: {target}")
            if before.st_size > limit:
                raise HarnessError(f"File exceeds {limit} byte limit: {target}")
            digest, chunks, size = hashlib.sha256(), [], 0
            while chunk := stream.read(min(65536, limit + 1 - size)):
                size += len(chunk)
                if size > limit:
                    raise HarnessError(f"File exceeds {limit} byte limit: {target}")
                digest.update(chunk)
                if collect:
                    chunks.append(chunk)
            after = os.fstat(stream.fileno())
            current = target.stat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise HarnessError(f"File changed while reading: {target}")
        return {"path": str(target), "sha256": digest.hexdigest(), "bytes": size}, b"".join(chunks)
    except (OSError, RuntimeError, ValueError) as error:
        raise HarnessError(f"Cannot read {target}: {error}") from error


def file_sha256(path, base):
    return _read(path, base, MAX_HASH_BYTES, collect=False)[0]


class _InvalidJSON(ValueError):
    pass


def json_check(path, base):
    result, data = _read(path, base, MAX_TEXT_BYTES)
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise _InvalidJSON(f"Duplicate object key: {key[:160]!r}")
            obj[key] = value
        return obj
    def constant(value):
        raise _InvalidJSON(f"Non-finite number is not JSON: {value}")
    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_int=Decimal)
        # JSON's finite lexical numbers may overflow Python floats; reject these too.
        stack, count = [value], 0
        import math
        while stack:
            item = stack.pop()
            count += 1
            if isinstance(item, float) and not math.isfinite(item):
                raise _InvalidJSON("Number is outside the finite numeric range")
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        root = ("null" if value is None else "boolean" if isinstance(value, bool) else
                "object" if isinstance(value, dict) else "array" if isinstance(value, list) else
                "string" if isinstance(value, str) else "number")
        result.update(valid=True, root_type=root, value_count=count, error=None)
    except (json.JSONDecodeError, UnicodeDecodeError, _InvalidJSON) as error:
        result.update(valid=False, root_type=None, value_count=None,
                      error={"message": str(error)[:500], "line": getattr(error, "lineno", None),
                             "column": getattr(error, "colno", None), "offset": getattr(error, "pos", None)})
    except (ValueError, RecursionError, MemoryError, OverflowError) as error:
        raise HarnessError(f"JSON exceeds parser capabilities: {type(error).__name__}: {str(error)[:300]}") from error
    return result


def file_diff(left, right, base):
    left_info, left_data = _read(left, base, MAX_TEXT_BYTES)
    right_info, right_data = _read(right, base, MAX_TEXT_BYTES)
    result = {"left": left_info, "right": right_info, "different": left_data != right_data,
              "binary": False, "diff": "", "truncated": False}
    try:
        if b"\0" in left_data or b"\0" in right_data:
            raise UnicodeError()
        left_text, right_text = left_data.decode("utf-8"), right_data.decode("utf-8")
    except UnicodeError:
        result["binary"] = True
        return result
    # Disable SequenceMatcher's quadratic worst cases for repetitive text via its
    # default autojunk heuristic, and bound emitted output independently of input.
    lines = difflib.unified_diff(left_text.splitlines(keepends=True), right_text.splitlines(keepends=True),
                                 fromfile="left", tofile="right", n=3)
    pieces, remaining = [], MAX_DIFF_CHARS
    for line in lines:
        if len(line) > remaining:
            pieces.append(line[:remaining])
            result["truncated"] = True
            break
        pieces.append(line)
        remaining -= len(line)
    result["diff"] = "".join(pieces)
    return result
