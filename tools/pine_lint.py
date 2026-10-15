#!/usr/bin/env python3
"""Structural lint for Pine Script v5/v6.

There is no offline Pine compiler, so this checks the failure modes that have
actually bitten this repo: repainting MTF requests, latched one-shot flags,
and v6 migration blockers.

Usage:  python3 tools/pine_lint.py pine/*.pine
Exit:   0 = clean or warnings only, 1 = at least one ERROR.
"""

import re
import sys
import pathlib

# Namespaced helpers dropped in Pine v6 — blocks the planned P7 migration.
V6_REMOVED_NS = ("ta.cum",)
# Bare v4-era functions dropped in Pine v6. Matched only when NOT prefixed
# with `ta.`, because `ta.crossover` is current and fine.
V6_REMOVED_BARE = ("iff", "offset", "rnd", "crossover", "crossunder")

OBJECTS = ("label.new", "box.new", "line.new", "label.set_", "box.set_")


def strip_noise(text):
    """Blank out comments and string literals, preserving line structure.

    Paren balancing and keyword matching must not see text inside a string
    like "BOS BULL (displacement)" or a trailing // comment.
    """
    out = []
    for line in text.split("\n"):
        buf, i, n = [], 0, len(line)
        in_str = False
        while i < n:
            c = line[i]
            if in_str:
                if c == "\\" and i + 1 < n:
                    buf.append(" ")
                    buf.append(" ")
                    i += 2
                    continue
                if c == '"':
                    in_str = False
                buf.append(" ")
                i += 1
                continue
            if c == '"':
                in_str = True
                buf.append(" ")
                i += 1
                continue
            if c == "/" and i + 1 < n and line[i + 1] == "/":
                buf.append(" " * (n - i))
                break
            buf.append(c)
            i += 1
        out.append("".join(buf))
    return out


def check_version(lines, err, warn):
    pragmas = [(i + 1, l) for i, l in enumerate(lines) if l.strip().startswith("//@version")]
    if not pragmas:
        err.append((0, "no //@version= pragma"))
        return
    if len(pragmas) > 1:
        err.append((pragmas[1][0], "more than one //@version= pragma"))
    line_no, pragma = pragmas[0]
    m = re.match(r"//@version=(\d+)", pragma.strip())
    if not m:
        err.append((line_no, f"malformed version pragma: {pragma.strip()}"))
    elif int(m.group(1)) not in (5, 6):
        err.append((line_no, f"unsupported Pine version {m.group(1)} (expected 5 or 6)"))


def check_lookahead(code_lines, err):
    """Any request.security without lookahead_off can repaint."""
    for i, line in enumerate(code_lines):
        if "request.security" not in line:
            continue
        # Calls can wrap; scan the logical statement until it closes.
        stmt, j = line, i
        while stmt.count("(") > stmt.count(")") and j + 1 < len(code_lines):
            j += 1
            stmt += " " + code_lines[j]
        if "barmerge.lookahead_off" not in stmt:
            err.append((i + 1, "request.security without barmerge.lookahead_off (repaints)"))


def check_latches(code_lines, warn):
    """A `var bool` set true and never set false stays true for the session.

    That is exactly how the v1 BOS/CHOCH flags fired once per script load.
    """
    declared = {
        m.group(1)
        for i, line in enumerate(code_lines)
        if (m := re.search(r"\bvar\s+bool\s+(\w+)", line))
    }
    for name in sorted(declared):
        set_true = any(re.search(rf"\b{re.escape(name)}\s*:=\s*true\b", l) for l in code_lines)
        set_false = any(re.search(rf"\b{re.escape(name)}\s*:=\s*false\b", l) for l in code_lines)
        if set_true and not set_false:
            ln = next(
                (i + 1 for i, l in enumerate(code_lines) if re.search(rf"\b{re.escape(name)}\s*:=\s*true\b", l)),
                0,
            )
            warn.append((ln, f"`var bool {name}` latched true with no reset — it fires once per load, not once per event"))


