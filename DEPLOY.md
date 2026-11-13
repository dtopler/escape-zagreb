# Hosting (free, no card)

The site is split into two independent pieces so that nothing has to run
continuously and nothing has to be paid for:

```
  GitHub Actions  ── every 15 min ──►  scrape (aggregate.py)
                                            │
                                            ▼
                                   `data` branch: data.json
                                            │
  GitHub Pages (static, CDN) ◄──────────────┘
        web/ fetches raw.githubusercontent.com/<owner>/<repo>/data/data.json
```

| Piece | Where | Cost |
|---|---|---|
| Scraper (15 min) | GitHub Actions, `scrape.yml` | $0 (public repo → unlimited minutes) |
| Snapshot | `data` branch, single commit, force-pushed | $0 |
| Front end | GitHub Pages, `pages.yml` | $0 |

No servers, no sleep, no cold start, no payment method.

## One-time setup

1. Push this repo to GitHub as a **public** repository (public matters: Actions
   minutes are only free for public repos, and private repos would need
   ~8,600 min/month for a 15-minute cycle against a 2,000 min allowance).
2. **Settings → Pages → Source: GitHub Actions**. The `pages` workflow fails
   to deploy until this is set.
3. Run the scraper once: **Actions → scrape → Run workflow**. It publishes the
   first snapshot to the `data` branch in ~3–5 minutes.
4. The site appears at `https://<owner>.github.io/escape-zagreb/`.

## How it stays correct

- **`data` branch, not `main`.** Committing the snapshot every 15 minutes to the
  deployed branch would trigger ~96 Pages builds/day. Keeping it on its own
  branch means the site is built once and only the JSON moves.
- **Single-commit branch.** Each run force-pushes an orphan commit, so the
  branch never accumulates 96 commits/day of history.
- **`ESCAPE_CACHE_TTL=600`** in the workflow. The default cache TTL is 20
  minutes, which is *longer* than the 15-minute cycle — every run would find its
  cache still fresh and skip the scrape entirely.
- **Caches are restored across runs** (`actions/cache`) so a failing provider
  falls back to its last known answer instead of publishing "nothing free".
- **Empty snapshots are never published.** If every room comes back empty the
  run fails and the previous snapshot stays live.
- **Page cache-busting** is bucketed to 5 minutes to match the CDN's
  `Cache-Control: max-age=300` on `raw.githubusercontent.com`.

## Tuning

- **Cadence:** edit the `cron` in `scrape.yml`. GitHub's minimum is 5 minutes,
  and scheduled runs can start 5–30 minutes late under load.
- **Horizon:** `workflow_dispatch` takes a `days` input (default 30).
- **Manual refresh:** *Run workflow* on `scrape` — takes effect within the CDN's
  5-minute cache window.

## Local development is unchanged

```powershell
.\run.cmd
```

`web/config.js` leaves `window.ESCAPE_DATA_URL` null locally, so `app.js` talks
to the Python server's `/api/state` exactly as before. Only the Pages build
overwrites `config.js` with the published URL, which is what switches the UI
into static mode (the Refresh button becomes "reload snapshot").

## Known limits

- Data is 15 minutes old plus scheduler and CDN slack (typically 5–20 min).
- Scraping runs from GitHub's datacenter IPs, where Bookeo and SimplyBook may
  throttle more aggressively than from a home connection; the stale cache keeps
  those rooms showing their last known slots and flags them `stale`.
- The repository must stay public.
- GitHub disables scheduled workflows after 60 days without repository activity;
  the scraper commits every 15 minutes, which keeps it active.
