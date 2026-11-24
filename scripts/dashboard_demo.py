"""Throwaway DEMO: copies the real DB to /tmp, adds 2 DEMO positions from REAL candles, serves on :8799."""
import sqlite3, sys, threading, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from app.config import load_settings
from app.dashboard.charts import fetch_candles
from app.dashboard.server import serve
s = load_settings()
src = sqlite3.connect(f"file:{s.db_path.resolve()}?mode=ro", uri=True)
dst = sqlite3.connect("/tmp/atlas_demo.sqlite3")
src.backup(dst); src.close()
now = datetime.now(timezone.utc)
for sym, qty in (("BTC-USD", 0.004), ("ETH-USD", 0.12)):
    c, tf, _ = fetch_candles(sym, "crypto")
    entry = c[-12][4]; last = c[-1][4]
    risk = entry * 0.008
    dst.execute("INSERT INTO positions(opened_ts, symbol, asset_class, strategy, direction, quantity, entry_price, "
                "stop_loss, take_profit, fees_usd, slippage_usd, unrealized_pnl_usd, status, planned_risk_usd, "
                "initial_stop, mark_price, mark_ts, validation_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c[-11][0].isoformat(), sym, "crypto", "DEMO_news_momentum", "long", qty, entry, entry - risk,
                 entry + 2.5 * risk, 0.0, 0.0, 0.0, "open", 2.5, entry - risk, last, now.isoformat(), "DEMO"))
dst.commit(); dst.close()
srv = serve(s, "127.0.0.1", 8799, "/tmp/atlas_demo.sqlite3", live_prices=True, demo=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 40)
srv.shutdown()
