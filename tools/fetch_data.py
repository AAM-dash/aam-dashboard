#!/usr/bin/env python3
"""
Fetch raw data for the AAM dashboard refresh, direct from Kit / Meta / Google APIs.
Runs standalone in GitHub Actions -- no Claude/MCP dependency.

Writes the same file names ./work/refresh.py already expects, so refresh.py itself
is untouched.

Env vars:
  KIT_API_KEY                  Kit v4 API key (Settings -> Advanced -> API)
  META_ACCESS_TOKEN            Meta Marketing API access token, ads_read on the account
  META_AD_ACCOUNT_ID           numeric account id, no "act_" prefix
  GOOGLE_SERVICE_ACCOUNT_JSON  full JSON key content for a service account with
                                Viewer access to the sales Google Sheet
  SALES_SHEET_ID                Google Sheet file id for the ThriveCart sales export

Any missing var -> that source is skipped (refresh.py keeps previous data for it).
"""
import os, re, sys, json, requests
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")
TODAY = datetime.now(TZ).date()
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.makedirs(WORK, exist_ok=True)


def save_json(name, obj):
    with open(os.path.join(WORK, name), "w", encoding="utf-8") as f:
        json.dump(obj, f)
    print(f"wrote {name}")


def save_text(name, text):
    with open(os.path.join(WORK, name), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {name}")


# ---------------- Kit ----------------
KIT_KEY = os.environ.get("KIT_API_KEY")


def kit_get(path, params=None):
    r = requests.get(
        f"https://api.kit.com/v4{path}",
        headers={"X-Kit-Api-Key": KIT_KEY},
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_kit_growth():
    if not KIT_KEY:
        print("skip kit_growth.json: no KIT_API_KEY")
        return
    windows = {
        "today": (TODAY, TODAY),
        "yesterday": (TODAY - timedelta(days=1), TODAY - timedelta(days=1)),
        "d7": (TODAY - timedelta(days=6), TODAY),
        "d30": (TODAY - timedelta(days=29), TODAY),
        "d90": (TODAY - timedelta(days=89), TODAY),
    }
    growth, subs_total = {}, None
    for key, (start, end) in windows.items():
        data = kit_get(
            "/account/growth_stats",
            {"starting": start.isoformat(), "ending": end.isoformat()},
        )
        s = data["stats"]
        growth[key] = {
            "new": s["new_subscribers"],
            "cancelled": -s["cancellations"],
            "net": s["net_new_subscribers"],
        }
        if key == "d90":
            subs_total = s["subscribers"]
    save_json("kit_growth.json", {"subscribers_total": subs_total, "growth": growth})


def fetch_kit_broadcasts():
    if not KIT_KEY:
        print("skip broadcasts_raw.json: no KIT_API_KEY")
        return
    sent_after = (TODAY - timedelta(days=90)).isoformat()
    all_broadcasts, after, page = [], None, 0
    while page < 3:
        params = {"sent_after": sent_after, "status": "completed", "per_page": 50}
        if after:
            params["after"] = after
        data = kit_get("/broadcasts/stats", params)
        all_broadcasts.extend(data.get("broadcasts", []))
        pg = data.get("pagination", {})
        if not pg.get("has_next_page"):
            break
        after = pg.get("end_cursor")
        page += 1
    save_json("broadcasts_raw.json", {"broadcasts": all_broadcasts})


# ---------------- Google Sheets (service account) ----------------
def fetch_sales_csv():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("SALES_SHEET_ID")
    if not sa_json or not sheet_id:
        print("skip sales.csv: missing GOOGLE_SERVICE_ACCOUNT_JSON or SALES_SHEET_ID")
        return
    from google.oauth2 import service_account
    import google.auth.transport.requests

    info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    r = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{sheet_id}/export",
        params={"mimeType": "text/csv"},
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=60,
    )
    r.raise_for_status()
    save_text("sales.csv", r.text)


# ---------------- Meta ----------------
META_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_ACCOUNT = os.environ.get("META_AD_ACCOUNT_ID")


def meta_paginate(path, params, limit=500):
    out, url, p = [], f"https://graph.facebook.com/v21.0/{path}", dict(params, access_token=META_TOKEN, limit=limit)
    while url and len(out) < 5000:
        r = requests.get(url, params=p, timeout=60)
        if r.status_code != 200:
            print(f"META WARN {path}: {r.status_code} {r.text[:300]}")
            break
        data = r.json()
        out.extend(data.get("data", []))
        nxt = data.get("paging", {}).get("next")
        url, p = nxt, None
    return out


def meta_entity_maps():
    """id -> (name, status) for campaigns/adsets/ads, used to enrich insights rows."""
    camps = meta_paginate(f"act_{META_ACCOUNT}/campaigns", {"fields": "id,name,effective_status"})
    adsets = meta_paginate(f"act_{META_ACCOUNT}/adsets", {"fields": "id,name,effective_status,campaign_id"})
    ads = meta_paginate(
        f"act_{META_ACCOUNT}/ads",
        {"fields": "id,name,effective_status,adset_id,campaign_id"},
    )
    return (
        {c["id"]: (c.get("name", ""), c.get("effective_status", "")) for c in camps},
        {a["id"]: (a.get("name", ""), a.get("effective_status", "")) for a in adsets},
        {a["id"]: (a.get("name", ""), a.get("effective_status", "")) for a in ads},
    )


def fmt_meta_date(iso_date):
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%B %d, %Y")


def fetch_meta_daily():
    if not (META_TOKEN and META_ACCOUNT):
        print("skip meta_daily_raw.json: no META_ACCESS_TOKEN/META_AD_ACCOUNT_ID")
        return
    since = (TODAY - timedelta(days=40)).isoformat()
    rows = meta_paginate(
        f"act_{META_ACCOUNT}/insights",
        {
            "level": "account",
            "time_range": json.dumps({"since": since, "until": TODAY.isoformat()}),
            "time_increment": "1",
            "fields": "spend,impressions,clicks,date_start",
        },
    )
    out = [
        {
            "date_start": fmt_meta_date(r["date_start"]),
            "amount_spent": r.get("spend", "0"),
            "impressions": r.get("impressions", "0"),
            "clicks": r.get("clicks", "0"),
        }
        for r in rows
    ]
    save_json("meta_daily_raw.json", out)


def fetch_meta_campaigns(preset, fname, cmap):
    if not (META_TOKEN and META_ACCOUNT):
        print(f"skip {fname}: no META_ACCESS_TOKEN/META_AD_ACCOUNT_ID")
        return
    rows = meta_paginate(
        f"act_{META_ACCOUNT}/insights",
        {
            "level": "campaign",
            "date_preset": preset,
            "fields": "campaign_id,spend,impressions,clicks,ctr,cpc,reach",
        },
    )
    out = []
    for r in rows:
        cid = r.get("campaign_id")
        name, status = cmap.get(cid, ("", ""))
        out.append(
            {
                "id": cid,
                "name": name,
                "status": status,
                "amount_spent": r.get("spend", "0"),
                "impressions": r.get("impressions", "0"),
                "clicks": r.get("clicks", "0"),
                "ctr": r.get("ctr", "0"),
                "cpc": r.get("cpc", "0"),
                "reach": r.get("reach", "0"),
            }
        )
    save_json(fname, out)


def fetch_meta_adtree(smap):
    """Adsets + ads, last 90d. Spend/impressions/clicks/ctr/cpc are reliable.
    results/cost_per_result are left blank here -- Meta's "Results" depends on
    each ad's chosen conversion event, which needs to be mapped per-adset from
    real account data. refresh.py tolerates missing results (shows spend/CTR
    only until that mapping is added)."""
    adset_rows = meta_paginate(
        f"act_{META_ACCOUNT}/insights",
        {
            "level": "adset",
            "date_preset": "last_90d",
            "fields": "adset_id,spend,impressions,clicks,ctr,cpc,reach,frequency",
            "sort": "spend_descending",
        },
    )
    adsets_out = []
    for r in adset_rows:
        sid = r.get("adset_id")
        name, status = smap.get(sid, ("Ad set", ""))
        adsets_out.append(
            {
                "id": sid,
                "name": name,
                "status": status,
                "amount_spent": r.get("spend", "0"),
                "impressions": r.get("impressions", "0"),
                "clicks": r.get("clicks", "0"),
                "ctr": r.get("ctr", "0"),
                "cpc": r.get("cpc", "0"),
                "reach": r.get("reach", "0"),
                "frequency": r.get("frequency", "0"),
            }
        )
    save_json("meta_adsets_raw.json", adsets_out)

    ad_rows = meta_paginate(
        f"act_{META_ACCOUNT}/insights",
        {
            "level": "ad",
            "date_preset": "last_90d",
            "fields": "ad_id,ad_name,adset_id,campaign_id,spend,impressions,clicks,ctr,cpc",
            "sort": "spend_descending",
        },
    )
    ads_out = []
    for r in ad_rows:
        ads_out.append(
            {
                "id": r.get("ad_id"),
                "name": r.get("ad_name", ""),
                "status": "",
                "adset_id": r.get("adset_id"),
                "campaign_id": r.get("campaign_id"),
                "amount_spent": r.get("spend", "0"),
                "impressions": r.get("impressions", "0"),
                "clicks": r.get("clicks", "0"),
                "ctr": r.get("ctr", "0"),
                "cpc": r.get("cpc", "0"),
                "results": None,
                "cost_per_result": None,
            }
        )
    save_json("meta_ads_raw.json", ads_out)


def main():
    fetch_kit_growth()
    fetch_kit_broadcasts()
    fetch_sales_csv()

    if META_TOKEN and META_ACCOUNT:
        cmap, smap, _ = meta_entity_maps()
        fetch_meta_daily()
        fetch_meta_campaigns("last_30d", "meta_campaigns30_raw.json", cmap)
        fetch_meta_campaigns("last_90d", "meta_campaigns90_raw.json", cmap)
        fetch_meta_adtree(smap)
    else:
        print("skip all meta_*_raw.json: no META_ACCESS_TOKEN/META_AD_ACCOUNT_ID")

    print("fetch_data.py done")


if __name__ == "__main__":
    main()
