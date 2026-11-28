<div align="center">

# JBOA-RSI PERMANENT CHANGELOG

### Versioned Release History Tied to `APP_VERSION` in `multiStreamRSI.py`

**Document ID:** `JBOA-RSI-CHANGELOG`  
**Versioning Policy:** `major.minor.build` (increment `build` on every `multiStreamRSI.py` modification; change `major` or `minor` only on explicit user instruction)

</div>

**Navigation:** [Overview (`README.md`)](../README.md) · **Changelog (`docs/CHANGELOG.md`)** · [Trading Algorithms & Risk](./TRADING_ALGORITHMS_AND_RISK.md) · [Option Buyer Guide](./OPTION_BUYER_GUIDE.md) · [Option Seller & Stop-Loss Guide](./OPTION_SELLER_AND_STOP_LOSS_GUIDE.md) · [Frontend Dashboard](./FRONTEND_DASHBOARD.md) · [Deployment & Operations](./DEPLOYMENT_AND_OPERATIONS.md) · [Configuration & API Reference](./CONFIGURATION_AND_API_REFERENCE.md)

---

> **Append Policy (`AGENTS.md` Rule 8):** Every time `APP_VERSION` in `multiStreamRSI.py` is incremented, append a dated entry tied to the new version number below so operators can inspect the full release history directly inside the live dashboard **Docs** viewer (or by clicking the **`vX.Y.Z`** version badge in the header).

---

## `v0.1.39` — 2026-11-27
- **NASDAQ Daily `%` & Buy Gate vs. Previous Trading Day's Close (`multiStreamRSI.py`):** Updated `NasdaqMarketTracker` (`diff`, `diff_pct`, `is_positive`, and `evaluate_buy_gate()`) to compare live `nasdaq_symbol` (default `QQQ`) price against the **previous trading day's official close** (`prev_close`, matching brokers, Yahoo Finance, and TradingView) instead of today's 09:30 ET open. Added NYSE-calendar-aware `_get_prev_trading_day()` and multi-source `_fetch_prev_close_sync()` (consolidated `yfinance` daily close first on Alpaca IEX free tier to avoid thin IEX open/close skew; Alpaca `previous_daily_bar` / daily bars first on SIP, with Tastytrade fallback) and automatic retry if `prev_close` is temporarily unavailable.
- **Dashboard & Log Labels:** Updated header badge tooltips, `.nasdaq-panel-chip` titles, telemetry readout (`Prev Close $XXX.XX · Now $YYY.YY`), and `OPTION_BUY_SKIPPED` / `NASDAQ_PREV_CLOSE` logs to reflect `prev_close` and `prev_close_date`.

---

## `v0.1.38` — 2026-11-26
- **Dynamic Exit Rules Bar-by-Bar Replay in Find Best (`multiStreamRSI.py`, `GET /api/findbest`):** Added `_replay_dynamic_exit_rules_timeline()` to causally replay Peak-Ratcheting Trailing Stop, Breakeven Profit Lock, and Vol/Spread-Adaptive Soft Stop-Loss minute-by-minute across a contract's captured Find Best timeline and attach `dynamic_rules.replay` to `/api/findbest`.
- **Simulated Sell Price & Delta Readout (`Find Best` Modal):** Upgraded the Dynamic Exit Rules panel to display the exact **First Trigger Timestamp** (`+Xm after buy`, bar `N/M`), **Simulated Sell Price (`$X.XX`)**, **Simulated P/L (`%` and `$`)**, and **Dollar / Percentage-Point Delta vs. Actual Exit** (or Current Mark for open trades), and tagged the matching trigger bar with an amber **`DYN EXIT`** badge in the Captured Data Points table.

---

## `v0.1.37` — 2026-11-26
- **Expanded Analyze Day Optimizer to `[general]` & `[kinematics]` (`multiStreamRSI.py`):** Extended `_ANALYZE_DAY_PARM_META`, `_simulate_trade_with_parms()`, `_synthesize_optimal_day_parms()`, and trade snapshot capture/hydration to evaluate **`[general] nasdaq_must_be_positive`**, **`[general] rsi_30d_enabled` / `rsi_30d_min`**, and **`[kinematics] smooth_method` (`hma`/`ema`/`none`) / `smooth_period` (`5`–`21`)** alongside `[option_buyer]` and `[option_seller]` settings (21 tunable parameters across 4 INI sections).
- **Logged Entry Telemetry & Causal Kinematics Velocity Replay:** Extracted entry-time `nasdaq_market.is_positive` and `rsi_30d` from `logs/minute-summary-*.jsonl` (and trade records), precomputed strictly causal `compute_smoothed_series()` underlying velocity profiles per trade for `[kinematics]` smoothing sweeps, normalized inactive sub-parameters during parsimony pruning, and added `SKIPPED (NASDAQ)` / `SKIPPED (30D RSI)` badges plus `telemetry_coverage` counts in the Analyze Day UI.

---