def check_v6_blockers(code_lines, warn):
    for i, line in enumerate(code_lines):
        for ns in V6_REMOVED_NS:
            if re.search(rf"\b{re.escape(ns)}\s*\(", line):
                warn.append((i + 1, f"`{ns}` is removed in Pine v6 — blocks the P7 migration"))
        for fn in V6_REMOVED_BARE:
            if re.search(rf"(?<!ta\.)\b{fn}\s*\(", line):
                warn.append((i + 1, f"bare `{fn}()` is removed in Pine v6 — use `ta.{fn}()`"))


def check_objects_in_loop(code_lines, warn):
    """Drawing inside a for-loop burns the object budget every bar."""
    depth = 0
    for i, line in enumerate(code_lines):
        stripped = line.strip()
        if re.match(r"^for\b", stripped):
            depth = 1
            continue
        if depth:
            if re.match(r"^if\b", stripped):
                depth += 1
            if any(o in line for o in OBJECTS):
                warn.append((i + 1, "object created inside a loop — drains max_labels/boxes/lines budget"))
            depth -= 1


def check_indent(raw_lines, code_lines, err, warn):
    depth = 0
    prev_cont = False
    for i, (raw, code) in enumerate(zip(raw_lines, code_lines)):
        lead = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()
        if "\t" in raw[:lead]:
            err.append((i + 1, "tab in indentation (Pine requires spaces)"))
        # A line is a continuation, not a block body, when the line above it
        # ended on an operator or left an expression open — wrapped `+` chains
        # and multi-line calls both indent to the author's alignment.
        cont = prev_cont or depth > 0
        if body and not cont and lead % 4 != 0:
            warn.append((i + 1, f"indent of {lead} spaces, not a multiple of 4"))
        prev_cont = body.endswith(("+", "\\"))
        depth += code.count("(") - code.count(")")
        depth += code.count("[") - code.count("]")


def check_alert_ticker(raw_lines, warn):
    """Alert text hardcoding a symbol lies on every other chart."""
    for i, line in enumerate(raw_lines):
        if not re.search(r"\balert(condition)?\s*\(", line):
            continue
        if "{{ticker}}" in line:
            continue
        if re.search(r'"[^"]*(XAUUSD|BTCUSD|EURUSD|GBPUSD|AUDUSD|NAS100|US30)[^"]*"', line):
            warn.append((i + 1, "alert message hardcodes a symbol and has no {{ticker}}"))


def check_balance(code_lines, err):
    depth = 0
    for i, line in enumerate(code_lines):
        depth += line.count("(") - line.count(")")
        depth += line.count("[") - line.count("]")
        if depth < 0:
            err.append((i + 1, "unbalanced brackets — closing before opening"))
            return
    if depth != 0:
        err.append((len(code_lines), f"unbalanced brackets at EOF (delta {depth:+d})"))


def lint_text(text):
    raw = text.split("\n")
    code = strip_noise("\n".join(raw))
    err, warn = [], []
    check_version(raw, err, warn)
    check_lookahead(code, err)
    check_latches(code, warn)
    check_v6_blockers(code, warn)
    check_objects_in_loop(code, warn)
    check_indent(raw, code, err, warn)
    check_alert_ticker(raw, warn)
    check_duplicates(code, err)
    check_balance(code, err)
    return err, warn


def lint(path):
    return lint_text(pathlib.Path(path).read_text(encoding="utf-8", errors="replace"))


