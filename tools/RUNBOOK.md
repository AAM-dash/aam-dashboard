# Hourly refresh runbook — Amazing Animal Minds KPI dashboard

You are a scheduled Claude session refreshing an encrypted KPI dashboard hosted on
GitHub Pages. Follow these steps exactly. The GitHub token (GHT) and dashboard
password (DASH_PASSWORD) are provided in your task instructions — never print them,
never include them in commit messages, never write them to any file inside the repo.

## 1. Clone

```bash
git clone --depth 1 https://x-access-token:${GHT}@github.com/OWNER/REPO.git aamdash
mkdir -p aamdash/tools/work
pip list 2>/dev/null | grep -qi cryptography || pip install cryptography --break-system-packages -q
```

(OWNER/REPO are given in your task instructions.)

## 2. Gather raw data via MCP tools → save under aamdash/tools/work/

Save each result as the exact file named below. Meta tool responses have shape
`{"ad_entities": "<json-string>"}` — save the whole response object as-is; the
refresh script handles both wrapped and unwrapped forms.

1. **sales.csv** — Google Drive tool `download_file_content` with
   `fileId: 1dK0T2KJJ5JOPXidnCfuWEhg8kWmOuViezPdCwCD99xk`, `exportMimeType: text/csv`.
   The response `content` field is base64 — decode it and write the CSV text to
   `work/sales.csv`. If the response is too large and is saved to a tool-results
   file on disk, decode from that file instead (it is JSON with a `content` key).

2. **kit_growth.json** — Kit tool `get_growth_stats`, 5 calls with
   (starting, ending) in Europe/Stockholm dates:
   today→(T,T), yesterday→(T-1,T-1), d7→(T-6,T), d30→(T-29,T), d90→(T-89,T).
   Kit returns `cancellations` as a negative number and `net_new_subscribers`
   accordingly; store positives for `cancelled` (= -cancellations). Build:
   ```json
   {"subscribers_total": <stats.subscribers from the d90 call>,
    "growth": {"today": {"new":0,"cancelled":1,"net":-1}, "yesterday": {...},
               "d7": {...}, "d30": {...}, "d90": {...}}}
   ```

3. **broadcasts_raw.json** — Kit tool `get_stats_for_a_list_of_broadcasts` with
   `sent_after` = (T-90 days), `status: completed`, `per_page: 50`. If
   `pagination.has_next_page`, fetch next pages (max 3) with `after` cursor and
   concatenate all `broadcasts` arrays into one object: `{"broadcasts":[ ...all... ]}`.

4. **meta_daily_raw.json** — Meta tool `ads_get_ad_entities`:
   `ad_account_id: 263286035984952`, `level: account`,
   `time_range: {"since": <T-40 days>, "until": <T>}`, `time_increment: "1"`,
   `fields: ["id","spend","impressions","clicks"]`.

5. **meta_campaigns30_raw.json** — same tool: `level: campaign`, `date_preset: last_30d`,
   `fields: ["id","name","status","spend","impressions","clicks","ctr","cpc","reach"]`.

6. **meta_campaigns90_raw.json** — same as 5 but `date_preset: last_90d`.

7. **meta_adsets_raw.json** — same tool: `level: adset`, `date_preset: last_90d`,
   `sort: spend_descending`, `limit: 30`, same fields as 5 plus `frequency`.

8. **meta_ads_raw.json** — same tool: `level: ad`, `date_preset: last_90d`,
   `sort: spend_descending`, `limit: 60`,
   `fields: ["id","name","status","adset_id","campaign_id","spend","impressions","clicks","ctr","cpc","results","cost_per_result"]`.

If a single source fails after 2 retries, continue anyway — the refresh script
keeps the previous data for any missing file. Only abort entirely if BOTH the
sales sheet AND all Meta/Kit calls fail.

## 3. Rebuild and publish

```bash
cd aamdash/tools
DASH_PASSWORD='<from task instructions>' python3 refresh.py
cd ..
git config user.email "dashboard-bot@users.noreply.github.com"
git config user.name "Dashboard refresh"
git add index.html
git commit -m "data refresh" && git push
```

If `refresh.py` errors, do NOT push. Nothing else in the repo should ever be
modified by a refresh run.

## 4. Verify (best effort)

`git log -1` should show your commit. GitHub Pages redeploys automatically within
~1 minute of the push. Done — end the session with a one-line summary (what was
refreshed, any sources skipped).
