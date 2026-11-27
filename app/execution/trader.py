"""PAPER TRADER: one deterministic cycle =
manage open positions -> (if trading allowed) scan -> risk-evaluate complete signals
-> simulate fills -> journal -> equity snapshot. Kill switch included."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.core.logging_setup import get_logger
from app.core.safety import SafeState
from app.data.calendar import ET
from app.data.engine import MarketDataEngine, asset_class_of
from app.data.models import AssetClass, tf_seconds
from app.database.db import Database
from app.execution.alpaca_paper import AlpacaPaperBroker
from app.execution.management import TradeManager
from app.execution.mirror import AlpacaMirror
from app.shadow.engine import ShadowEngine
from app.execution.simulator import DuplicateOrder, PaperSimulator
from app.portfolio.ledger import Ledger
from app.risk.leverage import daily_pct, risk_pct
from app.risk.engine import MarketQuote, Proposal, RiskEngine

log = get_logger("trader")


class PaperTrader:
    def __init__(self, settings: Settings, db: Database, safe: SafeState, engine: MarketDataEngine,
                 scanner=None, alpaca: AlpacaPaperBroker | None = None):
        self.s, self.db, self.safe, self.engine, self.scanner = settings, db, safe, engine, scanner
        self.ledger = Ledger(settings, db)
        self.risk = RiskEngine(settings, db, safe, self.ledger)
        self.sim = PaperSimulator(settings, db, safe, self.ledger)
        self.alpaca = alpaca
        self.mirror = AlpacaMirror(alpaca, db) if alpaca is not None else None
        self.shadow = ShadowEngine(settings, db)
        self.tm = TradeManager(settings, db, self.mirror)
        self._b15: dict = {}

    # ------------------------------------------------------------------ market access
    def quote(self, symbol: str, now: datetime) -> MarketQuote | None:
        cc, quotes, _ = self.engine.get_quote_consensus(symbol)
        if cc.consensus is None or cc.verdict == "divergent":     # non-core divergence no longer halts: never trade it
            return None
        maxsp = self.s.data.max_quote_spread_bps
        book = [q for q in quotes if q.bid and q.ask and q.quote_currency == "USD"
                and q.spread_bps is not None and q.spread_bps <= maxsp]
        best = book[0] if book else None
        ts = max((q.source_ts for q in quotes if q.source_ts), default=None) or now
        adv = None
        try:
            tf = (self.s.scanner.stock_timeframe if asset_class_of(symbol) == AssetClass.STOCK
                  else self.s.scanner.crypto_timeframe)
            c = self.engine.get_candles(symbol, tf, 30, crosscheck=False).series.complete[-20:]
            if c:
                adv = sum(k.close * k.volume for k in c) / len(c)
        except Exception:
            pass
        price = best.price if best else cc.consensus
        return MarketQuote(price, best.bid if best else None, best.ask if best else None, ts, cc.verdict, adv)

    # ------------------------------------------------------------------ cycle parts
    def manage(self, now: datetime) -> list[dict]:
        out = []
        from app.data.calendar import regular_session_open
        for pos in self.ledger.open_positions():
            sym = pos["symbol"]
            swing = self._is_swing(pos)
            if swing and not regular_session_open(now):
                # D103: held overnight; nothing can fill while the market is closed. The stop is re-checked
                # from the next regular-session bars (a gap through it fills at the open).
                out.append({"symbol": sym, "status": "open", "note": "held overnight (market closed)"})
                continue
            try:
                opened = datetime.fromisoformat(pos["opened_ts"])
                mins = int((now - opened).total_seconds() / tf_seconds(self.s.execution.manage_timeframe)) + 5
                fr = self.engine.get_candles(sym, self.s.execution.manage_timeframe, min(1000, max(10, mins)),
                                             crosscheck=False, now=now)
                bars = fr.series.candles
                if swing:                       # regular-session bars only: a stop order works 09:30-16:00
                    bars = [b for b in bars if getattr(b, "session", None) in (None, "regular")]
                if not bars:
                    raise RuntimeError("no bars")
                try:
                    q = self.quote(sym, now)
                except Exception:
                    q = None
                r = self.sim.manage_position(pos, bars, now, q)
                if swing and r.get("status") == "open":
                    why = self._swing_exit(pos, q, now)
                    if why:
                        r = self.sim.close_position(pos, self.sim.costs.sell_fill(
                            AssetClass.STOCK, q.price, q.bid, q.ask), why, now)
                if "reason" in r and self.mirror is not None:      # internal exit -> mirror it
                    r["alpaca"] = self.mirror.close(pos["id"], sym, pos["asset_class"])
                elif (r.get("status") == "open" and self.s.execution.trade_management_enabled and r.get("mark")
                      and not swing):           # D103: the swing keeps its backtested exits only
                    try:
                        r["adjustments"] = self.tm.apply("main", pos, self.bars15(sym, now), r["mark"], now,
                                                         lambda p, why: self._close_main(p, why, q, now))
                    except Exception as e:                           # management failure never touches the stop
                        self.db.log_error("execution.trade_management", e)
                out.append({"symbol": sym, **r})
            except Exception as e:
                # cannot watch the stop -> fail closed for NEW trades; position stays open, retried next cycle
                self.db.log_error("execution.manage", e)
                self.safe.halt(f"cannot manage open position {sym}: {type(e).__name__}: {e}", module="execution")
                out.append({"symbol": sym, "error": str(e)})
        return out

    # ------------------------------------------------------------------ D103 RSI(2) overnight swing
    def _is_swing(self, pos) -> bool:
        return pos["asset_class"] == "stock" and pos["strategy"] == self.s.swing.strategy

    def _daily(self, sym: str, now: datetime):
        """Daily bars incl. today's forming bar, straight from the providers (a context read: never SAFE STATE)."""
        from app.strategies.swing import daily_frame, ny_day
        today = now.astimezone(ET).date()
        for name in self.engine.sources_for(AssetClass.STOCK):
            try:
                df = daily_frame(self.engine.providers[name].fetch_candles(sym, "1d", 260, now=now))
            except Exception:
                continue
            if len(df) and ny_day(df.index[-1]) == today:
                return df
        return None

    def _swing_exit(self, pos, q, now: datetime) -> str | None:
        from app.strategies.swing import exit_reason, in_window
        if q is None or not in_window(now, self.s.swing):
            return None
        return exit_reason(self._daily(pos["symbol"], now), datetime.fromisoformat(pos["opened_ts"]), q.price,
                           self.s.swing)

    def swing_scan(self, now: datetime) -> list[int]:
        """Once per trading day inside 15:45-15:59 New York: RSI(2) setups on the stock watchlist -> signals."""
        cfg = self.s.swing
        from app.strategies.swing import STRATEGY, expires, in_window, setup, why_not
        if not (cfg.enabled and self.s.markets.stocks.enabled) or not in_window(now, cfg):
            return []
        day = now.astimezone(ET).date().isoformat()
        st = self.db.get_state("swing_scan") or {}
        if st.get("day") == day:
            return [st["run_id"]] if st.get("run_id") else []
        reg = None
        try:
            reg = self.scanner._current_regime(AssetClass.STOCK, now) if self.scanner else None
        except Exception as e:
            self.db.log_error("regime", e)
        reg = reg or self.db.get_state("regime_stock")
        from app.regime import allowed
        cells = (self.s.regime.matrix.get("stock") or {}).get(STRATEGY) or []
        if not reg or f"{reg['trend']}/{reg['vol']}" not in cells:      # overnight risk: fail closed on regime
            why = (f"regime {reg['trend']}/{reg['vol']}: strategy off in this market" if reg
                   else "regime unknown: overnight swing off")
            self.db.set_state("swing_scan", {"day": day, "run_id": None, "skipped": why})
            self.db.log_event("swing_skipped", "swing", f"RSI(2) swing: {why}")
            return []
        assert allowed(self.s.regime.matrix, "stock", STRATEGY, reg)
        self.db.set_state("swing_scan", {"day": day, "run_id": None, "started": now.isoformat()})
        wl = list(self.s.markets.stocks.watchlist)
        run_id = self.db.insert("scan_runs", {"started_ts": now.isoformat(), "scanner": "stock", "mode": "live",
                                              "timeframe": "1d", "symbols": ",".join(wl),
                                              "config_hash": "d103-rsi2"})
        found, rows = 0, []
        for sym in wl:
            df = self._daily(sym, now)
            plan = setup(df, cfg) if df is not None else None
            row = {"symbol": sym, "indicators": {"close": float(df["close"].iloc[-1]) if df is not None else None}}
            if not plan:
                rows.append(row | {"no_setup": {STRATEGY: why_not(df, cfg)}})
                continue
            rows.append(row | {"signals": [{"strategy": STRATEGY, "status": "new", "rr_net": None,
                                            "score": cfg.score}]})
            found += 1
            ev = {"strategy": STRATEGY, "why": plan["why"], "rsi2": plan["rsi2"], "sma200": plan["sma200"],
                  "atr10": plan["atr"], "regime": reg, "news": {}}
            sid = self.db.insert("signals", {
                "ts": now.isoformat(), "symbol": sym, "asset_class": "stock", "strategy": STRATEGY,
                "direction": "long", "entry_price": plan["entry"], "stop_loss": plan["stop"],
                "take_profit": plan["target"], "reward_risk": cfg.target_r - plan["rsi2"] / 100,   # deeper dip first
                "data_ts": now.isoformat(), "status": "new", "evidence_json": json.dumps(ev, default=str),
                "timeframe": "1d", "signal_key": f"{STRATEGY}:{sym}:{day}", "expires_ts": expires(now, cfg).isoformat(),
                "data_status": "live", "scan_run_id": run_id, "validation_status": "forward_test"})
            self.db.insert("signal_scores", {"signal_id": sid, "ts": now.isoformat(), "total_score": cfg.score,
                                             "weights_json": json.dumps({"rule_based": STRATEGY})})
        self.db.conn.execute("UPDATE scan_runs SET finished_ts=?, summary_json=? WHERE id=?",
                             (datetime.now(timezone.utc).isoformat(), json.dumps({"symbols": rows, "d103": True}),
                              run_id))
        self.db.set_state("swing_scan", {"day": day, "run_id": run_id, "signals": found})
        self.db.log_event("swing_scan", "swing", f"RSI(2) swing scan: {found} setups in {len(wl)} stocks "
                                                 f"(regime {reg['trend']}/{reg['vol']})")
        return [run_id]

    def bars15(self, sym: str, now: datetime):
        k = (sym, now.replace(second=0, microsecond=0))
        if k not in self._b15:
            self._b15 = {kk: v for kk, v in self._b15.items() if kk[1] == k[1]}
            self._b15[k] = self.engine.get_candles(sym, "15m", 120, crosscheck=False, now=now).series.candles
        return self._b15[k]

    def _close_main(self, pos: dict, reason: str, q, now: datetime) -> dict:
        ac = AssetClass(pos["asset_class"])
        cur = dict(self.db.conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone())
        last = q.price if q else cur["mark_price"]
        res = self.sim.close_position(cur, self.sim.costs.sell_fill(ac, last, q.bid if q else None,
                                                                    q.ask if q else None), reason, now)
        if self.mirror is not None:
            res["_alpaca"] = self.mirror.close(pos["id"], pos["symbol"], pos["asset_class"])
        return res

    def candidates(self, run_ids: list[int]) -> list:
        if not run_ids:
            return []
        qm = ",".join("?" * len(run_ids))
        return self.db.conn.execute(
            f"SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id = s.id "
            f"WHERE s.scan_run_id IN ({qm}) AND s.status = 'new' "
            f"ORDER BY s.reward_risk DESC, sc.total_score DESC", run_ids).fetchall()

    def _llm_check(self, row, now: datetime) -> dict | None:
        """D102: one APPROVE / VETO per signal (cached). Any failure -> None (entry proceeds as before)."""
        if not self.s.llm.enabled:
            return None
        try:
            from app.llm import LOG_KEY, LocalLLM
            for v in reversed(self.db.get_state(LOG_KEY) or []):
                if v.get("signal_id") == row["id"]:
                    return v
            llm = LocalLLM(self.s, self.db)
            ev = json.loads(row["evidence_json"] or "{}")
            news = ev.get("news") or {}
            agg = news.get("aggregate")
            fin = round(float(agg), 2) if isinstance(agg, (int, float)) else None
            sym = row["symbol"]
            tick = sym.split("-")[0] if row["asset_class"] == "crypto" else sym
            hl = [dict(r) for r in self.db.conn.execute(
                "SELECT title, sentiment FROM news_events WHERE (tickers LIKE ? OR tickers LIKE ?) "
                "AND COALESCE(published_ts, fetched_ts) >= ? ORDER BY id DESC LIMIT 6",
                (f"%,{tick},%", f"%,{sym},%", (now - timedelta(hours=24)).isoformat()))]
            sig = {"signal_id": row["id"], "symbol": sym, "asset_class": row["asset_class"],
                   "strategy": row["strategy"], "entry": row["entry_price"], "stop": row["stop_loss"],
                   "target": row["take_profit"], "rr": row["reward_risk"], "finbert": fin}
            reg = self.db.get_state(f"regime_{row['asset_class']}")
            v = llm.check(sig, reg, hl)
            if v["decision"] == "SKIPPED":
                return None                                  # fallback logged inside; not cached -> retried later
            llm.record(sig, v, now)
            if v["decision"] == "VETO":
                self.db.insert("strategy_decisions", {"ts": now.isoformat(), "signal_id": row["id"],
                                                      "decision": "reject", "reason": f"llm veto: {v['reason']}"})
            return v
        except Exception as e:
            self.db.log_error("llm", e)
            return None

    PENDING_KEY = "pending_limit_entries"

    def _limit_gate(self, p, q, now: datetime) -> str | None:
        """D101 maker entry: the first sighting places a post-only limit at the current bid; it only fills if a later
        quote trades at or below it before the TTL, otherwise no trade. Returns a reason while not filled."""
        pend = self.db.get_state(self.PENDING_KEY, {}) or {}
        cutoff = (now - timedelta(days=1)).isoformat()
        pend = {k: v for k, v in pend.items() if v["placed"] >= cutoff}
        key = str(p.signal_id)
        cur = pend.get(key)
        if cur is None:
            lim = q.bid if q.bid else q.price
            pend[key] = {"symbol": p.symbol, "limit": lim, "placed": now.isoformat(),
                         "expires": (now + timedelta(minutes=self.s.execution.crypto_limit_ttl_minutes)).isoformat()}
            self.db.set_state(self.PENDING_KEY, pend)
            self.db.log_event("limit_placed", "execution", f"{p.symbol} post-only buy limit {lim:.6g} (maker)")
            return "limit order placed at the bid, waiting for a fill"
        if cur.get("done"):
            return "limit order already handled"
        if now.isoformat() > cur["expires"]:
            cur["done"] = "expired"
            self.db.set_state(self.PENDING_KEY, pend)
            self.db.log_event("limit_expired", "execution", f"{p.symbol} limit {cur['limit']:.6g} not filled")
            return "limit order not filled in time (price never came back down)"
        if q.price > cur["limit"]:
            return "limit order waiting (price above the limit)"
        cur["done"] = "filled"
        self.db.set_state(self.PENDING_KEY, pend)
        return None

    def pending_signals(self, now: datetime) -> list:
        """Signals with a live (unexpired, unfilled) entry limit from an earlier cycle."""
        pend = self.db.get_state(self.PENDING_KEY, {}) or {}
        ids = [int(k) for k, v in pend.items() if not v.get("done") and v["expires"] >= now.isoformat()]
        if not ids:
            return []
        qm = ",".join("?" * len(ids))
        return self.db.conn.execute(
            f"SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id = s.id "
            f"WHERE s.id IN ({qm}) AND s.status = 'new' ORDER BY s.reward_risk DESC", ids).fetchall()

    def execute_signal(self, row, now: datetime) -> dict:
        p = Proposal.from_signal_row(row)
        if p.asset_class == AssetClass.STOCK:
            from app.data.calendar import regular_session_open
            if not regular_session_open(now):
                # D99: a stock signal from a scan that started before the close: don't even ask for a quote
                # (right after the close Alpaca's book is empty, which used to show up as data errors)
                self.db.insert("strategy_decisions", {"ts": now.isoformat(), "signal_id": p.signal_id,
                                                      "decision": "reject", "reason": "risk: market closed"})
                return {"signal_id": p.signal_id, "symbol": p.symbol, "strategy": p.strategy, "approved": False,
                        "reason": "market closed", "qty": 0, "planned_risk_usd": 0, "rr_net": None}
        try:
            q = self.quote(p.symbol, now)
        except Exception as e:
            self.db.log_error("execution.quote", e)
            q = None
        if q is not None and self.sim.costs.maker_entry(p.asset_class):
            wait = self._limit_gate(p, q, now)       # D101: post-only limit at the bid must actually get hit
            if wait:
                return {"signal_id": p.signal_id, "symbol": p.symbol, "strategy": p.strategy, "approved": False,
                        "reason": wait, "qty": 0, "planned_risk_usd": 0, "rr_net": None}
        from app.risk import leverage as LV
        swing = p.asset_class == AssetClass.STOCK and p.strategy == self.s.swing.strategy
        if swing:                               # D103: unlevered, own slot limit, backtested levels unchanged
            n_sw = sum(1 for x in self.ledger.open_positions() if self._is_swing(x))
            if n_sw >= self.s.swing.max_positions:
                self.db.insert("strategy_decisions", {"ts": now.isoformat(), "signal_id": p.signal_id,
                                                      "decision": "reject", "reason": "swing: max overnight positions"})
                return {"signal_id": p.signal_id, "symbol": p.symbol, "strategy": p.strategy, "approved": False,
                        "reason": "max overnight positions", "qty": 0, "planned_risk_usd": 0, "rr_net": None}
        if LV.lev(self.s) > 1 and q is not None and p.stop and p.target and p.entry and not swing:   # D94 scalp levels
            import dataclasses
            from app.technical.indicators import atr, to_frame
            try:
                df = to_frame(self.bars15(p.symbol, now))
                a15 = float(atr(df, 14).iloc[-1]) if len(df) >= 15 else None
            except Exception:
                a15 = None
            fill = self.sim.costs.buy_fill(p.asset_class, q.price, q.bid, q.ask).price
            if self.s.risk.exit_mode == "atr":           # D99: ATR distances from the fill, adaptive leverage
                st_, tg_, L_ = LV.atr_levels(self.s, fill, p.entry, p.stop, p.target)
                if st_ < fill < tg_:
                    p = dataclasses.replace(p, stop=st_, target=tg_, leverage=L_)
            elif p.stop < fill < p.target:
                st_, tg_ = LV.scalp_levels(self.s, fill, p.stop, p.target, a15)
                p = dataclasses.replace(p, stop=st_, target=tg_, leverage=LV.lev(self.s))
        d = self.risk.evaluate(p, q, now)
        res = {"signal_id": p.signal_id, "symbol": p.symbol, "strategy": p.strategy, "approved": d.approved,
               "evaluation_id": d.evaluation_id, "reason": d.reason, "qty": d.quantity,
               "planned_risk_usd": d.planned_risk_usd, "rr_net": d.rr_net}
        if not d.approved:
            self.db.insert("strategy_decisions", {"ts": now.isoformat(), "signal_id": p.signal_id,
                                                  "decision": "reject", "reason": f"risk: {d.reason}"})
            return res
        try:
            res["fill"] = self.sim.open_position(p, d, q, now)
        except DuplicateOrder as e:
            res.update(approved=False, reason=str(e))
            return res
        if self.mirror is not None and self.mirror.enabled:     # validated AND forward_test (D85); ledger = truth
            f = res["fill"]
            # D94: Alpaca can't do 100x -> mirror at 1x size (qty / leverage = the margin's worth)
            res["alpaca"] = self.mirror.open(p.symbol, p.asset_class.value, f["quantity"] / p.leverage, f["fill_price"], p.stop,
                                             p.target, f["client_order_id"], f["order_id"],
                                             self.ledger.state(now).equity)
        return res

    # ------------------------------------------------------------------ public
    def run_once(self, now: datetime | None = None, scan: bool = True) -> dict:
        now = now or datetime.now(timezone.utc)
        report = {"ts": now.isoformat(), "managed": self.manage(now), "executions": [], "scans": []}
        report["shadow"] = {"managed": self._shadow(lambda: self.shadow.manage(
            self._shadow_bars(now), now, (lambda s: self.bars15(s, now)) if self.s.execution.trade_management_enabled
            else None)), "opened": []}
        if scan and self.scanner is not None and self.safe.trading_allowed():
            run_ids = []
            for ac in (AssetClass.STOCK, AssetClass.CRYPTO):
                syms = self.scan_batch(ac, now)
                if not syms:
                    report["scans"].append({"scanner": ac.value, "skipped": "US market closed"})
                    try:                        # D100: keep the dashboard's market mood current while closed
                        self.scanner._current_regime(ac, now)
                    except Exception as e:
                        self.db.log_error("regime", e)
                    continue
                rep = self.scanner.run(ac, symbols=syms, mode="live")
                run_ids.append(rep["run_id"])
                report["scans"].append({"scanner": ac.value, "run_id": rep["run_id"], "signals": rep["signals"],
                                        "symbols": len(syms)})
            # shadow ledger first (every strategy, separate ledger; never affects the portfolio / risk / Alpaca)
            report["shadow"]["opened"] = self._shadow(lambda: self.shadow.open_from_signals(run_ids, self.quote, now))
            try:                                                    # D103: near-the-close RSI(2) swing signals
                sw = self.swing_scan(now)
                if sw:
                    report["scans"].append({"scanner": "stock_swing", "run_id": sw[0]})
                run_ids += sw
            except Exception as e:
                self.db.log_error("swing", e)
            seen, checked = set(), 0
            for row in list(self.pending_signals(now)) + list(self.candidates(run_ids)):
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                if not self.safe.trading_allowed():
                    break
                if checked < self.s.llm.max_candidates:            # D102: AI final check, can only veto
                    checked += 1
                    v = self._llm_check(row, now)
                    if v and v["decision"] == "VETO":
                        report["executions"].append({"signal_id": row["id"], "symbol": row["symbol"],
                                                     "approved": False, "reason": f"AI veto: {v['reason']}"})
                        continue
                report["executions"].append(self.execute_signal(row, now))
        if self.s.llm.enabled:
            try:
                from app.llm import LocalLLM
                LocalLLM(self.s, self.db).summarize_news(now)
            except Exception as e:                                  # never let the LLM break a cycle
                self.db.log_error("llm", e)
        st = self.ledger.state(now)
        self.ledger.snapshot(st, "cycle")
        report["portfolio"] = summary(st, self.s)
        report["safe_state"] = self.safe.status().reason
        return report

    def scan_batch(self, ac: AssetClass, now: datetime) -> list[str]:
        """Next slice of the watchlist (rotating cursor in system_state) so a big universe fits a short cycle."""
        stock = ac == AssetClass.STOCK
        sc = self.s.scanner
        if stock and sc.stocks_only_in_market_hours:
            from app.data.calendar import regular_session_open
            if not regular_session_open(now):
                return []
        wl = list(self.s.markets.stocks.watchlist if stock else self.s.markets.crypto.watchlist)
        if not (self.s.markets.stocks.enabled if stock else self.s.markets.crypto.enabled):
            return []
        n = sc.stock_batch_size if stock else sc.crypto_batch_size
        if not wl or n <= 0 or n >= len(wl):
            return wl
        key = f"scan_cursor_{ac.value}"
        cur = int(self.db.get_state(key, 0) or 0) % len(wl)
        batch = (wl + wl)[cur:cur + n]
        self.db.set_state(key, (cur + n) % len(wl))
        return batch

    def _shadow(self, fn):
        try:
            return fn()
        except Exception as e:                       # the shadow test must never break a trading cycle
            self.db.log_error("shadow", e)
            return [{"error": f"{type(e).__name__}: {e}"}]

    def _shadow_bars(self, now: datetime):
        cache: dict = {}

        def bars(pos):
            sym = pos["symbol"]
            if sym not in cache:
                opened = datetime.fromisoformat(pos["opened_ts"])
                n = int((now - opened).total_seconds() / tf_seconds(self.s.execution.manage_timeframe)) + 5
                cache[sym] = self.engine.get_candles(sym, self.s.execution.manage_timeframe, min(1000, max(10, n)),
                                                     crosscheck=False, now=now).series.candles
            return cache[sym]
        return bars

    def kill(self, reason: str, flatten: bool, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        self.safe.halt(f"KILL SWITCH: {reason}", module="kill_switch")
        closed = []
        if flatten:
            marks = {}
            for pos in self.ledger.open_positions():
                try:
                    marks[pos["symbol"]] = self.quote(pos["symbol"], now)
                except Exception:
                    marks[pos["symbol"]] = None
            opened = {p["id"]: p for p in self.ledger.open_positions()}
            closed = self.sim.flatten_all(marks, "kill_switch", now)
            if self.mirror is not None:
                for c in closed:
                    pos = opened.get(c["position_id"])
                    if pos:
                        c["alpaca"] = self.mirror.close(pos["id"], pos["symbol"], pos["asset_class"])
        st = self.ledger.state(now)
        self.ledger.snapshot(st, "kill switch")
        return {"halted": True, "closed": closed, "portfolio": summary(st, self.s)}


def summary(st, settings: Settings) -> dict:
    limit = st.day_start_equity * daily_pct(settings) / 100
    return {"equity": round(st.equity, 4), "cash": round(st.cash, 4), "positions_value": round(st.positions_value, 4),
            "realized_total": round(st.realized_total, 4), "unrealized": round(st.unrealized, 4),
            "total_pnl": round(st.equity - settings.portfolio.starting_equity_usd, 4),
            "trading_day": st.trading_day, "day_start_equity": st.day_start_equity,
            "daily_pnl": round(st.daily_pnl, 4), "daily_loss_limit": round(limit, 4),
            "daily_limit_reached": st.daily_loss >= limit, "open_positions": len(st.open_positions),
            "consecutive_losses_today": st.consecutive_losses_today,
            "risk_per_trade_budget": round(st.equity * risk_pct(settings) / 100, 4)}
