# Backtest & validation results (Milestone 6)

_Run 2026-11-24 ~10:35–10:50 (Europe/Prague), ATLAS 0.6.0-m6, real free historical data, live cost model
(crypto 10 bps fee + 10 bps slippage + 2 bps assumed half-spread per side; stocks 0 fee + 5 bps slippage + 2 bps)._
Regenerate: `python -m app.main backtest --json var/...` then `python scripts/backtest_report.py`.

**Bottom line: no strategy shows an edge after costs. None is validated, so none can trade.**
- Stocks (2 years, Alpaca IEX 5m): all 6 backtestable strategies **failed** out-of-sample. OOS profit factor was 0.57–1.01
  against a 1.3 target, expectancy ≤ +0.007 R, and only 0–2 of 5 walk-forward folds were positive. Their signals are now `rejected`.
- Crypto (2 years, Coinbase 15m): the live gate (net R:R ≥ 2 after costs) lets almost nothing through: 0–3 OOS
  trades per strategy, so the verdict is **insufficient_data** (signals stay `incomplete`). The diagnostic run with
  the gate lowered to 1.0 shows negative expectancy everywhere (PF 0.55–0.87), so this is not hidden edge.
- Yahoo 60-day cross-check (consolidated volume) agrees: negative or near-zero expectancy for all strategies.
- news_momentum, orderbook_imbalance and liquidity_change are not backtestable: there is no free historical news archive and no historical L2 order-book data.
- Nothing was tuned after seeing the results. The walk-forward-selected variants are listed in the JSON and are not deployed.

How to read the table:
- IS = first 60 % of the period, OOS = last 40 %.
- WF = 5-fold expanding walk-forward.
- Stress = OOS with costs ×1.5. Its candidate set differs, because higher costs filter more setups, so a small n makes it noisy.
- Diag = OOS with the net R:R gate lowered to 1.0 (diagnostic only).

### crypto · coinbase 15m · BTC-USD, ETH-USD, SOL-USD · 2024-11-24 → 2026-11-24

| strategy | IS n | IS PF | OOS n | OOS win % | OOS PF | OOS exp R | OOS net % | OOS max DD % | WF n | WF PF | WF + folds | stress PF | loose / strict OOS PF | diag. R:R≥1 OOS n / PF / exp R | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| momentum_breakout | 2 | 1.99 | 0 | – | – | – | – | – | 0 | – | 0/0 | – | – / – | 163 / 0.78 / -0.137 | **insufficient_data** |
| volume_expansion | 11 | 0.30 | 1 | 0 | 0.00 | -1.000 | -0.25 | 0.25 | 9 | 0.28 | 1/4 | – | 1.01 / – | 62 / 0.87 / -0.077 | **insufficient_data** |
| orderbook_imbalance | | | | | | | | | | | | | | | not backtestable: needs historical L2 order books - not available from free sources |
| volatility_expansion | 1 | 0.00 | 1 | 100 | – | 2.014 | 0.50 | 0.00 | 1 | – | 1/1 | – | – / – | 47 / 0.82 / -0.111 | **insufficient_data** |
| trend_continuation | 17 | 1.07 | 3 | 33 | 0.34 | -0.259 | -0.19 | 0.29 | 10 | 1.42 | 1/3 | – | 0.00 / – | 164 / 0.55 / -0.311 | **insufficient_data** |
| sr_breakout | 5 | 1.41 | 1 | 0 | 0.00 | -1.000 | -0.25 | 0.25 | 2 | 0.00 | 0/2 | – | 0.00 / 0.00 | 137 / 0.63 / -0.242 | **insufficient_data** |
| liquidity_change | | | | | | | | | | | | | | | not backtestable: needs historical L2 order books - not available from free sources |

### stock · alpaca 5m · SPY, QQQ, AAPL, MSFT, NVDA, TSLA, AMD, META · 2024-11-25 → 2026-11-23

