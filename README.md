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

## Status

- [x] Database schema + housemate netting model (built & tested)
- [x] Architecture and design
- [ ] Database layer (`db.py`)
- [ ] Monzo ingestion
- [ ] HSBC + Fidelity CSV importers
- [ ] Classification (rules + AI)
- [ ] Dashboard
- [ ] "Chat with your finances"
