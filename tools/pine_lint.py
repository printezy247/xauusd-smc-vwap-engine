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

# --- forward-reference scanning -------------------------------------------------
# Pine has no forward references: every bare identifier must be declared on an
# earlier line. This is the failure mode that cost the most TradingView paste
# round-trips (atr_val, fvg_max, conf_dir), so the linter owns it rather than
# depending on the network-backed compile gate being available.
KEYWORDS = set(
    "if else for while switch and or not to by from in var varip const "
    "true false na int float bool string color import as export method type".split()
)
# Script builtins and namespaces, plus the drawing/API types that only ever
# appear qualified. Anything preceded or followed by `.` is dropped anyway;
# this list covers the bare-token case.
BUILTINS = set(
    "open high low close volume hlc3 hl2 ohlc4 hlcc4 time timenow time_close "
    "bar_index barstate timeframe syminfo nz timestamp dayofmonth dayofweek "
    "weekofyear hour minute second month year session chart "
    "plot plotshape plotchar plotcandle plotbar plotarrow hline fill bgcolor "
    "barcolor alert alertcondition strategy indicator library study input "
    "array matrix table label line box polyline linefill ta math str request "
    "color size text position display hline plot "
    "int float bool string const var varip".split()
)

# `x = ...` / `int x = ...` / `var float x = ...`
# The type (if any) must be a whitespace-delimited token of its own. Without the
# trailing \s+ the optional type group backtracks into the name and matches only
# its last character — `show_vwap = ...` declared "p".
_TYPE_TOKEN = r"(?:(?:array|matrix)<[^>]+>|[\w.]+(?:\[\])?)\s+"
DECL_TYPED = re.compile(
    r"^(?:(?:const|var|varip)\s+)?"
    r"(?:" + _TYPE_TOKEN + r")*"
    r"([A-Za-z_]\w*)\s*=(?!=)"
)
DECL_BARE = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)")
# `[pdh, pdl] = request.security(...)` — tuple destructuring declares each name.
DECL_TUPLE = re.compile(r"^\[\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*\]\s*=(?!=)")
# `f(x, y) =>` and `f(int x, color bg = na) =>`
DECL_FUNC = re.compile(r"^([A-Za-z_]\w*)\s*\((.*)\)\s*=>")
DECL_FOR = re.compile(r"^for\s+([A-Za-z_]\w*)\s*=")
IDENT = re.compile(r"[A-Za-z_]\w*")
# Blank comparison/reassignment operators so `a == b` and `a := b` keep `a` as a
# use (it must already be declared), while `a = b` treats `a` as the declaration.
OP_BLANK = re.compile(r"<=|>=|==|:=|=>|!=")
LHS_BLANK = re.compile(r"[A-Za-z_]\w*(?=\s*=(?!=))")


def _split_params(sig):
    """Split a function signature on top-level commas."""
    parts, depth, buf = [], 0, []
    for ch in sig:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _declared_names(stripped):
    """Names this line declares, or [] if it declares nothing."""
    if not stripped:
        return []
    m = DECL_TUPLE.match(stripped)
    if m:
        return [n.strip() for n in m.group(1).split(",")]
    m = DECL_FUNC.match(stripped)
    if m:
        names = [m.group(1)]
        for part in _split_params(m.group(2)):
            head = part.split("=")[0]
            idents = [t for t in IDENT.findall(head) if t not in KEYWORDS]
            if idents:
                names.append(idents[-1])
        return names
    if stripped.startswith("for "):
        m = DECL_FOR.match(stripped)
        if m:
            return [m.group(1)]
        return []
    m = DECL_TYPED.match(stripped) or DECL_BARE.match(stripped)
    if m and m.group(1) not in KEYWORDS:
        return [m.group(1)]
    return []


def check_forward_refs(raw_lines, code_lines, err):
    """Every bare identifier must be declared earlier — Pine has no forward refs."""
    declared = {}  # name -> first declaration line
    uses = []  # (line, name)

    for i, (raw, code) in enumerate(zip(raw_lines, code_lines)):
        ln = i + 1
        stripped = code.strip()
        if raw.lstrip().startswith("//") or not stripped:
            continue
        for name in _declared_names(stripped):
            declared.setdefault(name, ln)

        # Blank operators, then the LHS/named-arg target of a single `=`.
        probe = LHS_BLANK.sub("   ", OP_BLANK.sub("     ", code))
        for tm in IDENT.finditer(probe):
            name, s, e = tm.group(0), tm.start(), tm.end()
            if s > 0 and probe[s - 1] == ".":
                continue  # member access: table.cell / syminfo.mintick
            if e < len(probe) and probe[e] == ".":
                continue  # namespace: ta.atr / strategy.entry
            if name in KEYWORDS or name in BUILTINS:
                continue
            uses.append((ln, name))

    seen = set()
    for ln, name in uses:
        if name in declared:
            if declared[name] > ln and (name, "fwd") not in seen:
                seen.add((name, "fwd"))
                err.append((ln, f"forward reference to `{name}` — declared at line {declared[name]}, Pine has no forward declarations"))
        elif (name, "undecl") not in seen:
            seen.add((name, "undecl"))
            err.append((ln, f"undeclared identifier `{name}`"))


