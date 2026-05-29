# Dashboard (encrypted static site)

A static, mobile-friendly finance dashboard for GitHub Pages. Because Pages is
public and can't run code, the data is **encrypted**: the export script writes
an AES-256-GCM blob (`data.enc.json`), and the page decrypts it in your browser
after you type your passphrase. No server, no accounts, no plaintext on disk.

All figures come from the netting views (`v_personal_flows`,
`v_housemate_ledger`), so housemate pass-through money never distorts them.

## Your steps (one-time setup)

1. **Enable Pages**: repo → Settings → Pages → *Build and deployment* →
   Source = **GitHub Actions**. (The workflow in `.github/workflows/pages.yml`
   does the rest.)

## Publishing / refreshing the data

Whenever you want the live site to reflect the latest database:

```bash
DASHBOARD_PASSPHRASE='your strong passphrase' python -m src.export_dashboard
git add dashboard/data.enc.json
git commit -m "Refresh dashboard data"
git push
```

Pushing triggers the Pages workflow, which redeploys the site. Open the site,
enter the same passphrase, and you're in.

- Use a **strong passphrase** (several words). The encrypted blob is publicly
  downloadable, so the passphrase is the only thing protecting it.
- Change the passphrase any time by re-running the export with a new one.
- `data.enc.json` is the **only** data artefact that should be committed. The
  database and any CSV/plaintext stay gitignored — never commit those.

## What's here

| File             | Purpose                                              |
|------------------|------------------------------------------------------|
| `index.html`     | Passphrase gate + dashboard layout                   |
| `app.js`         | WebCrypto decrypt + hand-rolled SVG/CSS charts       |
| `styles.css`     | Self-contained styling (no external fonts/CDNs)      |
| `data.enc.json`  | Encrypted payload (generated; commit to publish)     |

No third-party scripts are loaded — the page that holds your decrypted data
pulls nothing from any CDN.