| strategy | IS n | IS PF | OOS n | OOS win % | OOS PF | OOS exp R | OOS net % | OOS max DD % | WF n | WF PF | WF + folds | stress PF | loose / strict OOS PF | diag. R:R≥1 OOS n / PF / exp R | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| opening_range_breakout | 88 | 0.81 | 68 | 28 | 0.57 | -0.261 | -4.35 | 5.14 | 141 | 0.71 | 1/5 | 0.64 | 0.79 / 0.48 | 122 / 0.69 / -0.177 | **failed** |
| vwap_reclaim | 95 | 0.93 | 40 | 38 | 0.74 | -0.139 | -1.40 | 2.36 | 102 | 0.68 | 2/5 | 1.38 | 0.81 / 0.71 | 144 / 0.72 / -0.174 | **failed** |
| momentum_breakout | 93 | 0.84 | 79 | 39 | 1.01 | 0.007 | 0.11 | 2.46 | 189 | 0.80 | 1/5 | 0.87 | 1.11 / 1.68 | 311 / 0.65 / -0.237 | **failed** |
| relative_volume_anomaly | 80 | 0.67 | 57 | 40 | 0.86 | -0.070 | -1.00 | 1.74 | 82 | 0.67 | 2/5 | 0.43 | 0.91 / 0.77 | 158 / 0.70 / -0.192 | **failed** |
| trend_continuation | 191 | 0.65 | 145 | 36 | 0.79 | -0.118 | -4.20 | 5.03 | 364 | 0.66 | 1/5 | 0.36 | 0.74 / – | 432 / 0.52 / -0.304 | **failed** |
| sr_breakout | 75 | 0.34 | 44 | 39 | 0.97 | -0.013 | -0.17 | 1.53 | 100 | 0.59 | 0/5 | 1.44 | 0.89 / 0.78 | 334 / 0.70 / -0.211 | **failed** |
| news_momentum | | | | | | | | | | | | | | | not backtestable: needs historical news/catalysts - no free historical news archive |

### stock · yahoo 5m · SPY, QQQ, AAPL, MSFT, NVDA, TSLA, AMD, META · 2026-09-01 → 2026-11-23

| strategy | IS n | IS PF | OOS n | OOS win % | OOS PF | OOS exp R | OOS net % | OOS max DD % | WF n | WF PF | WF + folds | stress PF | loose / strict OOS PF | diag. R:R≥1 OOS n / PF / exp R | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| opening_range_breakout | 16 | 1.93 | 11 | 27 | 0.26 | -0.359 | -0.98 | 0.98 | 21 | 0.57 | 0/5 | 0.35 | 0.20 / 0.17 | 19 / 0.31 / -0.376 | **insufficient_data** |
| vwap_reclaim | 7 | 0.43 | 10 | 50 | 1.30 | 0.130 | 0.32 | 1.06 | 20 | 0.68 | 1/4 | 1.48 | 0.87 / 1.22 | 21 / 0.60 / -0.228 | **insufficient_data** |
| momentum_breakout | 21 | 0.71 | 12 | 33 | 0.73 | -0.144 | -0.43 | 0.80 | 44 | 0.69 | 2/5 | 0.95 | 0.85 / 0.00 | 36 / 0.43 / -0.400 | **insufficient_data** |
| relative_volume_anomaly | 10 | 0.42 | 16 | 38 | 0.82 | -0.097 | -0.40 | 1.54 | 33 | 0.70 | 1/5 | 1.12 | 0.86 / 0.92 | 25 / 0.43 / -0.382 | **insufficient_data** |
| trend_continuation | 24 | 0.15 | 16 | 12 | 0.28 | -0.478 | -1.90 | 2.09 | 39 | 0.37 | 2/5 | 0.31 | 0.58 / – | 40 / 0.63 / -0.181 | **insufficient_data** |
| sr_breakout | 13 | 0.41 | 13 | 23 | 0.29 | -0.477 | -1.54 | 1.67 | 23 | 0.39 | 1/5 | 0.23 | 0.53 / 0.47 | 51 / 0.54 / -0.328 | **insufficient_data** |
| news_momentum | | | | | | | | | | | | | | | not backtestable: needs historical news/catalysts - no free historical news archive |

Known biases: survivorship (today's large-cap watchlist), IEX partial volume, assumed spreads (no historical bid/ask),
exits on 5m/15m bars with stop-first ordering, a 2-year window, and a composite score (news/sentiment/liquidity) that cannot be
reproduced historically.
