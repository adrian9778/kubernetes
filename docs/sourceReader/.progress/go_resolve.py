#!/usr/bin/env python3
"""Resolve `file.go:NNN` refs with per-doc domain disambiguation.

Domain hints: each doc maps to a set of path prefixes that disambiguate
ambiguous basenames. A ref resolves to the candidate whose path best matches
the doc's domain (longest matching prefix); if exactly one candidate remains,
resolve it. Otherwise flag AMBIGUOUS.

Usage: go_resolve.py <doc.md> [domain_prefix ...]
Prints: doc_line | ref | resolved_symbol+offset | path | snippet
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from go_anchor import build, resolve, fmt_off, _lines

REPO = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                      capture_output=True, text=True).stdout.strip()

REF_RE = re.compile(r'(?<![\w/.])([A-Za-z_][A-Za-z0-9_]*\.go):(\d+)((?:[-~,]\d+)*)')


def go_paths():
    out = subprocess.run(['git', 'ls-files', '*.go'], capture_output=True,
                         text=True, cwd=REPO).stdout.split('\n')
    return [p for p in out if p.endswith('.go') and not p.startswith('vendor/')]


def pick(cands, domains):
    """Prefer candidate whose path contains the most domain substrings; tie-break shortest path."""
    def score(p):
        return sum(1 for d in domains if d in p)
    scored = [(score(p), -len(p), p) for p in cands]
    scored.sort(reverse=True)
    top = scored[0]
    if top[0] == 0:
        return None  # no domain helped
    winners = [p for p in cands if score(p) == top[0]]
    return winners[0] if len(winners) == 1 else None


def main():
    doc = sys.argv[1]
    domains = sys.argv[2:]
    # explicit overrides: basename=canonical/repo/path.go
    overrides = {}
    for d in list(domains):
        if '=' in d:
            b, p = d.split('=', 1)
            overrides[b] = p
            domains.remove(d)
    text = open(doc, encoding='utf-8').read()
    paths = go_paths()
    bidx = {}
    for p in paths:
        bidx.setdefault(os.path.basename(p), []).append(p)
    cache = {}
    for ln_no, line in enumerate(text.split('\n'), 1):
        for m in REF_RE.finditer(line):
            base, start, rest = m.group(1), int(m.group(2)), m.group(3)
            if base in overrides:
                gpath = overrides[base]
            else:
                cands = bidx.get(base, [])
                if not cands:
                    print(f"L{ln_no}\t{base}:{start}{rest}\tNOFILE"); continue
                if len(cands) == 1:
                    gpath = cands[0]
                else:
                    gpath = pick(cands, domains)
                    if gpath is None:
                        print(f"L{ln_no}\t{base}:{start}{rest}\tAMBIG ({len(cands)})"); continue
            full = os.path.join(REPO, gpath)
            if gpath not in cache:
                cache[gpath] = build(full)
            sym, off = resolve(cache[gpath], start)
            src = _lines(full)
            snip = src[start-1].strip()[:50] if 1 <= start <= len(src) else '<eof>'
            res = f"{sym}{fmt_off(off)}" if sym else "FILELEVEL"
            print(f"L{ln_no}\t{base}:{start}{rest}\t{res}\t{gpath}\t| {snip}")


if __name__ == '__main__':
    main()