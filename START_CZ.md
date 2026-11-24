# ATLAS – rychlý start (česky)

> **Jen papírové obchodování.** ATLAS nikdy neobchoduje se skutečnými penězi. Obchoduje na interním účtu $1 000
> a obchody může zrcadlit na Alpaca **PAPER** účet (cvičný). Klíče patří jen do souboru `.env`, nikdy na GitHub ani do chatu.

## 1. Co potřebuješ
- **Python 3.12 nebo novější** (doporučeno 3.13) – https://www.python.org/downloads/
  (Windows: při instalaci zaškrtni **„Add python.exe to PATH“**).
- Volitelně Git (https://git-scm.com) – nebo stáhni ZIP z GitHubu a rozbal ho.

## 2. Instalace
**Windows** (v rozbalené složce, PowerShell nebo dvojklik na příkazový řádek):
```
start.bat setup
```
**Mac / Linux** (Terminál ve složce projektu):
```
chmod +x start.sh
./start.sh setup
```
Setup vytvoří virtuální prostředí `.venv`, nainstaluje knihovny, založí databázi a vytvoří `.env` z `.env.example`.

## 3. Klíče (.env)
Otevři soubor `.env` v Poznámkovém bloku / TextEditu a doplň jen to, co máš (vše je volitelné):
```
ALPACA_API_KEY_ID=        # Alpaca PAPER klíč (app.alpaca.markets -> Paper Trading -> API Keys)
ALPACA_API_SECRET_KEY=
ATLAS_CONTACT_EMAIL=      # tvůj e-mail pro SEC (jen hlavička požadavku)
CLOUDFLARE_API_TOKEN=     # jen pro publikování dashboardu na web
CLOUDFLARE_ACCOUNT_ID=
```
`.env` se nikdy nenahrává na GitHub (je v `.gitignore`).

## 4. Spuštění
| Co | Windows | Mac / Linux |
|---|---|---|
| Testy (bez internetu) | `start.bat test` | `./start.sh test` |
| Stav systému | `start.bat status` | `./start.sh status` |
| **Obchodní smyčka 24/7** (cyklus každých 60 s) | `start.bat loop` | `./start.sh loop` |
| **Dashboard** → otevři http://127.0.0.1:8765 | `start.bat dashboard` | `./start.sh dashboard` |

Smyčka a dashboard běží v okně terminálu – nech ho otevřené (druhé okno pro dashboard).
Počítač musí být zapnutý; když usne, smyčka stojí.

## 5. Zastavení
- **Zastavit smyčku:** `start.bat stop` / `./start.sh stop` (doběhne aktuální cyklus), nebo **Ctrl+C** v okně smyčky.
- **Nouzová brzda (kill switch):** `start.bat kill "duvod"` / `./start.sh kill "duvod"` – zastaví nové obchody a zavře papírové pozice.
- **Znovu povolit obchodování:** `start.bat resume` / `./start.sh resume`.

## 6. Práce s jinou AI (ChatGPT, Claude, Cursor, Copilot)
Všechna pravidla pro AI jsou v `AGENTS.md`. Pro ChatGPT viz `docs/CHATGPT_PROMPT.md`
(ChatGPT umí kód analyzovat a upravovat, ale **neumí** držet smyčku běžící 24/7 – ta musí běžet na tvém PC nebo serveru).