def check_duplicates(code_lines, err):
    """A name declared twice at global scope is a Pine compile error.

    Easy to introduce while threading a rename through a long script, and
    the linter is the only thing standing between that and a paste error.

    Only `=` declares. `:=` reassigns an already-declared name (a bare
    `x := 1` with no prior `x = ...` is itself a Pine compile error), so
    reassignment is not tracked here.
    """
    typed = re.compile(
        r"^(?:const\s+|var\s+|varip\s+)?"
        r"(?:(?:array|matrix)<[^>]+>|[\w.]+(?:\[\])?)?\s+"
        r"([A-Za-z_]\w*)\s*=(?!=)\s*\S")
    bare = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)\s*\S")
    skip_decl = re.compile(r"^(?:indicator|strategy|library)\s*\(")
    keywords = {"if", "else", "for", "while", "switch", "and", "or", "not", "to", "by", "from"}
    seen = {}
    for i, line in enumerate(code_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if line[:1].isspace():
            continue  # indented = block-local, may legitimately shadow a global
        if skip_decl.match(stripped):
            continue  # indicator()/strategy() named args are not declarations
        m = typed.match(stripped) or bare.match(stripped)
        if not m:
            continue
        name = m.group(1)
        if name in keywords:
            continue
        if name in seen:
            err.append((i + 1, f"duplicate global declaration `{name}` (first at line {seen[name]})"))
        else:
            seen[name] = i + 1


def selftest():
    """Guard the guard. A silently broken linter passes CI forever."""
    cases = [
        ("//@version=5\nx = ta.cum(v)\n", "removed in Pine v6", 1),
        ("//@version=6\nvar bool hit = false\nif c\n    hit := true\n", "latched true", 1),
        ("//@version=6\nvar bool hit = false\nif c\n    hit := true\nelse\n    hit := false\n", "latched true", 0),
        ('//@version=6\nalertcondition(c, "BOS on XAUUSD")\n', "hardcodes a symbol", 1),
        ('//@version=6\nalertcondition(c, "BOS on {{ticker}}")\n', "hardcodes a symbol", 0),
        ("//@version=6\nrequest.security(s, tf, close, gaps_off)\n", "lookahead_off", 1),
        ("//@version=6\nrequest.security(s, tf, close, barmerge.lookahead_off)\n", "lookahead_off", 0),
        ('//@version=6\nf() =>\n    str.tostring(x) + " (a) ("\n', "unbalanced", 0),
        ("//@version=6\nf() =>\n    a(1\n", "unbalanced", 1),
        ("//@version=6\nfor i = 0 to 3\n    label.new(bar_index, high)\n", "inside a loop", 1),
        ("//@version=6\nmin_conf = input.int(7)\nmin_conf = input.int(9)\n", "duplicate global", 1),
        ("//@version=6\nvar int a = 0\nif c\n    int a = 1\n    b := a\n", "duplicate global", 0),
        ("//@version=6\nx = 1\nx := 2\n", "duplicate global", 0),
        ("//@version=6\nx = ta.sma(close, 20)\n", None, 0),
    ]
    failures = 0
    for src, needle, want_hit in cases:
        err, warn = lint_text(src)
        blob = "\n".join(m for _, m in err + warn)
        got = bool(blob.strip()) if needle is None else needle in blob
        if got != bool(want_hit):
            failures += 1
            print(f"  SELFTEST FAIL: expected {'hit' if want_hit else 'no hit'} for {needle!r}\n    got: {blob.strip() or '(nothing)'}")
        else:
            print(f"  ok  {needle or '(clean file yields no findings)'}")
    print("selftest: PASS" if not failures else f"selftest: {failures} FAILURE(S)")
    return failures


def main(argv):
    if argv[1:2] == ["--selftest"]:
        return 1 if selftest() else 0

    paths = argv[1:]
    if not paths:
        print("usage: pine_lint.py <file.pine> [file.pine ...] | --selftest", file=sys.stderr)
        return 2

    failures = 0
    for p in paths:
        err, warn = lint(p)
        status = "FAIL" if err else ("WARN" if warn else "PASS")
        print(f"{status}  {p}  ({len(err)} error, {len(warn)} warning)")
        for ln, msg in err:
            print(f"  ERROR  {p}:{ln}: {msg}")
        for ln, msg in warn:
            print(f"  warn   {p}:{ln}: {msg}")
        failures += len(err)

    if failures:
        print(f"\n{failures} error(s) — build failed.")
        return 1
    print("\nNo errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
