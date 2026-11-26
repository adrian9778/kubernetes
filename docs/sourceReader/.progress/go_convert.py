#!/usr/bin/env python3
"""Resolve all `file.go:NNN` refs in a doc to symbol+offset, producing a report.

Usage: go_convert.py <doc.md> [--dry]
- Finds every `basename.go:NNN` (and NNN-NNN ranges) in the doc.
- Resolves basename -> repo path (unique match required).
- Resolves NNN -> enclosing symbol+offset via go_anchor.
- Prints a table: doc_line | ref | resolved | source_snippet.
--dry: report only (default). Without --dry, also prints suggested rewrites.
"""
import re
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from go_anchor import build, resolve, fmt_off, _lines

REPO = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                      capture_output=True, text=True).stdout.strip()


def find_go_paths():
    out = subprocess.run(['git', 'ls-files', '*.go'], capture_output=True,
                         text=True, cwd=REPO).stdout.split('\n')
    return [p for p in out if p.endswith('.go')]


def basename_index(paths):
    idx = {}
    for p in paths:
        b = os.path.basename(p)
        idx.setdefault(b, []).append(p)
    return idx


# match basename.go:NNN  (with optional range -NNN or ~NNN or ,NNN)
REF_RE = re.compile(r'(?<![\w/.])([A-Za-z_][A-Za-z0-9_]*\.go):(\d+)((?:[-~,]\d+)*)')


def main():
    doc = sys.argv[1]
    dry = '--dry' in sys.argv or True  # always report; rewrite is manual/reviewed
    text = open(doc, encoding='utf-8').read()
    idx = basename_index(find_go_paths())
    cache = {}
    lines = text.split('\n')
    for ln_no, line in enumerate(lines, 1):
        for m in REF_RE.finditer(line):
            base, start, rest = m.group(1), int(m.group(2)), m.group(3)
            paths = idx.get(base, [])
            if not paths:
                print(f"L{ln_no}\t{base}:{start}\t?? NO SUCH FILE")
                continue
            if len(paths) > 1:
                # disambiguate by context later; list candidates
                cands = ' | '.join(paths[:4])
                print(f"L{ln_no}\t{base}:{start}\t?? AMBIGUOUS ({len(paths)}): {cands}")
                continue
            gpath = paths[0]
            full = os.path.join(REPO, gpath)
            if gpath not in cache:
                try:
                    cache[gpath] = build(full)
                except Exception as e:
                    cache[gpath] = None
                    print(f"L{ln_no}\t{base}:{start}\t?? READ ERR {e}")
                    continue
            anchors = cache[gpath]
            if anchors is None:
                continue
            sym, off = resolve(anchors, start)
            src_lines = _lines(full)
            snippet = src_lines[start - 1].strip()[:55] if 1 <= start <= len(src_lines) else '<eof>'
            resolved = f"{sym}{fmt_off(off)}" if sym else "?? NO SYMBOL (file-level/const?)"
            print(f"L{ln_no}\t{base}:{start}{rest}\t{resolved}\t| {snippet}")


if __name__ == '__main__':
    main()