def check_bool_na(raw_lines, version, err):
    """v6: `bool can never be na` — TradingView rejects `var bool x = na` (CE10173)."""
    if version < 6:
        return
    for i, line in enumerate(raw_lines):
        if re.search(r"\bvar\s+bool\s+\w+\s*=\s*na\b", line):
            err.append((i + 1, "`var bool ... = na` is invalid in Pine v6 (bool can never be na) — default it to false"))


def _cut_comment(line):
    """Drop a trailing // comment, ignoring one inside a string literal."""
    in_str = False
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "/" and i + 1 < n and line[i + 1] == "/":
            return line[:i]
        i += 1
    return line


def check_alertcondition_const(raw_lines, err):
    """alertcondition's message must be a const string: no `+`, no interpolation.

    TradingView enforces this on v5 and v6 alike; concatenation yields a series
    string and the call fails to compile. Runs on raw lines because the message
    is the string literal strip_noise() blanks away.
    """
    for i, raw in enumerate(raw_lines):
        if "alertcondition" not in raw:
            continue
        stmt = _cut_comment(raw)
        j = i
        while stmt.count("(") > stmt.count(")") and j + 1 < len(raw_lines):
            j += 1
            stmt += " " + _cut_comment(raw_lines[j])
        if "alertcondition" not in stmt:
            continue
        body = stmt[stmt.index("alertcondition") + len("alertcondition"):]
        # Drop the call's own wrapping parens so top-level commas are at depth 0
        # and the message part is not polluted by the final `)`.
        body = body.lstrip()
        if body.startswith("("):
            body = body[1:]
        if body.rstrip().endswith(")"):
            body = body.rstrip()[:-1]
        parts, depth, buf = [], 0, []
        in_str = False
        for ch in body:
            if ch == '"':
                in_str = not in_str
            if not in_str:
                if ch in "([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
                if ch == "," and depth == 0:
                    parts.append("".join(buf))
                    buf = []
                    continue
            buf.append(ch)
        parts.append("".join(buf))
        if len(parts) < 3:
            continue
        if not re.fullmatch(r'\s*"[^"]*"\s*', parts[2]):
            err.append((i + 1, "alertcondition message must be a const string literal — concatenation is a compile error"))


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


def check_latches(raw_lines, code_lines, warn):
    """A `var bool` set true and never set false stays true for the session.

    That is exactly how the v1 BOS/CHOCH flags fired once per script load.

    Some latches are the whole point — a drawdown halt must not reset. Mark
    those `// latch-ok` on the declaration line; the marker lives in the raw
    text because strip_noise() has already blanked the comments in code_lines.
    """
    declared = {}
    for i, line in enumerate(code_lines):
        m = re.search(r"\bvar\s+bool\s+(\w+)", line)
        if m:
            declared[m.group(1)] = "latch-ok" not in raw_lines[i]
    for name in sorted(declared):
        if not declared[name]:
            continue
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


def check_strategy_ticks(code_lines, err):
    """strategy.exit's `profit`/`loss` are series int — passing a float fails.

    `limit`/`stop` are prices (series float) and are fine; only the tick-count
    pair is an int. Flagged structurally: a float literal in the value, or a
    name declared with an explicit `float` type.
    """
    float_names = set()
    for line in code_lines:
        m = re.match(r"^\s*float\s+([A-Za-z_]\w*)\s*=", line)
        if m:
            float_names.add(m.group(1))
    for i, line in enumerate(code_lines):
        if not re.search(r"\bstrategy\.(exit|close|close_all)\s*\(", line):
            continue
        stmt, j = line, i
        while stmt.count("(") > stmt.count(")") and j + 1 < len(code_lines):
            j += 1
            stmt += " " + code_lines[j]
        for m in re.finditer(r"\b(profit|loss)\s*=\s*([^,)]+)", stmt):
            value = m.group(2)
            has_float_lit = re.search(r"\d+\.\d+", value)
            hits_float = sorted(n for n in float_names if re.search(rf"\b{re.escape(n)}\b", value))
            if has_float_lit or hits_float:
                detail = f"`{hits_float[0]}` is a float" if hits_float else f"float literal in {value.strip()}"
                err.append((i + 1, f"strategy.exit {m.group(1)}= must be an integer tick count ({detail}) — wrap in int()"))


