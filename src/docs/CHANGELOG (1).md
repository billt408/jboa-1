<div align="center">

# JBOA-RSI PERMANENT CHANGELOG

### Versioned Release History Tied to `APP_VERSION` in `multiStreamRSI.py`

**Document ID:** `JBOA-RSI-CHANGELOG`  
**Versioning Policy:** `major.minor.build` (increment `build` on every `multiStreamRSI.py` modification; change `major` or `minor` only on explicit user instruction)

</div>

**Navigation:** [Overview (`README.md`)](../README.md) · **Changelog (`docs/CHANGELOG.md`)** · [Trading Algorithms & Risk](./TRADING_ALGORITHMS_AND_RISK.md) · [Frontend Dashboard](./FRONTEND_DASHBOARD.md) · [Deployment & Operations](./DEPLOYMENT_AND_OPERATIONS.md) · [Configuration & API Reference](./CONFIGURATION_AND_API_REFERENCE.md)

---

> **Append Policy (`AGENTS.md` Rule 8):** Every time `APP_VERSION` in `multiStreamRSI.py` is incremented, append a dated entry tied to the new version number below so operators can inspect the full release history directly inside the live dashboard **Docs** viewer (or by clicking the **`vX.Y.Z`** version badge in the header).

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
