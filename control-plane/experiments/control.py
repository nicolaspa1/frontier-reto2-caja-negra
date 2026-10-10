#!/usr/bin/env python3
"""Compute the control digit (ISO 7064 MOD 97-10) of a black-box message.

Algorithm (verified): over "CMD;arg1;...;argN" only the alphanumeric
characters are kept; each letter A..Z (case-insensitive) -> 1..26 as text,
digits are kept as-is, everything is concatenated, "00" is appended,
N = int(result), control = 98 - (N % 97).

Usage:
  python3 control.py 'ALTA;Lucia'            -> prints the full message "ALTA;Lucia;82"
  python3 control.py --control 'ALTA;Lucia'  -> prints only the control "82"
"""
import sys


def control(payload: str) -> int:
    """Return the control integer for payload = 'CMD;arg1;...;argN' (no control)."""
    acc = []
    for ch in payload:
        if ch.isalpha() and ch.isascii():
            acc.append(str(ord(ch.upper()) - ord('A') + 1))
        elif ch.isdigit() and ch.isascii():
            acc.append(ch)
        # any other character (';', '-', space, non-ASCII) is discarded
    n = int("".join(acc) + "00")
    return 98 - (n % 97)


def message(payload: str) -> str:
    """Return the full message 'payload;control' ready to send."""
    return f"{payload};{control(payload)}"


if __name__ == "__main__":
    args = sys.argv[1:]
    only = False
    if args and args[0] == "--control":
        only = True
        args = args[1:]
    if not args:
        print("usage: python3 control.py [--control] 'CMD;arg1;...;argN'", file=sys.stderr)
        sys.exit(1)
    for p in args:
        print(control(p) if only else message(p))
