# Using ATLAS with ChatGPT (or another chat AI)

## What works and what doesn't (honest)
- **Works:** upload the repo ZIP to ChatGPT (code interpreter / "Advanced data analysis") or a Custom GPT and ask it to
  explain, review, modify code, run the **offline** test suite, or analyse exported data (e.g. a copy of `logs/loop.log`
  or a CSV you export).
- **Does NOT work:** running the 24/7 trading loop or the dashboard inside ChatGPT. Its sandbox has no persistent process,
  usually no internet access, and is wiped after the session. Live scans, news ingest, Alpaca PAPER mirroring and
  backtests on fresh market data need a real machine (your PC, a Mac, a Linux server/VPS) - see `START_CZ.md` / `AGENTS.md`.
- Never upload `.env`, `var/` (the ledger DB) with personal data, or any key to ChatGPT. The exported ZIP excludes them.

## How to use
1. Download the repo as ZIP (GitHub -> Code -> Download ZIP) - it contains no secrets.
2. Open ChatGPT -> attach the ZIP -> paste the instruction block below as the first message
   (or put it into a Custom GPT's "Instructions" field and attach the ZIP as Knowledge).
3. To bring changes back: ask ChatGPT for a patched ZIP or a unified diff, apply it on your machine, run `./start.sh test`.

## Instruction block (copy-paste)
```
You are helping with ATLAS Trading AI (attached ZIP). First unzip it and read AGENTS.md completely; obey every rule in it.
Key rules: PAPER trading only - never add live brokerage execution or real-money endpoints; never touch crypto wallets or
private keys; never loosen the risk limits ($2.50 risk/trade on $1,000, $10 daily stop, max 2 positions, no leverage,
net R:R >= 2); never fabricate data or results; free data sources only; never ask me to paste API keys.
You cannot run the 24/7 loop here - only read, explain, modify code and run offline tests:
  pip install -r requirements.txt && python -m pytest -m "not live" -q
When you change code: keep all tests passing, add a DECISIONS.md entry, and give me a downloadable ZIP or a unified diff.
```
