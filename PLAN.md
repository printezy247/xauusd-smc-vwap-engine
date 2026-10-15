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

Build order is **P0 → P2 → P4 → P3 → P1 → P5 → P6 → P7**. Position lifecycle
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
- **P7 BLIND-MIGRATION CAVEAT.** `pine/v4-VWAP-aly.pine` (`//@version=6`)
  and `pine/strategy-backtest.pine` have never been compiled — no offline
  Pine compiler exists, the linter is structural only. v3 stays as the
  known-good v5 fallback. The backtest models entries + TP1 partial +
  runner + SL only: no break-even, no trailing, zero costs, plan priced
  off the signal close with next-open fills. Quote no number from it
  until it compiles clean on TradingView and its fills are eyeballed
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

`tools/pine_lint.py` has no offline Pine compiler to lean on, so it targets the
failure modes that actually bit this script:

- `request.security` without `barmerge.lookahead_off` (repaint) — **error**
- `var bool` assigned true and never reset — **warn**, this is the P0 bug class
- Pine v6 removals (`ta.cum`, bare `iff`/`offset`/`rnd`) — **warn**
- alert messages hardcoding a symbol — **warn**
- unbalanced brackets, tab indentation — **error**
- duplicate global declarations (`=` redeclared; `:=` is reassignment) — **error**
- object creation inside a loop — **warn**
- version pragma sanity, indent multiples of 4 — **error** / **warn**

Run locally:

```
python3 tools/pine_lint.py --selftest
python3 tools/pine_lint.py pine/*.pine
```
