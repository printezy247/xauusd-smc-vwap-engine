# XAUUSD SMC VWAP Engine — upgrade plan

`pine/v1-VWAP-aly.pine` is the original, kept as the regression baseline.
`pine/v2-VWAP-aly.pine` is the P0 truth pass. Reference for the dashboard and
alert work: `EzyMapPro_v1_8.pine` (Pine v6, local copy at
`~/EzyMap/designresources/EzyMap/PineScripts/`).

## Status

| Phase | Scope | State |
|---|---|---|
| **P0** | Truth pass — stop the script lying | **done** |
| **P2** | Position lifecycle (no state machine exists yet) | **done** |
| **P4** | Alert engine rebuild, EzyMap style | **done** |
| **P3** | Dashboard rebuild, EzyMap style | **done** |
| **P1** | Structure integrity | **done** |
| **P5** | Confluence scoring | **done** |
| **P6** | Risk model + modes | **done** |
| **P7** | v6 migration + backtest harness | **done** |
| **P8** | Reliability pass — offline compile gate + lint rules | **done** |

Build order is **P0 → P2 → P4 → P3 → P1 → P5 → P6 → P7 → P8**. Position lifecycle
before the dashboard, because the card renders state that does not exist yet.

## P0 — done

- BOS/CHOCH converted from latched `var bool` to momentary series bools. They
  used to latch true on the first break of the session and never reset, which
  made every later "BOS" a re-firing, drew a label on every bar, and reduced
  the entry rules to a plain `vwap_bull` test.
- Breaks now classify against the swing level in force *before* the bar's
  pivot confirmation. The first break of a series no longer misreports as BOS.
- `smc_state` added so the dashboard shows the last real event.
- Deleted dead code: `eq_h`, `eq_l`, `eq_tolerance`, `cum_vol`, `m30_bias`,
  `m5_bias` (6 wasted `request.security` calls), `show_fib`.
- `barOK = barstate.isconfirmed` gates every state-mutating and drawing branch,
  so realtime ticks no longer duplicate FVG/OB array pushes.
- ATR volatility filter is now a ratio against ATR's own baseline instead of a
  raw number that meant different things at gold 2400 vs 4000.
- TP ladder 2R/3R now plotted; previously computed and never rendered.
- Alerts: BOS bear, CHOCH bear, liquidity sweep buy, and bearish VWAP reclaim
  added; `{{ticker}}` replaces hardcoded symbols; strict `<` cross fixed to
  `<=` so exact touches are not skipped.

## Still open

- **BEHAVIOUR CHANGE (P6): fixed 1R/2R/3R ladder is gone.** SL/TP now come
  from the mode-driven EzyMap G3 model (SCALP/INTRADAY/SWING): volatility
  floor, S&R front-run, min/max RR clamp, ATR/risk fallback. TP1 is the
  partial halfway to plan, TP the runner for the full plan. `atr_sl_mult`
  deleted (superseded by per-mode stop ATR multiples); `t_tp2` and its
  plot deleted (no other reader). Trade card, R:R readout and LIVE alert
  now quote the final target, not TP1.
- **ANCHOR FIX (P6):** anchored VWAP defaults to the session open and
  restarts daily ("Swing Leg" / "Fixed Date" selectable); bands follow
  the active line (B6). Manual `var` sums also cleared the two `ta.cum`
  v6 blockers a phase early. `volume` is nz-guarded now.
- **BEHAVIOUR CHANGE (P5): min_score → min_conf.** The signal gate was a
  0-100 weighted sum (`bull_score >= min_score`, default 60). It is now a
  count of agreeing independent sources (`conf_bull_n >= min_conf`,
  default 7 of 15). Old workspaces keep no equivalent threshold — anyone
  carrying a tuned `min_score` must retune `min_conf` from scratch, and
  the P7 backtest harness exists for exactly that. The weighted
  `bull_score`/`bear_score` are still computed but only feed the trade
  card's MODE line (`n/100` display); they no longer gate entries.
- **P7 CAVEAT, narrowed by P8.** `pine/v4-VWAP-aly.pine` (`//@version=6`) has
  compiled and run on TradingView, and now every push transpiles all five
  sources offline with PineTS before CI goes green (see CI). TradingView's
  compiler is still the only authority: PineTS accepts `alertcondition` string
  concatenation and `var bool x = na`, which it should reject, so those two are
  enforced by the linter instead. `pine/strategy-backtest.pine` has only ever
  been transpiled offline — paste it before quoting any number. It models
  entries + TP1 partial + runner + SL only: no break-even, no trailing. Costs
  are no longer zero (slippage 2 ticks, commission 0 for a spread-only account)
  and `max_dd_halt` stops entry after a ruinous stretch. Eyeball its fills
  against the indicator's markers.
- ~~OB array never shrinks~~ — closed by P1 (mitigation/max-age eviction
  via `array.remove`; SL reads the nearest relevant OB).
