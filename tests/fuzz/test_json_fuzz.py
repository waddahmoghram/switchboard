"""Seeded mutation fuzzing of ``json_loads``; authored and unrun.

Properties: any input either parses or raises ``ValueError`` (which includes decoding errors), except that
only documents with very deep nesting may exhaust recursion. Parsed values contain only JSON types and finite
numbers, and shallow values survive a canonical round trip unchanged.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from switchboard.util import canonical_json, json_equal, json_loads
from tests.fuzz.mutation import generator, iterations, mutate

pytestmark = pytest.mark.fuzz

SEEDS: tuple[bytes, ...] = (
    b'{"tool":"switchboard_status","arguments":{"limit":5,"tags":["a","b"],"nested":{"ok":true,"none":null}}}',
    b"[0,-0,1.5,-2.25e10,9007199254740991,18446744073709551615,-9223372036854775808,1e-300,0.1]",
    b'{"text":"caf\\u00e9 \\ud83d\\ude00 \\u0000 tab\\t quote\\" slash\\/","empty":""}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x","arguments":{}}}',
    b'"\\udc00"',
    b"  \n\t 3.25e-4 \r\n",
    b"\xef\xbb\xbf{}",
    b"[[[[[[[[[[1]]]]]]]]]]",
)


def shape(value: Any) -> tuple[bool, int]:
    """Whether every node is a JSON type with finite numbers, and the nesting depth, without recursion."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    deepest = 0
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        if isinstance(node, dict):
            if not all(isinstance(key, str) for key in node):
                return False, deepest
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
        elif isinstance(node, float):
            if not math.isfinite(node):
                return False, deepest
        elif node is not None and not isinstance(node, (str, int)):
            return False, deepest
    return True, deepest


def test_json_loads_accepts_or_rejects_cleanly() -> None:
    rng = generator("json_loads")
    corpus = list(SEEDS)
    parsed = rejected = 0
    for _ in range(iterations()):
        data = mutate(rng, rng.choice(corpus), corpus)
        try:
            value = json_loads(data)
        except ValueError:
            rejected += 1
            continue
        except RecursionError:
            assert data.count(b"[") + data.count(b"{") > 100, data[:200]
            rejected += 1
            continue
        parsed += 1
        valid, depth = shape(value)
        assert valid, data[:200]
        if depth <= 100:
            text = canonical_json(value)
            assert json_equal(json_loads(text), value), data[:200]
            if len(corpus) < 256:
                corpus.append(text.encode("ascii"))
    assert parsed > 0
    assert rejected > 0


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999", b"[1e400]"])
def test_json_loads_rejects_non_finite_numbers(literal: bytes) -> None:
    with pytest.raises(ValueError, match="is not a finite JSON number"):
        json_loads(literal)
