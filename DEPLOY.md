# Hosting (free, no card)

The site is split into two independent pieces so that nothing has to run
continuously and nothing has to be paid for:

```
  GitHub Actions  ── every hour ──►  scrape (aggregate.py, 21-day window)
                                            │
                                            ▼
                                   `data` branch: data.json
                                            │
  GitHub Pages (static, CDN) ◄──────────────┘
        web/ fetches raw.githubusercontent.com/<owner>/<repo>/data/data.json
```

| Piece | Where | Cost |
|---|---|---|
| Scraper (hourly) | GitHub Actions, `scrape.yml` | $0 (public repo → unlimited minutes) |
| Snapshot | `data` branch, single commit, force-pushed | $0 |
| Front end | GitHub Pages, `pages.yml` | $0 |

No servers, no sleep, no cold start, no payment method.

## One-time setup

1. Push this repo to GitHub as a **public** repository. Public matters: Actions
   minutes are only free for public repos, and hourly full scans would need
   roughly 6,000–7,000 min/month against a private repo's 2,000 min allowance.
2. **Settings → Pages → Source: GitHub Actions**. The `pages` workflow fails
   to deploy until this is set.
3. Run the scraper once: **Actions → scrape → Run workflow**. It publishes the
   first snapshot to the `data` branch in ~10–15 minutes.
4. The site appears at `https://<owner>.github.io/escape-zagreb/`.

## How it stays correct

- **`data` branch, not `main`.** Committing the snapshot hourly to the deployed
  branch would trigger ~24 Pages builds/day for no benefit. Keeping it on its
  own branch means the site is built once and only the JSON moves.
- **Single-commit branch.** Each run force-pushes an orphan commit, so the
  branch never accumulates history — it always holds exactly one commit.
- **`ESCAPE_CACHE_TTL=600`** in the workflow. It must stay well below the cron
  interval, otherwise a run finds its cache still fresh and skips the scrape.
- **Caches are restored across runs** (`actions/cache`) so a failing provider
  falls back to its last known answer instead of publishing "nothing free".
- **Empty snapshots are never published.** If every room comes back empty the
  run fails and the previous snapshot stays live.
- **Page cache-busting** is bucketed to 5 minutes to match the CDN's
  `Cache-Control: max-age=300` on `raw.githubusercontent.com`.

## Why hourly and 21 days

A full 31-day scan over 34 rooms took ~13 minutes locally and exceeded 20
minutes from a GitHub runner — far too slow to sustain a 15-minute cycle, and
near the 30-minute job timeout. Cutting the horizon to 21 days (−30%) brings it
back to a comfortable margin, and running hourly means a slow or throttled run
never collides with the next one.

Raise the horizon only if you also raise the cron interval.

## Tuning

- **Cadence:** edit the `cron` in `scrape.yml`. It is `7 * * * *` — on the 7th
  minute rather than `0`, because GitHub queues a huge share of the world's
  crons on the hour and off-peak minutes are less likely to be delayed.
  GitHub's minimum is 5 minutes; scheduled runs can still start late under load.
- **Horizon:** `workflow_dispatch` takes a `days` input (default 21).
- **Manual refresh:** *Run workflow* on `scrape` — visible within the CDN's
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

- Data is up to an hour old, plus scheduler and CDN slack (typically 5–15 min
  past the hour).
- Scraping runs from GitHub's datacenter IPs, where Bookeo and SimplyBook may
  throttle more aggressively than from a home connection; the stale cache keeps
  those rooms showing their last known slots and flags them `stale`.
- The repository must stay public.
- GitHub disables scheduled workflows after 60 days without repository activity;
  the scraper commits hourly, which keeps it active.