- ~~FVG/OB unbounded, boxes untied~~ — closed by P1 (bounded arrays,
  `box.delete` tied to array state).
- VWAP bands use `stdev(price)`, not VWAP variance. The anchored half is
  fixed (P6/B6: bands follow the active line); the variance definition is
  still open.
- ~~Fib from two independent pivots~~ — closed by P1 (real swing leg).
- ~~No position state machine~~ — closed by P2 (IDLE/ACTIVE/TP/SL/BE with
  partials, break-even, trailing, blended P/L).
- ~~`ta.cum` blocks v6~~ — closed by P6 (manual `var` sums in v3, carried
  into v4). v1/v2 baselines still carry it and the linter still flags
  them; they are frozen and that is expected.

## CI

Two independent jobs. `pine-lint` is pure stdlib and always runs;
`pine-transpile` needs the network for `npm ci`, so a registry outage degrades
to "lint only" instead of failing the build on an unrelated error.

### pine-lint (`tools/pine_lint.py`)

Structural, no compiler behind it, so it targets the failure modes that
actually bit this script:

- `request.security` without `barmerge.lookahead_off` (repaint) — **error**
- **undeclared identifier / forward reference** (Pine has none) — **error**
- **`alertcondition` message that is not a const string** — **error**
- **`strategy.exit` `profit=`/`loss=` given a float** — **error**
- **`var bool x = na` on v6** (bool can never be na) — **error**
- `var bool` assigned true and never reset — **warn**, this is the P0 bug class
- Pine v6 removals (`ta.cum`, bare `iff`/`offset`/`rnd`) — **warn**
- alert messages hardcoding a symbol — **warn**
- unbalanced brackets, tab indentation — **error**
- duplicate global declarations (`=` redeclared; `:=` is reassignment) — **error**
- object creation inside a loop — **warn**
- version pragma sanity, indent multiples of 4 — **error** / **warn**

Two escape hatches, both deliberate: `v1`/`v2` are frozen regression baselines
so *new* error classes are reported to them as warnings rather than failing the
build on files we must not edit, and a `// latch-ok` marker on a `var bool`
declaration exempts a latch that is meant to be permanent (the DD halt).

### pine-transpile (`tools/pine_compile.mjs`)

Runs `Indicator.prepare()` from PineTS — transpile plus scope resolution, no
data provider, no bar simulation — over every source. This is the check the
linter cannot make: it resolves real scopes, so an undeclared identifier fails
the build with the identifier named. It is proven able to fail: broken copies
produced for undeclared identifier, duplicate global and truncated source all
exit non-zero while the real files pass.

PineTS is a pre-check, not a substitute. Where the two disagree, TradingView
wins.

Run locally:

```
python3 tools/pine_lint.py --selftest
python3 tools/pine_lint.py pine/*.pine
node tools/pine_compile.mjs pine/*.pine   # npm install --prefix tools first
```

## Reference cross-check (P8)

Definitions compared against `joshyattridge/smart-money-concepts` (2k+ stars,
also the source of `smc.py`'s test fixtures). **No defects found** — every
geometric definition matches. Four deliberate divergences, all defensible, all
left as they are because changing any of them would retune a signal path:

- **FVG** — geometry is identical: bullish gap is `high[2] < low` with
  top = `low`, bottom = `high[2]`; bearish is the mirror. The direction filter
  differs: ours requires the middle candle to *displace* through the prior
  candle's extreme (`close[1] > high[2]`), the reference only requires that
  candle be bullish. Ours is a displacement filter, not a candle-colour filter.
- **BOS/CHOCH** — both measure the break on the close (the reference's
  `close_break=True` default). The reference requires a full four-swing
  pattern *including* a higher-low before calling it a BOS; ours tracks only
  the prior swing's HH/LH direction. Ours adds a displacement gate on CHOCH,
  which the reference does not have — deliberately stricter, not looser.
- **OB** — both take the last opposite-colour candle before a strong move
  (`close_index - 1` in the reference, `high[1]`/`low[1]` here), so the box
  lands on the same candle. The trigger differs: ours fires on an ATR
  displacement breaking the prior candle's extreme, the reference on a break
  of the last swing. The reference also scores OB strength with volume; this
  engine reads no volume at all.
- **Mitigation** — ours kills a block only when price *closes* through it; the
  reference's default (`close_mitigation=False`) is a wick. Ours survives
  intrabar pokes, which is the stricter and more common choice.

If entries ever feel too loose, the OB trigger is the one to revisit first —
it is the only source with no structural condition behind it.

### Considered and rejected

- **`max_bars_back`** on `indicator()`/`strategy()`. It only guards RE10139
  ("referencing variable has too long of a history"), which needs deep or
  variable-indexed history references. Neither file has any: zero
  variable-indexed refs, and the deepest literal index is 2 bars
  (`close[1]`, `high[2]`). Docs say use it only when needed and keep the value
  minimal, so adding it would be configuration that feeds nothing.
