#!/usr/bin/env python3
"""Comparator implementing the Challenge 2 evaluator rule.

A step passes if:
  - the HTTP status is identical, and
  - the body is "equal": if BOTH bodies are valid JSON, they are compared by
    semantic equality (object keys in any order, arrays in the same order,
    strings and numbers identical); if either is not JSON, they are compared
    as EXACT text, character by character.

API:
  compare(expected, observed) -> (ok: bool, reason: str)
    where expected/observed are dicts {"status": int, "body": str}.
  bodies_equal(a, b) -> bool
  normalize(body_str) -> ("json", obj) | ("text", str)

CLI (for quick tests):
  python3 compare.py '{"status":200,"body":"{\\"a\\":1,\\"b\\":2}"}' \\
                     '{"status":200,"body":"{\\"b\\":2,\\"a\\":1}"}'
"""
import json
import sys


def _parse_json(body):
    """Return (True, obj) if body is valid JSON, (False, None) otherwise."""
    if body is None:
        return False, None
    try:
        return True, json.loads(body)
    except (ValueError, TypeError):
        return False, None


def normalize(body):
    ok, obj = _parse_json(body)
    return ("json", obj) if ok else ("text", body)


def _semantic_equal(a, b):
    """JSON equality ignoring object key order; arrays keep order, numbers/strings/bools compare by type and value."""
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(_semantic_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_semantic_equal(x, y) for x, y in zip(a, b))
    # bool is a subclass of int: do not confuse True with 1
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def bodies_equal(a, b):
    """Apply the evaluator's body rule to two body strings."""
    ta, va = normalize(a)
    tb, vb = normalize(b)
    if ta == "json" and tb == "json":
        return _semantic_equal(va, vb)
    return (a or "") == (b or "")


def compare(expected, observed):
    """expected/observed: {"status": int, "body": str}. -> (ok, reason)."""
    es, eb = expected.get("status"), expected.get("body")
    os_, ob = observed.get("status"), observed.get("body")
    if es != os_:
        return False, f"status {es} != {os_}"
    if not bodies_equal(eb, ob):
        return False, f"body differs\n   expected: {eb!r}\n   observed: {ob!r}"
    return True, "ok"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    exp = json.loads(sys.argv[1])
    obs = json.loads(sys.argv[2])
    ok, reason = compare(exp, obs)
    print("EQUAL" if ok else "DIFFERENT", "-", reason)
    sys.exit(0 if ok else 1)
