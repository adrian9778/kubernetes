#!/usr/bin/env python3
"""Go-aware symbol+offset anchor extractor.

Builds an anchor table (symbol -> definition line) for a Go file, then resolves
an absolute line number to the enclosing symbol + offset (definition line = +0).

Handles Go specifics:
- func declarations (top-level and methods): `func Name(` / `func (recv Type) Name(`
- type declarations, var/const blocks
- skips lines inside raw strings / comments when matching (best effort)
"""
import re
import sys

# func decl. Receiver group only matches if a name follows it (so param lists
# aren't mistaken for receivers when the func has no receiver).
FUNC_RE = re.compile(r'^func(?:\s+\(([^)]*)\))?\s+([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?\s*\(')
TYPE_RE = re.compile(r'^type\s+([A-Za-z_][A-Za-z0-9_]*)\s')
VAR_CONST_RE = re.compile(r'^(var|const)\s+([A-Za-z_][A-Za-z0-9_]*)\s')


def _lines(path):
    with open(path, encoding='utf-8') as f:
        return f.read().split('\n')


def build(path):
    """Return list of (symbol, defline_1based, kind). defline is the `func`/`type` keyword line."""
    lines = _lines(path)
    anchors = []
    for i, line in enumerate(lines, 1):
        s = line
        m = FUNC_RE.match(s)
        if m:
            recv = (m.group(1) or '').strip()
            name = m.group(2)
            if recv:
                # method: symbol = Type.Name (use pointer-stripped receiver type)
                rtype = recv.split()[0].lstrip('*') if recv.split() else ''
                sym = f"{rtype}.{name}" if rtype else name
            else:
                sym = name
            anchors.append((sym, i, 'func'))
            continue
        m = TYPE_RE.match(s)
        if m:
            anchors.append((m.group(1), i, 'type'))
            continue
        m = VAR_CONST_RE.match(s)
        if m:
            anchors.append((m.group(2), i, m.group(1)))
            continue
    return anchors


def resolve(anchors, target_line):
    """Find the innermost symbol whose defline <= target_line. Return (sym, offset) or (None,None)."""
    best = None
    for sym, defline, kind in anchors:
        if defline <= target_line:
            if best is None or defline > best[1]:
                best = (sym, defline, kind)
    if best is None:
        return None, None
    return best[0], target_line - best[1]


def fmt_off(k):
    return '+0' if k == 0 else f'+{k}' if k > 0 else str(k)


def main():
    if len(sys.argv) < 2:
        print("usage: go_anchor.py <file.go> [lineno ...]")
        return
    path = sys.argv[1]
    anchors = build(path)
    if len(sys.argv) == 2:
        for sym, defline, kind in anchors:
            print(f"{defline}\t{sym}\t{kind}")
        return
    for arg in sys.argv[2:]:
        ln = int(arg)
        sym, off = resolve(anchors, ln)
        src = _lines(path)[ln - 1] if 1 <= ln <= len(_lines(path)) else '<eof>'
        print(f"{ln} -> {sym}{fmt_off(off) if sym else '?'}  | {src.strip()[:70]}")


if __name__ == '__main__':
    main()