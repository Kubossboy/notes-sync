"""Stock and crypto scanners: data -> indicators -> strategies -> scored, journaled signals.

mode="live":     only tradable data (live, fresh, valid); signals persisted.
mode="research": also runs on delayed data (e.g. market closed) for inspection;
                 NOTHING is persisted as a signal (dry run, scan_runs row only).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.core.logging_setup import get_logger
from app.data.engine import MarketDataEngine, asset_class_of
from app.data.models import AssetClass, DataStatus, tf_seconds
from app.data.orderbook import BookSnapshot, fetch_book
from app.data.providers.base import ProviderError
from app.database.db import utcnow
from app.scanners.scoring import (PENDING, build_plan, component_scores, cost_bps_per_side,
                                  liquidity_assessment, news_problems, total_score)
from app.quant.engine import QuantEngine
from app.sentiment.aggregate import combine as combine_sentiment
from app.strategies.base import NoSetup, StrategyContext
from app.strategies.crypto import CRYPTO_STRATEGIES, liquidity_alert
from app.strategies.stock import STOCK_STRATEGIES
from app.technical.indicators import compute_all, support_resistance, to_frame

log = get_logger("scanner")


class Scanner:
    def __init__(self, settings: Settings, engine: MarketDataEngine, news=None, quant=None):
        self.settings, self.engine, self.db = settings, engine, engine.db
        self.sc = settings.scanner
        self.news = news            # NewsEngine or None (news/sentiment components then unavailable)
        self.quant = quant if quant is not None else QuantEngine(settings, self.db)   # backtest validation
        self._market_trend: str | None = None
        self._regime: dict | None = None
        self._bench_ret: float | None = None      # benchmark 20-bar return % (relative strength)

    # ------------------------------------------------------------------ public
    def run(self, asset_class: AssetClass, symbols: list[str] | None = None, mode: str = "live",
            now: datetime | None = None) -> dict:
        assert mode in ("live", "research")
        stock = asset_class == AssetClass.STOCK
        symbols = symbols or (self.settings.markets.stocks.watchlist if stock
                              else self.settings.markets.crypto.watchlist)
        tf = self.sc.stock_timeframe if stock else self.sc.crypto_timeframe
        run_id = self.db.insert("scan_runs", {
            "started_ts": utcnow(), "scanner": asset_class.value, "mode": mode, "timeframe": tf,
            "symbols": ",".join(symbols), "config_hash": self.settings.config_hash()})
        news_info = None
        if self.news is not None and self.settings.news.enabled:
            try:
                if now is None and self.news.needs_refresh():
                    st = self.news.ingest()
                    news_info = {"ingested": True, "ok": st["ok"], "failed": st["failed"], "new": st["new"]}
            except Exception as e:      # news failure degrades the score, never crashes the scan
                self.db.log_error("news", e)
                news_info = {"ingested": False, "error": f"{type(e).__name__}: {e}"}
            news_info = (news_info or {}) | {"health": self.news.health(now)}
        self._bench_ret = None
        self._market_trend = self._benchmark_trend(asset_class, tf, now)
        self._regime = self._current_regime(asset_class, now)
        results = []
        for sym in symbols:
            try:
                results.append(self._scan_symbol(sym, asset_class, tf, mode, run_id, now))
            except Exception as e:   # never let one symbol kill the scan silently
                self.db.log_error("scanner", e)
                results.append({"symbol": sym, "skipped": f"error: {type(e).__name__}: {e}"})
        summary = {"run_id": run_id, "scanner": asset_class.value, "mode": mode, "timeframe": tf,
                   "symbols": results,
                   "signals": sum(len(r.get("signals", [])) for r in results),
                   "news": news_info, "market_trend": self._market_trend, "regime": self._regime,
                   "safe_state_halted": not self.engine.safe.trading_allowed()}
        self.db.conn.execute("UPDATE scan_runs SET finished_ts=?, summary_json=? WHERE id=?",
                             (utcnow(), json.dumps(summary, default=str), run_id))
        return summary

    # ------------------------------------------------------------------ internals
    def _current_regime(self, ac: AssetClass, now) -> dict | None:
        """D100: market-wide regime from completed daily bars of the benchmark (cached per UTC day)."""
        rc = self.settings.regime
        if not rc.enabled:
            return None
        key = f"regime_{ac.value}"
        today = (now or datetime.now(timezone.utc)).date().isoformat()
        cached = self.db.get_state(key)
        if cached and cached.get("day") == today:
            return cached
        sym = rc.stock_benchmark if ac == AssetClass.STOCK else rc.crypto_benchmark
        from app.regime import current
        reg, err = None, "no source"
        # straight from the providers: a context read, so a bad daily payload must never trip SAFE STATE
        for name in self.engine.sources_for(ac):
            try:
                cs = self.engine.providers[name].fetch_candles(sym, "1d", 300)
                df = to_frame(cs)
                gaps = df.index.to_series().diff().dropna()
                if len(df) < 70 or gaps.median() < timedelta(hours=20):
                    err = f"{name}: not daily bars"
                    continue
                reg = current(df, now or datetime.now(timezone.utc))
                if reg:
                    break
            except Exception as e:
                err = f"{name}: {type(e).__name__}: {e}"
        if not reg:
            self.db.log_event("regime_unavailable", "scanner", f"{sym}: {err}"[:300], "warning")
            return cached          # keep the last known regime rather than none
        if reg:
            reg = reg | {"day": today, "benchmark": sym}
            self.db.set_state(key, reg)
        return reg or cached

    def _regime_problems(self, setup, ac: AssetClass) -> list[str]:
        rc = self.settings.regime
        out = []
        if ac == AssetClass.STOCK and rc.stock_watch_only:
            out.append("watch-only: stock entries paused (no stock strategy had a tested edge)")
        reg = getattr(self, "_regime", None)
        if rc.enabled and reg:
            from app.regime import allowed
            if not allowed(rc.matrix, ac.value, setup.strategy, reg):
                out.append(f"regime {reg['trend']}/{reg['vol']}: strategy off in this market")
        return out

    def _scan_symbol(self, sym, ac, tf, mode, run_id, now) -> dict:
        stock = ac == AssetClass.STOCK
        limit = min(1000, self.sc.lookback_bars * (3 if stock else 1))
        fr = self.engine.get_candles(sym, tf, limit, now=now)
        s = fr.series
        info = {"symbol": sym, "source": s.source, "data_status": s.status.value, "tradable": s.tradable,
                "age_s": s.age_seconds, "warnings": s.warnings[:5],
                "crosscheck": fr.crosscheck.verdict if fr.crosscheck else None}
        if mode == "live" and not s.tradable:
            info["skipped"] = ("market closed" if s.market_open is False else
                               f"data not tradable (status={s.status.value}, stale={s.stale})")
            return info
        if s.status == DataStatus.UNAVAILABLE or s.issues:
            info["skipped"] = "no valid data"
            return info
        if mode == "live" and not self.engine.safe.trading_allowed():
            info["skipped"] = "SAFE STATE active"
            return info

        df_all = to_frame(s.candles)
        base = df_all[df_all["session"] == "regular"] if stock else df_all
        need_bars = 60
        if len(base) < need_bars:
            info["skipped"] = f"only {len(base)} complete bars (need {need_bars})"
            return info
        df = compute_all(base, ac)
        levels = support_resistance(df)

        book, history, spread = None, [], None
        if not stock:
            book = self._book(sym, s.source)
            if book is not None:
                history = self._book_history(sym, book.source)
                self._store_book(book)
                spread = book.spread_bps
        elif "alpaca" in self.engine.providers and mode == "live":
            try:
                q = self.engine.providers["alpaca"].fetch_quote(sym)
                if q.spread_bps is not None and q.spread_bps <= self.settings.data.max_quote_spread_bps:
                    spread = q.spread_bps
            except ProviderError:
                pass

        news_ctx = None
        if self.news is not None and self.settings.news.enabled:
            news_ctx = self.news.symbol_context(sym, ac.value, now or datetime.now(timezone.utc))
        sentiment = combine_sentiment(news_ctx, df, self._market_trend)
        ctx = StrategyContext(sym, ac, tf, df, df_all if stock else None, levels, book, history,
                              now, cost_bps_per_side(self.settings, ac, spread), news_ctx, sentiment)
        info["news"] = None if news_ctx is None else {
            "events": news_ctx["events"], "aggregate": news_ctx["aggregate"], "healthy": news_ctx["health"]["healthy"],
            "negative_high_verified": len(news_ctx["negative_high_verified"]),
            "positive_catalysts": len(news_ctx["positive_catalysts"])}
        info["sentiment"] = {"composite": sentiment["composite"], "conflicts": sentiment["conflicts"]}
        liq = liquidity_assessment(ctx, self.settings, spread)
        info["liquidity"] = {k: v for k, v in liq.items() if k != "problems"} | {"problems": liq["problems"]}
        info["last_bar"] = df.index[-1].isoformat()
        info["indicators"] = {k: (round(float(df[k].iloc[-1]), 4) if df[k].iloc[-1] == df[k].iloc[-1] else None)
                              for k in ("close", "vwap", "ema9", "ema20", "ema50", "sma200", "rsi14",
                                        "macd_hist", "atr14", "rvol20", "bb_width")}
        info["trend"] = df["trend"].iloc[-1]
        if not stock:
            alert = liquidity_alert(ctx)
            if alert:
                info["liquidity_alert"] = alert
                self.db.log_event("liquidity_alert", "scanner", f"{sym}: {alert}", "warning")

        table = STOCK_STRATEGIES if stock else CRYPTO_STRATEGIES
        toggles = self.sc.stock_strategies if stock else self.sc.crypto_strategies
        no_setup, signals = {}, []
        for name, fn in table.items():
            t = toggles.get(name)
            if t is not None and not t.enabled:
                no_setup[name] = "disabled in config"
                continue
            try:
                setup = fn(ctx, dict(t.params) if t else {})
            except NoSetup as e:
                no_setup[name] = str(e)
                continue
            except Exception as e:
                self.db.log_error(f"strategy.{name}", e)
                no_setup[name] = f"error: {type(e).__name__}: {e}"
                continue
            signals.append(self._finalize(setup, ctx, liq, fr, s, tf, mode, run_id))
        info["signals"] = signals
        info["no_setup"] = no_setup
        return info

    def _finalize(self, setup, ctx, liq, fr, s, tf, mode, run_id) -> dict:
        plan = build_plan(setup, ctx, self.settings)
        comps = component_scores(ctx, liq, self.settings.news)
        quant = self.quant.assess(ctx, setup, plan, self._bench_ret)
        comps["quant"] = quant["score"]
        total, avail, on_avail = total_score(comps, self.settings)
        problems = (plan.problems + liq["problems"] + news_problems(ctx, setup) + quant["problems"]
                    + self._regime_problems(setup, ctx.asset_class))
        missing = [k for k, v in comps.items() if v is None]
        pending = dict(PENDING, quant=quant["reason"])
        if problems:
            status = "rejected"
        elif missing:
            status = "incomplete"
        else:
            status = "new"
        dq = 1.0 if (fr.crosscheck and fr.crosscheck.reliable) else 0.8
        confidence = round(on_avail / 100 * avail * dq, 3)
        data_ts = ctx.df.index[-1].isoformat()
        expires = ctx.df.index[-1] + timedelta(seconds=tf_seconds(tf) * (self.sc.signal_ttl_bars + 1))
        evidence = {**setup.evidence, "plan": asdict(plan), "components": comps,
                    "missing_components": {k: pending.get(k, "unavailable") for k in missing},
                    "quant": quant,
                    "liquidity": {k: v for k, v in liq.items() if k != "problems"},
                    "data_source": s.source, "crosscheck": fr.crosscheck.verdict if fr.crosscheck else None,
                    "confidence": confidence, "trend": ctx.last.get("trend"),
                    "cost_bps_per_side": ctx.cost_bps_per_side,
                    "news": None if ctx.news is None else {k: ctx.news[k] for k in (
                        "aggregate", "events", "negative_high_verified", "positive_catalysts", "unexpected", "top")},
                    "sentiment": ctx.sentiment, "market_trend": self._market_trend,
                    "regime": getattr(self, "_regime", None), "features": _features(ctx, plan.entry),  # D104 review
                    "note": ("strategy validated out-of-sample (still not a guarantee)" if quant["state"] == "validated"
                             else "FORWARD PAPER TEST - unvalidated, internal ledger only" if quant["state"] == "forward_test"
                             else "PROMOTED by the shadow forward test (live evidence, not a guarantee)"
                             if quant["state"] == "shadow_promoted"
                             else "parameters are unvalidated hypotheses - not a reliable predictive edge")}
        sig = {"strategy": setup.strategy, "symbol": setup.symbol, "direction": setup.direction,
               "entry": round(plan.entry, 6), "stop": round(plan.stop, 6), "target": round(plan.target, 6),
               "rr_net": plan.reward_risk_net, "score": total, "score_on_available": on_avail,
               "available_weight": avail, "confidence": confidence, "status": status,
               "reject_reason": "; ".join(problems) or None, "data_ts": data_ts,
               "validation_status": quant["state"],
               "persisted": False}
        if mode != "live":
            return sig
        key = f"{setup.symbol}|{setup.strategy}|{tf}|{data_ts}"
        try:
            with self.db.transaction():
                sid = self.db.insert("signals", {
                    "ts": utcnow(), "symbol": setup.symbol, "asset_class": ctx.asset_class.value,
                    "strategy": setup.strategy, "direction": setup.direction, "entry_price": plan.entry,
                    "stop_loss": plan.stop, "take_profit": plan.target, "reward_risk": plan.reward_risk_net,
                    "data_ts": data_ts, "status": status, "evidence_json": json.dumps(evidence, default=str),
                    "timeframe": tf, "signal_key": key, "expires_ts": expires.isoformat(),
                    "data_status": s.status.value, "scan_run_id": run_id,
                    "reject_reason": sig["reject_reason"], "validation_status": quant["state"]})
                self.db.insert("signal_scores", {
                    "signal_id": sid, "ts": utcnow(), "total_score": total, **comps,
                    "weights_json": json.dumps(self.settings.scoring.weights.model_dump()),
                    "data_quality": json.dumps({"status": s.status.value, "crosscheck": evidence["crosscheck"],
                                                "warnings": s.warnings[:5]}),
                    "available_weight": avail, "score_on_available": on_avail})
                self.db.insert("strategy_decisions", {
                    "ts": utcnow(), "signal_id": sid,
                    "decision": "reject" if status == "rejected" else "skip",
                    "reason": sig["reject_reason"] or (
                        "incomplete score: " + ", ".join(missing) + " - never executed"
                        if missing else "complete signal - handed to the risk engine by the paper trader")})
            sig["persisted"], sig["signal_id"] = True, sid
        except sqlite3.IntegrityError as e:
            if "signal_key" in str(e) or "UNIQUE" in str(e):
                sig["duplicate"] = True    # same setup on the same bar already journaled
            else:
                raise
        return sig

    def _benchmark_trend(self, ac: AssetClass, tf: str, now) -> str | None:
        sym = "SPY" if ac == AssetClass.STOCK else "BTC-USD"
        try:
            fr = self.engine.get_candles(sym, tf, 300, now=now)
            df = to_frame(fr.series.candles)
            if ac == AssetClass.STOCK and "session" in df:
                df = df[df["session"] == "regular"]
            if len(df) < 60:
                return None
            self._bench_ret = float(df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100
            t = compute_all(df, ac)["trend"].iloc[-1]
            return t if isinstance(t, str) else None
        except Exception as e:
            self.db.log_event("benchmark_trend_unavailable", "scanner", f"{sym}: {e}", "warning")
            return None

    # ------------------------------------------------------------------ order books
    def _book(self, sym, preferred) -> BookSnapshot | None:
        order = [preferred] + [n for n in self.engine.sources_for(AssetClass.CRYPTO) if n != preferred]
        for name in order:
            prov = self.engine.providers.get(name)
            if prov is None:
                continue
            try:
                return fetch_book(prov, sym)
            except ProviderError as e:
                self.db.log_event("orderbook_unavailable", f"data.{name}", f"{sym}: {e}", "warning")
        return None

    def _store_book(self, b: BookSnapshot) -> None:
        b05, a05 = b.depth_usd(0.5)
        b1, a1 = b.depth_usd(1.0)
        self.db.insert("orderbook_snapshots", {
            "ts": b.ts.isoformat(), "symbol": b.symbol, "source": b.source, "best_bid": b.best_bid,
            "best_ask": b.best_ask, "spread_bps": b.spread_bps, "bid_depth_05_usd": b05,
            "ask_depth_05_usd": a05, "bid_depth_1_usd": b1, "ask_depth_1_usd": a1,
            "imbalance_05": b.imbalance(0.5), "levels": min(len(b.bids), len(b.asks))})

    def _book_history(self, sym, source, n: int = 50) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM orderbook_snapshots WHERE symbol=? AND source=? ORDER BY id DESC LIMIT ?",
            (sym, source, n)).fetchall()
        return [dict(r) for r in rows]


def _features(ctx, entry: float) -> dict:
    """D104: entry-time features for the post-trade review (rsi14, atr14, rvol20, distance to resistance in ATR)."""
    out = {}
    try:
        last = ctx.last
        for k in ("rsi14", "atr14", "rvol20"):
            v = last.get(k)
            out[k] = round(float(v), 4) if v is not None and v == v else None
        atr = out.get("atr14")
        res = sorted(L["price"] for L in (ctx.levels or []) if "high" in L.get("kinds", []) and L["price"] > entry)
        out["res_dist_atr"] = round((res[0] - entry) / atr, 2) if res and atr else None
    except Exception:
        pass
    return out
