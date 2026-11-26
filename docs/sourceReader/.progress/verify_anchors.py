#!/usr/bin/env python3
"""Reverse-verify Symbol+offset anchors in a topic against actual source.

For each `Symbol+offset` anchor found in the topic, locate the symbol's
definition line in the referenced source file, compute abs_line = defline+offset,
and print the source line for manual semantic confirmation.

Usage: verify_anchors.py <topic.md> <path.go> [<path.go> ...]
Searches the union of given files for each symbol. Reports:
  OK   - symbol found, abs_line source shown
  MISS - symbol not found in any given file
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from go_anchor import build, _lines, fmt_off

REPO = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                      capture_output=True, text=True).stdout.strip()
ANCHOR_RE = re.compile(r'`([A-Za-z_][A-Za-z0-9_.]*)\+(-?\d+)(?:~\+?(\d+))?`')


def main():
    doc = sys.argv[1]
    files = sys.argv[2:]
    # build anchor tables per file
    tables = {}
    for f in files:
        full = os.path.join(REPO, f)
        try:
            tables[f] = build(full)
        except Exception as e:
            print(f"READ ERR {f}: {e}")
    # symbol -> (file, defline) index (last-wins per file, prefer first found)
    sym_index = {}
    for f, anchors in tables.items():
        for sym, defline, kind in anchors:
            key = sym.split('.')[-1]  # match by short name
            if key not in sym_index:
                sym_index[key] = (f, defline, sym)
    text = open(doc, encoding='utf-8').read()
    seen = set()
    ok = miss = 0
    for m in ANCHOR_RE.finditer(text):
        sym, off_s, off_e = m.group(1), int(m.group(2)), m.group(3)
        key = sym.split('.')[-1]
        if key in seen:
            continue
        seen.add(key)
        if key not in sym_index:
            print(f"MISS  {sym}+{off_s}  (not in given files)")
            miss += 1
            continue
        f, defline, full_sym = sym_index[key]
        abs_line = defline + off_s
        src = _lines(os.path.join(REPO, f))
        snippet = src[abs_line - 1].strip()[:60] if 1 <= abs_line <= len(src) else '<eof>'
        print(f"OK    {sym}{fmt_off(off_s)}  @{f}:{abs_line}  | {snippet}")
        ok += 1
    print(f"\n--- OK={ok} MISS={miss} ---")


if __name__ == '__main__':
    main()