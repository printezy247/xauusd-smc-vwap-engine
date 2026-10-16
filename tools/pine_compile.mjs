#!/usr/bin/env node
// Offline transpile gate for the Pine sources.
//
// TradingView's compiler is the only authority, and it only runs on their
// site. LuxAlgo's PineTS transpiles Pine v5/v6 with no data provider, and
// Indicator.prepare() does the transpile plus the scope pass only — no bar
// simulation — which is what a CI gate wants: it resolves every identifier, so
// it catches the forward-reference class (atr_val, fvg_max, conf_dir) that
// cost the most paste round-trips, along with syntax errors and duplicate
// globals. It runs in seconds with no network beyond npm.
//
// KNOWN GAPS — PineTS accepts two things TradingView rejects:
//   - alertcondition() with a concatenated message
//   - `var bool x = na` (v6: bool can never be na)
// Both are enforced by tools/pine_lint.py instead. This is a pre-check, not a
// substitute for pasting into TradingView; when they disagree, TradingView wins.
//
// Usage: node tools/pine_compile.mjs pine/*.pine
// Exit:  0 = every file transpiled, 1 = at least one failed, 2 = bad usage.

import fs from "node:fs";
import { Indicator } from "pinets";

const files = process.argv.slice(2);
if (files.length === 0) {
  console.error("usage: node tools/pine_compile.mjs <file.pine> [...]");
  process.exit(2);
}

const inActions = process.env.GITHUB_ACTIONS === "true";
let failed = 0;

for (const file of files) {
  const started = Date.now();
  try {
    new Indicator(fs.readFileSync(file, "utf8")).prepare();
    console.log(`OK    ${file}  (${Date.now() - started}ms)`);
  } catch (err) {
    failed += 1;
    const name = err && err.name ? err.name : "Error";
    const msg = String(err && err.message ? err.message : err).replace(/\s+/g, " ");
    console.error(`FAIL  ${file}\n      ${name}: ${msg}`);
    if (inActions) {
      // Surface it on the diff instead of burying it in the log.
      console.error(`::error file=${file},title=Pine transpile failed::${name}: ${msg}`);
    }
  }
}

if (failed > 0) {
  console.error(`\n${failed} of ${files.length} file(s) failed to transpile.`);
  process.exit(1);
}
console.log(`\nAll ${files.length} file(s) transpiled.`);
