"""Download Alpaca free (IEX) daily (~5y) and 15m (~1y) bars for the stock watchlist into var/history/stk/."""
import sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd, requests
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.config import load_secrets, load_settings
OUT = Path(__file__).resolve().parents[2] / "var/history/stk"

def bars(sym, tf, start, sec, s):
    hdr = {"APCA-API-KEY-ID": sec.alpaca_api_key_id.get_secret_value(),
           "APCA-API-SECRET-KEY": sec.alpaca_api_secret_key.get_secret_value()}
    p = {"timeframe": tf, "start": start, "limit": 10000, "feed": "iex", "adjustment": "all", "sort": "asc"}
    rows = []
    while True:
        r = s.get(f"https://data.alpaca.markets/v2/stocks/{sym}/bars", params=p, headers=hdr, timeout=30)
        if r.status_code == 429:
            time.sleep(5); continue
        r.raise_for_status(); j = r.json(); rows += j.get("bars") or []
        if not j.get("next_page_token"): break
        p["page_token"] = j["next_page_token"]; time.sleep(0.3)
    df = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    if df.empty: return df
    df.index = pd.to_datetime(df.pop("t"), utc=True)
    return df[["open", "high", "low", "close", "volume"]]

if __name__ == "__main__":
    S, sec = load_settings(), load_secrets()
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    s = requests.Session()
    for sym in S.markets.stocks.watchlist:
        for tf, days in (("1Day", 365 * 6), ("15Min", 400)):
            f = OUT / f"{sym}_{tf}.csv.gz"
            if f.exists(): continue
            try:
                df = bars(sym, tf, (now - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z"), sec, s)
                df.to_csv(f); print(sym, tf, len(df), flush=True)
            except Exception as e:
                print(sym, tf, "ERR", type(e).__name__, flush=True)
            time.sleep(0.3)
