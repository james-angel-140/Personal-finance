# Finance Tool

A personal finance visualiser that brings Monzo, an HSBC credit card, and a
Fidelity ISA into one place, handles housemate money that passes through the
account, and adds an AI layer on top.

See [`DESIGN.md`](DESIGN.md) for the architecture and [`schema.sql`](schema.sql)
for the database.

## Get it onto GitHub (private)

```bash
cd finance-tool
git init
git add .
git commit -m "Initial scaffold: schema, design, netting model"
# Create a PRIVATE repo on GitHub, then:
git remote add origin git@github.com:<you>/finance-tool.git
git push -u origin main
```

Private matters: this repo will sit next to financial data and API keys. The
included `.gitignore` keeps `.env` and the `data/` database out of git — keep it
that way.

## Build it with Claude Code

1. Install Claude Code (`npm install -g @anthropic-ai/claude-code`, needs
   Node 18+) or download the desktop app from claude.com/download. Included with
   Claude Pro/Max.
1. Open this folder with Claude Code. It reads `CLAUDE.md` automatically, so it
   already knows the architecture, the netting model, and the build order.
1. Tell it which piece to build first (recommended: the database layer, then the
   Monzo connection).

## Local setup (once code exists)

```bash
cp .env.example .env      # then fill in your Monzo + Anthropic keys
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Dashboard

A mobile-friendly dashboard that lives on **GitHub Pages**. Because Pages is
public and static, the data is **encrypted**: an export script writes an
AES-256-GCM blob and the page decrypts it in your browser after you enter a
passphrase. No server, no accounts, no plaintext committed.

```bash
# Refresh / publish the data (uses a passphrase from the environment):
DASHBOARD_PASSPHRASE='your strong passphrase' python -m src.export_dashboard
git add dashboard/data.enc.json && git commit -m "Refresh dashboard data" && git push
```

The push triggers `.github/workflows/pages.yml`, which redeploys the site. Open
it, enter the same passphrase, and you're in. See [`dashboard/README.md`](dashboard/README.md)
for details. The passphrase is never stored — keep it strong; the blob is public.

## Status

- [x] Database schema + housemate netting model (built & tested)
- [x] Architecture and design
- [x] Database layer (`db.py`), seed, interactive setup assistant
- [x] Monzo **CSV** ingestion
- [x] Classification — deterministic rules (`classify/rules.py`)
- [x] Dashboard (encrypted static site on GitHub Pages)
- [ ] Live Monzo OAuth sync · HSBC + Fidelity CSV importers
- [ ] Classification — Claude fallback for uncategorised txns
- [ ] "Chat with your finances"
