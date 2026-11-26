#!/usr/bin/env python3
"""Auto-convert residual `file.go:NNN` refs in a doc to symbol+offset.

Strategy: for each `basename.go:NNN` token in the doc, look up the authoritative
Symbol+offset from go_resolve.py, then rewrite the `basename.go:NNN` token to
`Symbol+offset`. Preserves surrounding separators (/+0, ，+0, 附近, etc.) by only
replacing the `basename.go:NNN` core token.

Safety: dry-run by default. Use --apply to write. Skips refs inside the doc's
type-table notation (类型名 文件:行号) — those are intentional.
"""
import re
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from go_anchor import build, resolve, fmt_off

REPO = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                      capture_output=True, text=True).stdout.strip()
REF_RE = re.compile(r'(?<![\w/.])([A-Za-z_][A-Za-z0-9_]*\.go):(\d+)((?:[-~,]\d+)*)')


def go_paths():
    out = subprocess.run(['git', 'ls-files', '*.go'], capture_output=True,
                         text=True, cwd=REPO).stdout.split('\n')
    return [p for p in out if p.endswith('.go') and not p.startswith('vendor/')]


def load_overrides():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'overrides.txt')
    ov = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith('#') and 'is dual' not in line and '=' in line:
                b, p = line.split('=', 1)
                ov[b] = p
    return ov


def main():
    doc = sys.argv[1]
    apply = '--apply' in sys.argv
    text = open(doc, encoding='utf-8').read()
    overrides = load_overrides()
    paths = go_paths()
    bidx = {}
    for p in paths:
        bidx.setdefault(os.path.basename(p), []).append(p)
    cache = {}
    lines = text.split('\n')
    out_lines = []
    n_conv = 0
    n_skip = 0
    for ln_no, line in enumerate(lines, 1):
        new_line = line
        # process refs right-to-left to keep offsets valid
        matches = list(REF_RE.finditer(line))
        for m in reversed(matches):
            base, start, rest = m.group(1), int(m.group(2)), m.group(3)
            gpath = overrides.get(base)
            if gpath is None:
                cands = bidx.get(base, [])
                if len(cands) == 1:
                    gpath = cands[0]
                else:
                    n_skip += 1
                    continue
            full = os.path.join(REPO, gpath)
            if gpath not in cache:
                try:
                    cache[gpath] = build(full)
                except Exception:
                    cache[gpath] = []
            anchors = cache[gpath]
            sym, off = resolve(anchors, start)
            if not sym:
                n_skip += 1
                continue
            # build replacement: Symbol+offset (preserve range tail if present)
            repl = f"{sym}{fmt_off(off)}"
            if rest:
                # range: resolve end if same symbol
                m2 = re.match(r'[-~,](\d+)', rest)
                if m2:
                    end = int(m2.group(1))
                    sym2, off2 = resolve(anchors, end)
                    if sym2 == sym:
                        repl = f"{sym}{fmt_off(off)}~{fmt_off(off2)}"
            # replace only the `base:start` core (keep any trailing sep like ，+0 handled below)
            s, e = m.start(1), m.end(2)
            # De-dup: if the symbol (last dotted segment) already appears immediately
            # before this ref (in backticks or plain), drop the file.go:NNN token AND
            # any immediately-following separator like /+0 or ，+0 (avoid orphan).
            short = sym.split('.')[-1]
            before = new_line[max(0, s - 60):s]
            # extent of the whole match (base:start + trailing range/sep)
            e_full = m.end(2) + len(rest)
            # also swallow a following /+0 or ，+0 separator if present
            tail = new_line[e_full:e_full + 6]
            m_tail = re.match(r'(/|，|,)\s*\+?\d*', tail)
            if short in before:
                drop_end = e_full + (m_tail.end() if m_tail else 0)
                new_line = new_line[:s] + new_line[drop_end:]
                # collapse an now-empty paren pair left behind: （） or （ ）
                new_line = re.sub(r'（\s*）', '', new_line)
            else:
                new_line = new_line[:s] + repl + new_line[e_full:]
            n_conv += 1
        out_lines.append(new_line)
    result = '\n'.join(out_lines)
    if apply:
        open(doc, 'w', encoding='utf-8').write(result)
        print(f"APPLIED: {n_conv} converted, {n_skip} skipped")
    else:
        print(f"DRY-RUN: {n_conv} would convert, {n_skip} skipped")
        # show a diff preview
        import difflib
        for l in list(difflib.unified_diff(lines, result.split('\n'), lineterm=''))[:40]:
            print(l)


if __name__ == '__main__':
    main()