def lint_text(text, frozen=False):
    raw = text.split("\n")
    code = strip_noise("\n".join(raw))
    err, warn = [], []
    m = next((re.match(r"//@version=(\d+)", l.strip()) for l in raw if l.strip().startswith("//@version")), None)
    version = int(m.group(1)) if m else 5
    check_version(raw, err, warn)
    check_lookahead(code, err)
    check_latches(raw, code, warn)
    check_v6_blockers(code, warn)
    check_objects_in_loop(code, warn)
    check_indent(raw, code, err, warn)
    check_alert_ticker(raw, warn)
    check_duplicates(code, err)
    # Frozen baselines are kept byte-for-byte for regression diffing, so new
    # error classes are reported as warnings there instead of failing the build.
    check_alertcondition_const(raw, warn if frozen else err)
    check_strategy_ticks(code, err)
    check_bool_na(raw, version, err)
    check_forward_refs(raw, code, err)
    check_balance(code, err)
    return err, warn


# Kept unmodified as regression baselines — see PLAN.md. Their contents are not
# ours to fix, so they are reported but never allowed to fail the build.
FROZEN_BASELINES = {"v1-VWAP-aly.pine", "v2-VWAP-aly.pine"}


def lint(path):
    p = pathlib.Path(path)
    return lint_text(p.read_text(encoding="utf-8", errors="replace"), frozen=p.name in FROZEN_BASELINES)


def check_duplicates(code_lines, err):
    """A name declared twice at global scope is a Pine compile error.

    Easy to introduce while threading a rename through a long script, and
    the linter is the only thing standing between that and a paste error.

    Only `=` declares. `:=` reassigns an already-declared name (a bare
    `x := 1` with no prior `x = ...` is itself a Pine compile error), so
    reassignment is not tracked here.
    """
    typed = re.compile(
        r"^(?:(?:const|var|varip)\s+)?"
        r"(?:(?:(?:array|matrix)<[^>]+>|[\w.]+(?:\[\])?)\s+)*"
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
        ("//@version=6\nvar bool halt = false  // latch-ok: one-way halt\nif c\n    halt := true\n", "latched true", 0),
        ('//@version=6\nalertcondition(c, "BOS on XAUUSD")\n', "hardcodes a symbol", 1),
        ('//@version=6\nalertcondition(c, "BOS on {{ticker}}")\n', "hardcodes a symbol", 0),
        ("//@version=6\nrequest.security(s, tf, close, gaps_off)\n", "lookahead_off", 1),
        ("//@version=6\nrequest.security(s, tf, close, barmerge.lookahead_off)\n", "lookahead_off", 0),
        ('//@version=6\nf() =>\n    str.tostring(x) + " (a) ("\n', "unbalanced", 0),
        ("//@version=6\nf() =>\n    a(1\n", "unbalanced", 1),
        ("//@version=6\nfor i = 0 to 3\n    label.new(bar_index, high)\n", "inside a loop", 1),
        ("//@version=6\nmin_conf = input.int(7)\nmin_conf = input.int(9)\n", "duplicate global", 1),
        ("//@version=6\nint min_conf = 7\nint min_conf = 9\n", "duplicate global", 1),
        ("//@version=6\nvar int a = 0\nif c\n    int a = 1\n    b := a\n", "duplicate global", 0),
        ("//@version=6\nx = 1\nx := 2\n", "duplicate global", 0),
        # Forward references / undeclared identifiers — the class that cost the
        # most TradingView paste round-trips (atr_val, fvg_max, conf_dir).
        ("//@version=6\ny = 2\nx = y + 1\n", "forward reference", 0),
        ("//@version=6\nx = y + 1\ny = 2\n", "forward reference", 1),
        ("//@version=6\nplot(zzz)\n", "undeclared identifier `zzz`", 1),
        ("//@version=6\ns = syminfo.tickerid\n[a, b] = request.security(s, \"60\", close, barmerge.lookahead_off)\nplot(a + b)\n", "undeclared identifier", 0),
        ("//@version=6\nf(int a, color bg = na) =>\n    a\nz = f(1)\n", "undeclared identifier", 0),
        # The declaration regex must capture the whole name, not its last char.
        ("//@version=6\nint show_vwap = 1\nother = show_vwap + 1\n", "undeclared identifier", 0),
        ("//@version=6\nint show_vwap = 1\nint show_vwap = 2\n", "duplicate global", 1),
        # v6 rejects `var bool x = na`; v5 allows it.
        ("//@version=6\nvar bool leg_up = na\n", "bool can never be na", 1),
        ("//@version=5\nvar bool leg_up = na\n", "bool can never be na", 0),
        # alertcondition message must be a const string on v5 and v6 alike.
        ('//@version=6\nalertcondition(c, "T", "TP " + dir)\n', "const string literal", 1),
        ('//@version=6\nalertcondition(c, "T", "TP {{ticker}}")\n', "const string literal", 0),
        # strategy.exit profit/loss are series int; limit/stop are prices.
        ("//@version=6\nstrategy.exit(\"X\", \"L\", profit=tp_ticks / 2.0)\n", "integer tick count", 1),
        ("//@version=6\nstrategy.exit(\"X\", \"L\", profit=100, limit=1.5)\n", "integer tick count", 0),
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