## `v0.1.36` — 2026-11-26
- **Analyze Day Date Range Support (`multiStreamRSI.py`, `GET /api/analyze-day`):** Enhanced **Analyze Day** to accept a **`From` / `To` date range** (defaulting both dates to **today** when opened, without overriding today's default), plus one-click range presets (**`Today`**, **`Last 7d`**, **`Last 30d`**, **`All Trades`**) alongside single-day chips.
- **Range-Wide Trade Summary & Optimal `lib/parms.ini` Synthesis:** `analyze_day_trades()` and `GET /api/analyze-day?from=YYYY-MM-DD&to=YYYY-MM-DD` (backward-compatible with `?date=YYYY-MM-DD`) now evaluate all fully executed closed trades opened and closed within the chosen US/Eastern range (including multi-day/overnight holds), render a per-day P/L summary (`daily_breakdown`) when spanning multiple days, and synthesize the single changed-only `lib/parms.ini` update set that maximizes overall combined profit for the entire date range.

---

## `v0.1.35` — 2026-11-26
- **Fixed Startup `NameError` (`multiStreamRSI.py`):** Added missing `from typing import Any, Dict, List, Optional, Tuple` import at top level to resolve `NameError: name 'Optional' is not defined` (and `Dict`) on module load.

---

## `v0.1.34` — 2026-11-26
- **Redacted Full-`lib/parms.ini` Snapshot Store (`logs/parms_snapshots/`):** Added content-addressed, secret-redacted full configuration snapshotting (`_is_secret_ini_key`, `_persist_full_parms_snapshot`, `_load_parms_snapshot_by_id`, `_list_parms_snapshots`). On startup/reload and trade execution, the engine atomically writes deduplicated `logs/parms_snapshots/parms_<hash12>.json` and human-readable `logs/parms_snapshots/parms_<hash12>.ini` files with all API keys, secrets, tokens, passwords, and credential fields replaced by `<REDACTED>`.
- **Per-Trade Snapshot Attribution & API:** Attached `parms_snapshot_id` (and `parms_snapshot_buy_id`) to open/closed contract records and buy/sell events, hydrated snapshots in **Find Best** and **Analyze Day** (displaying `#<hash8>` provenance badges in both modals), and mounted authenticated read-only `GET /api/parms-snapshot` (list or fetch by `?id=<hash12>`).

---

## `v0.1.33` — 2026-11-26
- **Recorded Per-Trade `lib/parms.ini` Snapshots (`multiStreamRSI.py`):** Added `_capture_active_trade_parms_snapshot()` and persisted active trading settings onto every option buy/sell record (`parms_snapshot` / `parms_snapshot_buy` in `OptionContractHistoryTracker` state and buy/sell events) so future **Find Best** and **Analyze Day** analyses compare against the exact settings active at execution time instead of guessing from current `lib/parms.ini`.
- **Transparent Historical Parms Provenance Badges (`Find Best` & `Analyze Day`):** Both modals now explicitly distinguish trades with recorded execution-time snapshots (`Parms: Recorded @ Trade`) from legacy pre-`v0.1.33` trades where historical settings were not recorded (`Parms: Current parms.ini (legacy trade)`).
- **Fixed Analyze Day `+$0.00` False-Change Bug & Expanded Optimizer Search:** Enforced a strict improvement floor (`synthesized_pnl > max(actual_combined_pnl, baseline_replay_pnl)`) so **Analyze Day** never suggests `lib/parms.ini` changes unless they strictly beat Actual Combined P/L, and expanded the search grid (finer trailing-stop activation/drawdown steps, observed peak anchors, pure-trailing seeds with profit-target off, and `rsi_high` up to `95`) to surface real profit-maximizing rule combinations.

---

## `v0.1.32` — 2026-11-26
- **Added Top-Level `Analyze Day` Portfolio Optimizer (`multiStreamRSI.py`, `lib/parms.ini`):** Added `[dashboard] analyze_day = false` (alias `analyze_day_enabled`, env `ANALYZE_DAY_ENABLED`) gating a new **`Analyze Day`** button next to `Docs` in the dashboard header and `GET /api/analyze-day?date=YYYY-MM-DD`.
- **Same-Day Round-Trip Synthesis Using Find Best Logic:** Prompts for a trading date (no time), analyzes all trades both opened and closed on that US/Eastern date via `analyze_findbest_contract()`, runs multi-start portfolio optimization + parsimony pruning across `OptionSeller` 4-tier exits & entry spread filters, displays **only changed `lib/parms.ini` settings** with a copyable INI diff, and compares **Actual Combined P/L vs. Synthesized P/L** per day and per contract.

---

## `v0.1.31` — 2026-11-26
- **Fixed Off-Hours Flat-Line & History Eviction Defect in `OptionContractHistoryTracker` (`multiStreamRSI.py`):** Restricted 1-minute option contract chart history points strictly to US Regular Trading Hours (`09:30–16:00 ET` on NYSE trading days via `regular_session()`) across `record_positions_snapshot()`, `_fill_minute_gaps()`, `_build_pre_entry_history()`, `_load_persisted_state()`, and persisted/log price replay. Prevents 24/7 overnight, weekend, and holiday polls from generating flat bars and evicting real post-entry intraday bars (while keeping live position KPIs continuously updated).
- **Consolidated `history_max_bars = 2400` Under `[general]` (`multiStreamRSI.py`, `lib/parms.ini`):** Unified `[kinematics] history_max_bars` and `[option_trading] history_max_bars` into a single `[general] history_max_bars` key (with backwards-compatible fallback and `CONFIG_COMPAT` logging for legacy locations), exposed `history_max_bars` at the top level of `/cli/status`, and synced documentation.

---

## `v0.1.30` — 2026-11-26
- **Folded `[option_history]` into `[option_trading]` (`multiStreamRSI.py`, `lib/parms.ini`):** Moved `history_max_bars = 2400`, `pre_entry_lookback_minutes = 390`, and `position_poll_seconds = 15` under `[option_trading]` and removed the standalone `[option_history]` section from `lib/parms.ini`.
- **Backwards-Compatible Loader & Status Visibility:** `_apply_ini_feature_settings()` reads the three keys from `[option_trading]` first and falls back to legacy `[option_history]` (logging a `CONFIG_COMPAT` warning) if an older local INI still defines them. Exposed `history_max_bars`, `pre_entry_lookback_minutes`, and `position_poll_seconds` under `option_trading` in `/cli/status` and synced documentation.

---

## `v0.1.29` — 2026-11-26
- **Consolidated `options_only` & `peak_profit_calls_only` Under `[general] options_only = true` (`multiStreamRSI.py`, `lib/parms.ini`):** Removed `[option_seller] options_only` and `[option_seller] peak_profit_calls_only`, introducing a single global `[general] options_only` setting (default `true`, env `OPTIONS_ONLY`, with backwards-compatible fallback for legacy `[option_seller] options_only`).
- **Major Gate Across Buying & Selling:** Enforced `OPTIONS_ONLY` as a major gate in `OptionBuyer.evaluate_and_buy()` (rejecting non-OCC candidates with `options_only_non_option_contract`) and `OptionSeller.evaluate_and_sell()` (skipping non-option holdings with `options_only_equity_ignored` and switching Tier 1 profit target / Tier 3 trailing stop `peak_eligible` from `peak_profit_calls_only` to `(not OPTIONS_ONLY) or is_option`). Exposed `"options_only"` in `/cli/status` and synced all documentation.

---

## `v0.1.28` — 2026-11-26
- **Restrict Option Seller to Option Contracts Only (`lib/parms.ini`):** Updated `[option_seller] options_only` from `false` to `true` in `lib/parms.ini` (and synced the sample configuration in `docs/CONFIGURATION_AND_API_REFERENCE.md`) so `OptionSeller` ignores equity/stock holdings and evaluates only OCC option contracts.

---

## `v0.1.27` — 2026-11-26
- **Fixed Startup `TypeError` in `_apply_ini_feature_settings()` (`multiStreamRSI.py`):** Added optional `minimum=None` support to `_ini_float()` (matching `_ini_int()`) and removed the redundant `minimum=0.0` keyword argument on `[option_seller] dynamic_breakeven_activation_pct` so `load_settings()` initializes cleanly at startup without crashing.

---

## `v0.1.26` — 2026-11-26
- **Automated `parms.ini` Backup & Merge in `sh/getLatest.sh`:** Enhanced `sh/getLatest.sh` to check for `parms.ini` on Windows Downloads, stage files in a timestamped `/tmp/jboa_deploy.<YYYYMMDD_HHMMSS>` directory (`0700`, auto-removed on exit), back up each instance's `parms.ini` to `parms.ini.<timestamp>` (`/home/billt/jboa-1/src/lib` and `/home/jboa/lib`), merge master comments/new keys with existing local values/custom keys into `parms.ini.combined`, and overlay each instance's `parms.ini` while preserving permissions and `jboa:jboa` ownership.
- **Configuration Update (`lib/parms.ini`):** Updated `[kinematics] rsi_session` from `extended` to `regular` in `lib/parms.ini` (and synced the sample config in `docs/CONFIGURATION_AND_API_REFERENCE.md` and script catalog in `docs/DEPLOYMENT_AND_OPERATIONS.md`).

---

## `v0.1.25` — 2026-11-25
- **Open-Contract `Find Best` Support (`[dashboard] findbest = true`):** Enabled the **`Find Best`** button on the **Option Buyer (Open Contracts)** drilldown tab (in addition to closed contracts), strictly gated by `[dashboard] findbest = true`. `GET /api/findbest?is_open=1` now evaluates open contracts from `purchased_at_utc` through live `now_utc` using `logs/optionchains/*.json` snapshots + 1-minute tracked contract history, displaying `OPEN POSITION (LIVE)`, `Current Mark` comparisons, and live `parms.ini` replay metrics.
- **Peak-Ratcheting Trailing Stops & Volatility/Spread-Adaptive Dynamic Exit Rules (`[option_seller] dynamic_rules_mode`):** Added 5 new `[option_seller]` configuration parameters (`dynamic_rules_mode = off | view_only | view_and_act`, `dynamic_ratchet_tiers = 5:4.0,10:3.0,20:2.0`, `dynamic_breakeven_activation_pct = 8.0`, `dynamic_vol_adapt_enabled = true`, `dynamic_min_samples = 3`) powered by `_evaluate_dynamic_contract_rules()`. In **`view_only`** (shadow) mode, live sell execution remains 100% unchanged while `OptionSeller` computes dynamic thresholds every minute, logs `OPTION_SELL_DYNAMIC_SHADOW`, and exposes full telemetry via `/cli/status` and `/api/findbest`. In **`view_and_act`** mode, per-contract dynamic thresholds actively override Tier 3 trailing stop, dynamic `+0.5%` breakeven lock, and Tier 4b soft stop-loss (while still honoring `min_hold_minutes`, `stop_loss_hard_pct`, velocity/kinematics gates, cooldown, and `dry_run`).
- **Full Dashboard UI Visibility for Dynamic Exit Rules:** Added a live **Dynamic Exit Rules** telemetry block (`OFF` / `VIEW-ONLY` / `VIEW & ACT` badge + clickable per-contract status cards) to the Option Seller dashboard panel, an inline **Dynamic Exit Rules** strip on open contracts in `optionDrilldownModal`, and a dedicated **Dynamic Exit Rules — Peak-Ratcheting & Vol/Spread-Adaptive** panel with visual Peak-Ratchet Tier Ladder and shadow/active verdict inside `findBestModal`. Also added `docs/OPTION_BUYER_GUIDE.md` and `docs/OPTION_SELLER_AND_STOP_LOSS_GUIDE.md` to the in-app **Docs** modal catalog order.

---

## `v0.1.24` — 2026-11-25
- **Fixed Engine Freeze (`Next Minute: 0s` / Data Collection Halted) Caused by `NasdaqMarketTracker` Lock Deadlock (`multiStreamRSI.py`):** Resolved a deterministic self-deadlock in `NasdaqMarketTracker.refresh_sync()` where a same-minute cache hit called `self.get_status()` while already holding non-reentrant `self._lock`, permanently wedging `self._lock` and freezing `MinuteCoordinator.finalize_due()` (as well as `/cli/status` and `/api/kinematics`) on the very next minute. Replaced the nested call with `self._build_status_locked()`, upgraded `NasdaqMarketTracker._lock` and `DailyRsi30Tracker._lock` to `threading.RLock()`, and bounded `refresh_for_minute()` with a 25s `asyncio.wait_for` timeout plus 15s `yfinance` timeouts.
- **Event-Loop & Coordinator Resilience:** Offloaded `OptionSeller.evaluate_and_sell()`'s `option_history.record_positions_snapshot()` call (which performs disk cache writes and closed-order reconciliation HTTP calls) onto `asyncio.to_thread()` with a 20s timeout, wrapped `MinuteCoordinator.run()`'s `finalize_due()` loop in defensive `try/except` logging, and fixed `NasdaqMarketTracker._fetch_minute_price_sync()` to read `TastytradeClient.get_quote()` `SimpleNamespace` fields (`last_price`/`mark_price`/`mid_price`/`ask_price`/`bid_price`).

---

## `v0.1.23` — 2026-11-25
- **Fixed Constantly Spinning Dashboard Refresh Icon (`multiStreamRSI.py`):** Restricted `#refreshIcon` `animate-spin` solely to manual clicks on **Refresh Now** (`fetchAllData(true)`) instead of spinning on every 2.5s background poll, and added `_fetchWithTimeout()` (`AbortController` timeouts of 12s–20s) to `fetchAllData()` and `fetchMinuteSummaries()` so a stalled request can never leave `_fetchAllInFlight` or the spinner wedged indefinitely.

---

## `v0.1.22` — 2026-11-25
- **Fixed `browserTzName is not defined` in `Buy / Sell Log Details` Modal (`multiStreamRSI.py`):** Added `const { tzName: browserTzName, tzAbbr: browserTzAbbr } = _getBrowserTzInfo();` inside `renderOptionLogDetailsPanel()` so the trade-scoped BUY/SELL `±3` log details table renders cleanly in the browser's local timezone.

---

## `v0.1.21` — 2026-11-25
- **Dashboard Auth Session Stability & Multi-Tab `401` Fix (`multiStreamRSI.py`):** Removed `resp.delete_cookie(...)` from `401` `/api/*` and `/cli/*` responses (and `302` `/login` redirects) in `_enforce_dashboard_auth()` so stale background tabs or concurrent in-flight polls after a restart can no longer wipe a freshly issued valid session cookie. Explicit `/auth/logout` continues to revoke sessions server-side and clear the cookie.
- **Frontend Telemetry Polling Guards & Clean Redirects:** Added `_stopPollingTimers()`, explicit `credentials: 'same-origin'` default in the `window.fetch` wrapper, in-flight serialization guards (`_fetchAllInFlight`, `_fetchSummariesInFlight`), and strict `r.ok` / `status === 'ok'` checks in `fetchAllData()` and `fetchMinuteSummaries()` so unauthenticated `401` responses stop polling immediately instead of flooding the server or leaving the UI stuck on `"Connecting to engine telemetry stream..."`.
- **Benign Client-Disconnect TLS/Socket Log Suppression:** Added `_is_benign_disconnect_error()` filtering in `AdaptiveSSLThreadedWSGIServer` and `BoundedRequestHandler` (plus raised per-connection timeout from `2.0s` to `5.0s`) to cleanly ignore harmless browser navigation disconnects (`BrokenPipeError`, `ConnectionResetError`, `ssl.SSLError: UNEXPECTED_EOF_WHILE_READING`).
- **`NasdaqMarketTracker` (`QQQ`) & `DailyRsi30Tracker` Resilience:** Added `None`-safe `session_bounds(day)` guards across `NasdaqMarketTracker` (preventing holiday/weekend `TypeError`), added Alpaca `SIP -> IEX` feed fallback, `StockSnapshotRequest` fallback, wider 15-minute bar lookback, `api_key`/`secret_key`/env credential fallbacks, `samples_today`/`status`/`log_file` status fields (fixing `0m` counter display), root `jboa.log` startup/open logging, and concurrent startup execution of `daily_rsi30_tracker.seed_all()` alongside RSI warmup.

---

## `v0.1.20` — 2026-11-25
- **30-Day RSI Always Collected, Logged & Displayed (`multiStreamRSI.py`):** Matched the `NASDAQ` monitor pattern so `DailyRsi30Tracker` always seeds daily 30-period Wilder RSI baselines at startup, calculates live `rsi_30d` on every 1-minute bar, persists `rsi_30d` across provider/minute/buyer/seller logs, and always displays `30D RSI` in all dashboard panels and modals. `[general] rsi_30d_enabled = true` now strictly controls whether the `OptionBuyer` buy gate (`rsi_30d > rsi_30d_min`) is enforced (badge switches between `30D RSI MONITOR` and `30D RSI GATE > <min>`).
- **Live `QQQ` Price & Intraday `%` Chips on All Panel Headers:** Added live `.nasdaq-panel-chip` badges showing the latest collected `nasdaq_symbol` (default `QQQ`) price and up/down `%` vs. the 09:30 ET open (`QQQ $XXX.XX ▲ +X.XX%` / `▼ -X.XX%`) to the headers of all dashboard panels and modals alongside the top navbar readout.
- **Option Buyer / Option Seller `Buy/Sell Log Details` Button & Trade-Scoped Modal (`GET /api/option-log-details`, `#optionLogDetailsModal`):** Renamed `#btnOptDrilldownBuySellLog` to **`Buy/Sell Log Details`** (always visible, not `parms.ini`-controlled). Clicking it opens `#optionLogDetailsModal` in the same table format as `optionLogsModal`, showing the triggering **BUY** log entry plus **3 entries prior and 3 entries after** (`-3..+3`) and the triggering **SELL** log entry plus **3 entries prior and 3 entries after** (or an open-position note if not yet closed). Included a **`Buy/Sell Log`** button in the modal header (`openOptionLogsFromDetailsModal()`) that preserves the previous jump-to-buy-time full log behavior.
- **Config & Docs Updated:** Updated `lib/parms.ini`, `docs/CONFIGURATION_AND_API_REFERENCE.md`, and `docs/FRONTEND_DASHBOARD.md`.

---

## `v0.1.19` — 2026-11-25
- **Strict `false` Defaults for `[dashboard] strike_analyzer_enabled` and `findbest` (`multiStreamRSI.py`):** Hardened `_ini_bool()` and `FINDBEST_ENABLED` resolution so blank or omitted INI entries and blank/unset environment variables strictly default to `false` without raising parse errors.
- **NASDAQ Must Be Positive Tracker & Buy Gate (`[general] nasdaq_must_be_positive`, `nasdaq_symbol`):** Added `NasdaqMarketTracker` (`logs/nasdaq/<SYMBOL>-<RUN_ID>.log`, default `nasdaq_symbol = QQQ`, `nasdaq_must_be_positive = false`, env fallbacks `NASDAQ_MUST_BE_POSITIVE` / `NASDAQ_SYMBOL`). Always collects the 09:30 ET RTH open and every 1-minute price during RTH regardless of the gate toggle, persists snapshots into `minute-summary-*.jsonl`, and displays live `QQQ` open/current/diff status in the dashboard header & telemetry card. When `nasdaq_must_be_positive = true`, `OptionBuyer.evaluate_and_buy()` requires `(current_price - open_price) > 0` before buying and explicitly logs rejections (`OPTION_BUY_ANALYSIS decision=REJECTED reason=nasdaq_down(...)` and `OPTION_BUY_SKIPPED reason=nasdaq_down`).
- **30-Day Daily RSI Calculation, Persistence, UI Display & Buy Gate (`[general] rsi_30d_enabled`, `rsi_30d_min`):** Added `DailyRsi30Tracker` (`rsi_30d_enabled = false`, `rsi_30d_min = 30.0`, env fallbacks `RSI_30D_ENABLED` / `RSI_30D_MIN`). When enabled, seeds 30-day daily Wilder RSI baselines (Alpaca daily bars with `yfinance` fallback), updates live provisional `rsi_30d` alongside every 1-minute RSI calculation, persists `rsi_30d` in provider symbol logs, `minute-summary-*.jsonl`, and `option_buyer`/`option_seller` analysis logs, renders `30D RSI` across all dashboard panels (header, watchlist table, quadrant matrix, symbol deep-dive, crossover chart HUD, provider comparison, minute summaries, Buy/Sell Logs modal, Find Best modal, Strike Analyzer modal), and gates buys on strictly `rsi_30d > rsi_30d_min` with explicit rejection logging (`rsi_30d_below_min` / `rsi_30d_unavailable`).
- **Config & Docs Updated:** Added all 4 `[general]` parameters to `lib/parms.ini` and updated `docs/CONFIGURATION_AND_API_REFERENCE.md` and `docs/FRONTEND_DASHBOARD.md`.

---

## `v0.1.18` — 2026-11-24
- **Per-Minute Open-Position Option Capture Mode (`[option_seller] open_position_capture_mode`, `multiStreamRSI.py`):** Added a single opt-in configuration setting (`open_position_capture_mode = none | contract_quote | full_chain`, env fallback `OPTION_SELL_OPEN_POSITION_CAPTURE_MODE`, default `none` preserving existing behavior).
  - **`contract_quote` (Approach 1):** Runs one batched live quote + Greeks snapshot per finalized minute for all open OCC contracts (Alpaca `v1beta1/options/snapshots?symbols=...` or Tastytrade `get_quotes_batch` + optional `get_option_greeks_batch`), enriching each contract's 1-minute `OptionContractHistoryTracker` bar with `bid`, `ask`, `mid`, `spread_pct`, `delta`, `gamma`, `theta`, `vega`, `iv`, `volume`, and `open_interest` (preserved across subsequent `live_poll` upserts).
  - **`full_chain` (Approach 2):** Captures and persists a full pruned `logs/optionchains/optionchain-OPTION_SELLER-<PROV>-<UND>-HOLD_1M-*.json` snapshot every finalized minute for each unique `(underlying, right, expiry)` held in an open option position (serialized with `OPTION_CHAIN_PACING_SECONDS` pacing so order evaluation is never blocked and Strike Analyzer BUY counts remain unaffected).
- **Find Best Executable-Bid & Hold-Chain Integration:** Updated `_parse_optionchain_file_meta()` to recognize `HOLD_1M` snapshots (`signal="HOLD"`, `trigger="hold_1m"`) and updated `analyze_findbest_contract()` so 1m history bars enriched by `contract_quote` automatically price exits using executable `bid` (falling back to `mid` then `price`) while surfacing spread and Greeks metrics.
- **Reference `lib/parms.ini` & Docs Updated:** Added `open_position_capture_mode = none` to `lib/parms.ini` and `docs/CONFIGURATION_AND_API_REFERENCE.md`.

---

## `v0.1.17` — 2026-11-24
- **New `findbest` Option Seller Analyzer (`[dashboard] findbest = true`, `multiStreamRSI.py`):** Added `[dashboard] findbest` (alias `findbest_enabled`, env `FINDBEST_ENABLED`, default `false`) to reveal a **`Find Best`** button (`#btnOptDrilldownFindBest`) on the **Option Seller (Closed Contracts)** panel header and enable `GET /api/findbest`.
- **Buy→Sell Option Chain & `parms.ini` Tuning Analytics (`analyze_findbest_contract()`, `#findBestModal`):** Scans all captured `logs/optionchains/*.json` snapshots and 1-minute contract valuation points between the selected contract's buy and sell timestamps (`Combined`, `Option Chains Only`, and `1m Contract History` views; browser-local timestamps). Summarizes total data points, the exact most-profitable (peak/MFE) timestamp and P/L, profitable point count & overall average (`%`/`$`), losing point count & overall average (`%`/`$`) + worst MAE, exit capture efficiency & profit left on table, a Fixed Profit Target ladder (`profit_target_pct`), a Trailing Stop replay grid (`trailing_activation_pct` / `trailing_drawdown_pct` with & without `v < 0`), Tier 2 RSI / Tier 4 stop-loss & `min_hold_minutes` grace diagnostics, option-chain spread/Greeks liquidity stats, and deterministic `lib/parms.ini` tuning takeaways.
- **Docs Updated:** Documented `[dashboard] findbest`, `GET /api/findbest`, and `findBestModal` in `docs/CONFIGURATION_AND_API_REFERENCE.md` and `docs/FRONTEND_DASHBOARD.md`.

---

## `v0.1.16` — 2026-11-24
- **Option Buyer & Option Seller `Buy/Sell Log` Jump to Buy Time (`multiStreamRSI.py`):** Clicking **`Buy/Sell Log`** (`#btnOptDrilldownBuySellLog` / `openOptionLogsFromDrilldown()`) on either the Option Buyer or Option Seller panel now jumps straight to the selected contract's buy time (`purchased_at_utc`), auto-populating the Start/Stop filters to a `±5 min` window in the browser's local timezone, resetting stream/decision pills to `All`, highlighting the closest buy-time log row (`BUY TIME`), and scrolling it into the center of the log table. Opening `Buy/Sell Log` from the main dashboard clears drilldown-set time bounds automatically so live logs display cleanly.
- **Docs Updated:** Updated `docs/FRONTEND_DASHBOARD.md` (Section 4.2).

---

## `v0.1.15` — 2026-11-24
- **Buy/Sell Decision Logs Start & Stop Date/Time Filters (`multiStreamRSI.py`):** Added browser-timezone-aware **Start** (`#optLogStartInput`) and **Stop** (`#optLogStopInput`) `datetime-local` inputs, browser timezone badge (`#optLogsTzBadge`), and `Clear` button to the Buy / Sell Decision & Execution Logs toolbar; supports entering Start only, Stop only, or both, keeps pickers mounted across 2.5s polls, and formats the `Time` column in the browser's local timezone.
- **Server-Side Time-Window Filtering (`/api/option-logs`):** Exposed `timestamp_epoch_ms` on parsed log records and added `start_ms` / `stop_ms` (plus `start` / `stop` ISO fallback) filtering inside `read_option_analysis_logs()` before applying per-stream caps so full historical windows and pill counts remain accurate across any browser vs. server timezone difference.
- **Docs Updated:** Updated `docs/FRONTEND_DASHBOARD.md` (Section 4.3) and `docs/CONFIGURATION_AND_API_REFERENCE.md` (`/api/option-logs`).

---

## `v0.1.14` — 2026-11-24
- **Option Buyer & Option Seller `Buy/Sell Log` Button (`multiStreamRSI.py`):** Added `#btnOptDrilldownBuySellLog` (`Buy/Sell Log`) directly beside `Sync Broker History` in the shared Option Buyer / Option Seller drilldown modal header (`openOptionLogsFromDrilldown()`), opening `#optionLogsModal` layered on top (`z-[60]`, with clean single-modal `Escape` close) and pre-filtered to the currently selected contract's underlying symbol.
- **Docs Updated:** Documented the new Option Buyer / Option Seller `Buy/Sell Log` button in `docs/FRONTEND_DASHBOARD.md` (Sections 4.2 and 4.3).

---

## `v0.1.13` — 2026-11-24
- **Option Seller `kind=take_profit` Profit Guard (`multiStreamRSI.py`):** Added `[option_seller] take_profit_require_profit` (default `true`, env `OPTION_SELL_TAKE_PROFIT_REQUIRE_PROFIT`) and `take_profit_min_pnl_pct` (default `0.0`, env `OPTION_SELL_TAKE_PROFIT_MIN_PNL_PCT`) to Tier 2 (`kind=take_profit`, Overbought RSI + Bearish Kinematics) in `OptionSeller.evaluate_and_sell()`. Tier 2 now refuses to sell at a loss or breakeven (failing closed if `pnl_pct` is unavailable, logging `take_profit_no_profit` / `take_profit_pnl_unavailable`) and falls through cleanly to Tier 3 (Trailing Stop) and Tier 4 (Stop Loss).
- **Preserve Horizontal & Vertical Scroll Across Live Dashboard Polling:** Refactored `renderOptionLogsPanel()` so `#optLogsScrollContainer` and `#optLogSearchInput` remain mounted in-place across 2.5s polls (updating `#optLogsTableBody` only on diff and preserving both `scrollLeft` and `scrollTop`), and added horizontal + vertical scroll preservation with DOM diff-skipping to the Instruments Table, Provider Comparison Table, Minute Summaries feed, and `optionDrilldownModal`.
- **Docs Updated:** Updated `docs/CONFIGURATION_AND_API_REFERENCE.md`, `docs/TRADING_ALGORITHMS_AND_RISK.md`, and `docs/FRONTEND_DASHBOARD.md`.

---

## `v0.1.12` — 2026-11-22
- **Defensive INI Parsing (`_parse_ini_text_tolerant()`):** Hardened `multiStreamRSI.py` (`load_settings()` and INI helper readers) and `findthewinners.py` so duplicate keys/sections in `lib/parms.ini` (such as a repeated `[kinematics] summary_delay_seconds`) log a `CONFIG_INI_DUPLICATE_IGNORED` warning and use the last definition (`strict=False`) instead of crashing startup with `configparser.DuplicateOptionError`.
- **Docs Updated:** Added `CONFIG_INI_DUPLICATE_IGNORED` troubleshooting guidance to `docs/CONFIGURATION_AND_API_REFERENCE.md`.

---

## `v0.1.11` — 2026-11-22
- **Configurable Finalization Delay (`[kinematics] summary_delay_seconds`):** Exposed `SUMMARY_DELAY_SECONDS` (default `45.0`s, range `0`–`3600`s) in `lib/parms.ini` under `[kinematics]` (with `[general]` and env fallbacks) and fixed `MinuteCoordinator.__init__()` to read the runtime INI-loaded value rather than the import-time default.
- **Optional Early Finalization (`early_finalize_on_complete`):** Added `[kinematics] early_finalize_on_complete` (default `false`) and `early_finalize_min_delay_seconds` (default `2.0`s) in `MinuteCoordinator._can_finalize_early()`, allowing streaming feeds (Alpaca / Tastytrade DXLink) to finalize in ~2–3 seconds once every connected provider reports every configured symbol while preserving `summary_delay_seconds` as a safety deadline.
- **Telemetry & Docs Updated:** Added `finalized_early` to `MINUTE_SUMMARY` JSONL records, exposed early-finalization metrics (`summary_delay_seconds`, `early_finalize_on_complete`, `early_finalized_count`, `last_finalize_latency_seconds`) in `/cli/status`, and updated `README.md`, `docs/CONFIGURATION_AND_API_REFERENCE.md`, and `docs/TRADING_ALGORITHMS_AND_RISK.md`.

---

## `v0.1.10` — 2026-11-22
- **Broker-Agnostic Task & Class Rename (`multiStreamRSI.py`):** Renamed `AlpacaPositionMonitor` → `PositionMonitor`, `AlpacaOptionBuyer` → `OptionBuyer`, and `AlpacaOptionSeller` → `OptionSeller` (plus asyncio task names `POSITION_MONITOR`, `OPTION_BUYER`, `OPTION_SELLER`) to accurately reflect shared Alpaca and Tastytrade execution via `build_trading_client()`.
- **Backwards-Compatible Aliases:** Retained module-level aliases (`AlpacaPositionMonitor`, `AlpacaOptionBuyer`, `AlpacaOptionSeller`) so any external script importing the legacy names continues to work without changes.
- **Docs & Architecture Diagram Updated:** Updated `README.md` (including the Mermaid architecture diagram) and `docs/TRADING_ALGORITHMS_AND_RISK.md`, `docs/DEPLOYMENT_AND_OPERATIONS.md`, and `docs/CONFIGURATION_AND_API_REFERENCE.md`.

---

## `v0.1.9` — 2026-11-22
- **Interactive Mermaid Diagram Pan & Zoom (`docsModal`):** Added `_attachMermaidPanZoom()` in `multiStreamRSI.py` with click-and-drag panning, cursor-centered mouse-wheel zoom (`12%`–`800%`), double-click zoom-in/reset, and two-finger touch pinch-to-zoom over crisp natural-scale Mermaid SVGs (`useMaxWidth: false`).
- **Per-Diagram Zoom Toolbar & Fullscreen Expand:** Added **`-`**, live **`%`** zoom indicator, **`+`**, **`1:1`** (100% readable font size), **`Fit`**, and **`Expand` / `Collapse`** fullscreen overlay controls (with `+`/`-`/`0`/`1`/`Esc` shortcuts while expanded) alongside the existing **`Source` / `Diagram`** toggle.
- **Docs Updated:** Documented Mermaid pan, zoom, and fullscreen controls in `docs/FRONTEND_DASHBOARD.md` (Section 4.5).

---

## `v0.1.8` — 2026-11-22
- **Standardized Changelog Location (`docs/CHANGELOG.md`):** Moved `permanentchangelog.md` into `docs/CHANGELOG.md` to match the uppercase `docs/*.md` documentation suite convention.
- **UI & Catalog Alignment (`multiStreamRSI.py`):** Pointed `#appVersionBadge` (`openDocsModal('docs/CHANGELOG.md')`) and `_DOCS_PREFERRED_ORDER` to `docs/CHANGELOG.md`, removed the root-level special case from `_resolve_docs_catalog()`, and simplified `sh/run_dual_instances.sh` so `docs/*.md` sync covers it automatically.
- **Documentation & Guardrails Updated:** Updated `AGENTS.md` Rule 8, `README.md`, and all `docs/*.md` navigation links to reference `docs/CHANGELOG.md`.

---

## `v0.1.7` — 2026-11-22
- **Permanent Changelog & UI Docs Integration:** Created `permanentchangelog.md` at the repository root and allowlisted it in `multiStreamRSI.py :: _resolve_docs_catalog()` and `_DOCS_PREFERRED_ORDER` so it appears directly in the Port `5001`/`6001` dashboard **Docs** modal (`GET /api/docs`).
- **Clickable Header Version Badge:** Wired the dashboard header version badge (`#appVersionBadge`, `v__APP_VERSION__`) to open `permanentchangelog.md` inside `docsModal` on click (`openDocsModal('permanentchangelog.md')`).
- **Cross-Doc & Dual-Instance Sync:** Linked `permanentchangelog.md` across `README.md` and all `docs/*.md` navigation headers, and updated `sh/run_dual_instances.sh` (`setup_instance2`) to sync `README.md`, `permanentchangelog.md`, and `docs/*.md` into Instance 2 (`/home/jboa`).

---

## `v0.1.6` — 2026-11-22
- **Built-In Dark-Theme Mermaid SVG Rendering (`docsModal`):** Updated `renderSafeMarkdown()` and added lazy-loaded `mermaid.js` v11 (`_ensureMermaidLoaded()`, `renderDocsMermaidDiagrams()`) with `securityLevel: 'strict'` and dark-slate/cyan theme variables so fenced ```` ```mermaid ```` blocks render automatically as responsive inline SVGs inside the dashboard Docs viewer.
- **Offline & Error Fallback + Source Toggle:** Added a 6-second CDN timeout across jsDelivr and unpkg so offline browsers or malformed diagrams automatically keep the escaped raw Mermaid code block visible (`offline — raw source` / `render fallback — raw source`), and added a per-diagram **`Source` / `Diagram`** toggle button.
- **Docs Updated:** Documented Mermaid rendering and offline fallback behavior in `docs/FRONTEND_DASHBOARD.md` (Section 4.5).

---

## `v0.1.5` — 2026-11-22
- **Configurable Zero-Hindsight Ex-Ante Contract Selection:** Added `_assign_chain_otm_ranks()`, `_compute_max_single_contract_budget()`, and `_select_ex_ante_option_contract()` over the already-cached pruned `chain_snapshot` (zero extra API calls), controlled by `[option_buyer] contract_selection_mode` (`plus2_otm` default, `atm`, `itm1`, `itm2`, `otm1`, `atm_itm_low_spread`, `delta_mid`, `delta_itm_low_spread`, `composite_score`), delta bounds/target (`contract_target_delta`, `contract_delta_min`, `contract_delta_max`), liquidity floors (`contract_min_open_interest`, `contract_min_volume`), and automatic outward `contract_budget_stepdown`.
- **Forecast-Independent Entry Mode & ET Clock Window:** Added `[option_buyer] entry_mode` (`kinematics_forecast` default, or `rsi_ex_ante` which qualifies on oversold RSI / bounce lookback without requiring 1-minute Taylor `forecast_direction` or `projected_delta > 0`, while still blocking falling knives and accelerating drops) plus optional US/Eastern time bounds (`entry_window_start_et`, `entry_window_end_et`).
- **Telemetry & Zero-Regression Defaults:** Exposed all new `[option_buyer]` settings in `FEATURE_CONFIG`, `OPTION_BUYER_START`, and `/cli/status` (`option_buyer_report`); defaults remain 100% identical to prior `+2 OTM` and `kinematics_forecast` behavior. Updated `docs/CONFIGURATION_AND_API_REFERENCE.md`, `docs/TRADING_ALGORITHMS_AND_RISK.md`, and `README.md`.

---

## `v0.1.4` — 2026-11-21
- **Tastytrade OAuth Hardening & Safe Diagnostics:** Added `TastytradeClient._clean_credential()` to strip stray quotes/whitespace/comments and `_diagnose_token_exchange_error()` to emit safe, non-secret JWT structure hints on `HTTP 400 invalid_grant` token exchange failures.
- **Provider + Environment-Scoped Option History:** Scoped `OptionContractHistoryTracker` disk state to `option_history_state_<provider>_<env>.json` with provider-filtered log replay, closed-contract pruning on startup backfill, and surfaced broker error diagnostics directly in the dashboard telemetry card.

---

## `v0.1.3` — 2026-11-20
- **Tastytrade Client Startup Fix:** Resolved `AttributeError: 'TastytradeClient' object has no attribute 'env'` during `AlpacaOptionBuyer.__init__()` and `AlpacaOptionSeller.__init__()` when `[option_trading] transactionprovider = tastytrade`.
- **Strict Broker Routing Isolation:** Eliminated cross-broker history mixing between Alpaca and Tastytrade execution environments.

---

## `v0.1.2` — 2026-11-20
- **Buy Capital Scaling Guardrails:** Added `_calculate_budget_scaled_qty()` enforcing `[option_buyer] max_single_trade_dollars` and `invest_buffer_withhold_pct` with automatic whole-contract `qty` scaling down before rejection.
- **Dashboard Docs Viewer & Version Badge:** Added authenticated, path-confined `GET /api/docs` endpoint, two-pane `docsModal` Markdown viewer (`renderSafeMarkdown`), and `#appVersionBadge` header readout synced via `/cli/status`.

---

## `v0.1.1` — 2026-11-19
- **Built-In Strike Analyzer & Multi-Instance Identity:** Added `GET /api/strike-analysis` (gated by `[dashboard] strike_analyzer_enabled`), `[general] owner` color stripe/tab branding for dual-instance operation, adaptive HTTP/HTTPS TLS (`ssl_mode = auto`), and cross-tab inactivity session locking.

---

## `v0.1.0` — 2026-11-18
- **Initial Versioned Baseline (`APP_VERSION`):** Established semantic versioning (`major.minor.build`) in `multiStreamRSI.py` with multi-provider 1-minute RSI & HMA/EMA kinematics aggregation (`ALPACA`, `TASTYTRADE`, `TRADINGVIEW`, `TRADIER`, `TWELVEDATA`, `YAHOOFINANCE`), `AlpacaOptionBuyer` ex-ante guardrails, `AlpacaOptionSeller` 4-tier exit waterfall, `ChartCaptureManager` 1-minute PNG renderer, and Port `5000`/`5001` Flask interfaces.
