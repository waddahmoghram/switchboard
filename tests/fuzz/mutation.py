"""Deterministic generators and byte mutators shared by the seeded fuzz tests.

Each test draws from its own ``random.Random`` seeded from a fixed default, so a failure reproduces exactly
and adding a test never changes another test's inputs. ``SWITCHBOARD_FUZZ_SEED`` and
``SWITCHBOARD_FUZZ_ITERATIONS`` override the seed and the per-test budget for longer local runs.
"""

from __future__ import annotations

import os
import random
from collections.abc import Sequence
from typing import Any

DEFAULT_SEED = 20261111
DEFAULT_ITERATIONS = 1500
MAX_INPUT_BYTES = 4096

# Fragments for regular expressions in schemas and rule conditions. They form valid and invalid patterns but
# never repetition counts that overflow, deep nesting, or nested character sets (which warn in ``re``).
PATTERN_FRAGMENTS = (
    "a",
    "b",
    "[a-z]",
    "[^0-9]",
    "\\d",
    "\\w",
    "\\",
    ".",
    "+",
    "*",
    "?",
    "{2}",
    "{1,3}",
    "{",
    "}",
    "(",
    ")",
    "(?:",
    "|",
    "^",
    "$",
)
# Patterns without nested quantifiers, so matching stays linear for generated text.
SAFE_PATTERNS = (
    "^[0-9a-f]{8}$",
    "^[a-z][a-z0-9_]{0,63}$",
    "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    "^[A-Z]{3}$",
    "x",
    "^$",
)
TOKENS: tuple[bytes, ...] = (
    b"{",
    b"}",
    b"[",
    b"]",
    b":",
    b",",
    b'"',
    b"\\",
    b"true",
    b"false",
    b"null",
    b"NaN",
    b"Infinity",
    b"-Infinity",
    b"1e999",
    b"-0",
    b"0.0",
    b"1E+2",
    b'"\\ud800"',
    b'"\\udc00"',
    b'"\\u0000"',
    b'"\\ud83d\\ude00"',
    b"\xff",
    b"\xc3\xa9",
    b"\xef\xbb\xbf",
    b"9" * 40,
    b"1" * 5000,
    b'"key":',
    b"{}",
    b"[]",
    b" ",
    b"\n",
)
TEXT_ALPHABET = "abcxyz0123456789-_ .:/\u00e9\u4e2d\U0001f600"


def seed() -> int:
    raw = os.environ.get("SWITCHBOARD_FUZZ_SEED", "")
    return int(raw) if raw else DEFAULT_SEED


def iterations(default: int = DEFAULT_ITERATIONS) -> int:
    raw = os.environ.get("SWITCHBOARD_FUZZ_ITERATIONS", "")
    value = int(raw) if raw else default
    if value < 1:
        raise ValueError("SWITCHBOARD_FUZZ_ITERATIONS must be a positive integer")
    return value


def generator(name: str) -> random.Random:
    """A generator for one test; string seeds hash the same way in every process."""
    return random.Random(f"{seed()}:{name}")  # noqa: S311 - reproducible test inputs, not secrets


def mutate(rng: random.Random, data: bytes, corpus: Sequence[bytes], limit: int = MAX_INPUT_BYTES) -> bytes:
    """Apply one to four byte-level mutations and truncate the result to ``limit`` bytes."""
    buffer = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        operation = rng.randrange(7)
        position = rng.randint(0, len(buffer))
        if operation == 0 and buffer:
            buffer[rng.randrange(len(buffer))] ^= 1 << rng.randrange(8)
        elif operation == 1:
            buffer[position:position] = rng.choice(TOKENS)
        elif operation == 2 and buffer:
            del buffer[position : position + rng.randint(1, 8)]
        elif operation == 3 and buffer:
            start = rng.randrange(len(buffer))
            buffer[position:position] = buffer[start : start + rng.randint(1, 16)]
        elif operation == 4:
            other = rng.choice(corpus)
            buffer = buffer[:position] + bytearray(other[rng.randint(0, len(other)) :])
        elif operation == 5 and buffer:
            buffer[rng.randrange(len(buffer))] = rng.randrange(256)
        else:
            depth = rng.choice((1, 2, 8, 64, 1500))
            buffer = bytearray(b"[" * depth) + buffer + bytearray(b"]" * depth)
    return bytes(buffer[:limit])


def text(rng: random.Random, maximum: int = 12) -> str:
    return "".join(rng.choice(TEXT_ALPHABET) for _ in range(rng.randint(0, maximum)))


def pattern(rng: random.Random) -> str:
    return "".join(rng.choice(PATTERN_FRAGMENTS) for _ in range(rng.randint(1, 8)))


def json_value(rng: random.Random, depth: int = 0) -> Any:
    """A finite JSON value as ``json_loads`` would return it."""
    kind = rng.randrange(9 if depth < 3 else 7)
    if kind == 0:
        return None
    if kind == 1:
        return rng.random() < 0.5
    if kind == 2:
        return rng.choice((0, 1, -1, 7, 255, 2**53, -(2**63), 10**30))
    if kind == 3:
        return rng.choice((0.0, -0.0, 0.5, -2.25, 1e-300, 1.7e308))
    if kind in (4, 5, 6):
        return text(rng)
    if kind == 7:
        return [json_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {text(rng, 6): json_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}
