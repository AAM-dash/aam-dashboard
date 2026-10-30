#!/usr/bin/env python3
"""
Refresh the Amazing Animal Minds KPI dashboard.

Reads raw data files from ./work/ (produced by the Claude refresh session via MCP
tools — see RUNBOOK.md), merges them into the encrypted payload of ../index.html,
and rewrites ../index.html with a freshly encrypted payload.

Env:  DASH_PASSWORD  (required) — the dashboard password used for AES-GCM encryption.

Inputs in ./work/ (all optional except sales.csv — missing files keep old data):
  sales.csv               ThriveCart sheet export (CSV)
  kit_growth.json         {"subscribers_total": int, "growth": {today|yesterday|d7|d30|d90:
                           {"new": int, "cancelled": int (positive), "net": int}}}
  broadcasts_raw.json     {"broadcasts": [raw Kit get_stats_for_a_list_of_broadcasts items]}
  meta_daily_raw.json     [raw Meta account-level daily entities]
  meta_campaigns30_raw.json / meta_campaigns90_raw.json  [raw campaign entities]
  meta_adsets_raw.json    [raw adset entities, last 90d]
  meta_ads_raw.json       [raw ad entities with adset_id/campaign_id/cost_per_result, last 90d]

Usage: cd tools && python3 refresh.py
"""
import base64, csv, hashlib, json, os, re, statistics, sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.exit("Run: pip install cryptography --break-system-packages")

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime.now(TZ)
TODAY = NOW.date()
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
PASSWORD = os.environ.get("DASH_PASSWORD") or sys.exit("DASH_PASSWORD env var required")

# ---------------- crypto ----------------
def decrypt_payload(html):
    enc = re.search(r'const ENC = "([^"]+)"', html).group(1)
    salt = re.search(r'const SALT = "([^"]+)"', html).group(1)
    raw = base64.b64decode(enc)
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), base64.b64decode(salt), 250000, dklen=32)
    pt = AESGCM(key).decrypt(raw[:12], raw[12:], None)
    return json.loads(pt)

def encrypt_payload(payload):
    data = json.dumps(payload, ensure_ascii=False).encode()
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 250000, dklen=32)
    ct = AESGCM(key).encrypt(iv, data, None)
    return base64.b64encode(iv + ct).decode(), base64.b64encode(salt).decode()

# ---------------- parse helpers for raw Meta strings ----------------
def sek(v):
    if v is None: return 0.0
    m = re.search(r'[\d,]+(?:\.\d+)?', str(v))
    return float(m.group(0).replace(",", "")) if m else 0.0

def num(v):
    return int(str(v).replace(",", "")) if v not in (None, "") else 0

def pct(v):
    m = re.search(r'[\d.]+', str(v)); return float(m.group(0)) if m else 0.0

def meta_date(s):  # "April 21, 2026" -> "2026-04-21"
    return datetime.strptime(s, "%B %d, %Y").strftime("%Y-%m-%d")

def load(name):
    p = os.path.join(WORK, name)
    if not os.path.exists(p): return None
    with open(p) as f:
        return json.load(f)

def load_entities(name):
    """Meta responses wrap entities as {"ad_entities": "<json string>"} or a plain list."""
    d = load(name)
    if d is None: return None
    if isinstance(d, dict) and "ad_entities" in d:
        d = d["ad_entities"]
    if isinstance(d, str):
        d = json.loads(d)
    return d

# ---------------- sales (full rebuild from CSV) ----------------
def product_group(p):
    s = p.lower()
    if 'verlatingsangst' in s or 'samen sterk' in s or 'implementatie' in s:
        return 'Verlatingsangst (NL)'
    if 'separation' in s: return 'Separation Anxiety (EN)'
    if 'vuurwerk' in s: return 'Vuurwerkangst (NL)'
    if 'geluidsangst' in s: return 'Geluidsangst (NL)'
    if 'noise' in s: return 'Noise Phobia (EN)'
    if 'bootcamp' in s: return 'Bootcamp (NL)'
    if 'happy dog' in s: return 'Happy Dog'
    if 'fearless' in s: return 'Fearless Dogs (EN)'
    if 'stress' in s: return 'Stress Management (NL)'
    if 'emotion' in s: return 'Emotions (EN)'
    if 'emotie' in s: return 'Emoties (NL)'
    return 'Other'

def parse_products(cell):
    m = re.match(r'Purchase of (.*) via ThriveCart', cell.strip())
    inner = m.group(1) if m else cell
    return [p.strip() for p in re.split(r',\s*&\s*', inner) if p.strip()]

def money(v):
    v = re.sub(r'[€£$]|kr|SEK|USD|GBP|EUR', '', v or '', flags=re.I).replace(',', '').strip()
    try: return float(v) if v else 0.0
    except ValueError: return 0.0

def _parse_sales_file(path, src):
    """Parse one payments sheet CSV. Handles both formats:
    - legacy Zapier sheet: Date '2025-09-18 11:47:32' (local), column 'Payment amount',
      Amount is the gross EUR charge ('€497.00').
    - Make.com sheet ('Thrivecart payments 2026'): Date ISO UTC '2026-08-21T13:25:31.000Z',
      column 'Payment amount in SEK', Amount is ex-VAT — gross lives in 'Total'.
    Returns (orders, skipped_bad_date)."""
    orders, bad_dates = [], 0
    with open(path) as f:
        reader = csv.DictReader(f)
        is_new = 'Payment amount in SEK' in (reader.fieldnames or [])
        for row in reader:
            email = (row.get('email') or '').strip().lower()
            if not row.get('Date') or not (row.get('Amount') or '').strip(): continue
            if '+test' in email: continue  # Else's own test purchases in the Make sheet
            raw_date = row['Date'].strip()
            try:
                if is_new:
                    dt = datetime.fromisoformat(raw_date.replace('Z', '+00:00')).astimezone(TZ).replace(tzinfo=None)
                else:
                    dt = datetime.strptime(raw_date, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                bad_dates += 1
                continue
            prods = parse_products(row['Product'])
            if prods and prods[0].lower() == 'test': continue  # exclude test purchases
            if is_new:
                amount = money(row.get('Total') or '') or money(row['Amount'])
                sek = money(row.get('Payment amount in SEK', ''))
            else:
                amount = money(row['Amount'])
                sek = money(row.get('Payment amount', ''))
            if amount <= 0 and sek <= 0: continue
            orders.append({
                'date': dt.strftime('%Y-%m-%d'), 'ts': dt.isoformat(), 'dt': dt, 'src': src,
                'name': (row.get('Name') or '').strip(), 'email': email,
                'country': (row.get('Country') or '').strip(),
                'currency': (row.get('Currency') or 'eur').strip().lower(),
                'amount': amount, 'sek': sek,
                'products': prods, 'group': product_group(prods[0]) if prods else 'Other',
                'refunded': (row.get('refunded') or '0').strip() not in ('0', '', 'none'),
            })
    return orders, bad_dates

def rebuild_sales(payload):
    p1 = os.path.join(WORK, "sales.csv")   # legacy Zapier sheet (history + rebills)
    p2 = os.path.join(WORK, "sales2.csv")  # Make.com sheet (Aug-Oct 2026)
    if not os.path.exists(p1) and not os.path.exists(p2): return None
    old_orders, old_bad = _parse_sales_file(p1, 'zapier') if os.path.exists(p1) else ([], 0)
    if os.path.exists(p2):
        new_orders, new_bad = _parse_sales_file(p2, 'make')
        # persist a compact copy inside the payload, so a later refresh that cannot
        # read the Make sheet (e.g. not shared with the service account) does not
        # silently lose these orders
        payload['makeOrders'] = [
            {k: o[k] for k in ('ts','name','email','country','currency','amount','sek','products','refunded')}
            for o in new_orders]
    else:
        new_bad = 0
        new_orders = []
        for m in payload.get('makeOrders', []):
            dt = datetime.fromisoformat(m['ts'])
            new_orders.append({**m, 'dt': dt, 'date': dt.strftime('%Y-%m-%d'), 'src': 'make-cached',
                               'group': product_group(m['products'][0]) if m['products'] else 'Other'})
        if new_orders:
            print(f"sales2.csv missing — reusing {len(new_orders)} cached Make orders from payload")
    if os.path.exists(p1) and len(old_orders) < 50:  # sanity guard: broken export must not wipe history
        print(f"WARNING: only {len(old_orders)} orders in legacy sheet — keeping previous sales data"); return None
    # De-duplicate: both pipelines can capture the same payment. Keep the Zapier copy;
    # drop a Make row when the same email has a Zapier payment of (nearly) the same
    # size within 2 days. Monthly installments are ~30 days apart, so they survive.
    by_email = defaultdict(list)
    for o in old_orders: by_email[o['email']].append(o)
    deduped, dropped = [], 0
    for n in new_orders:
        dup = False
        for o in by_email.get(n['email'], []):
            close_time = abs((o['dt'] - n['dt']).total_seconds()) <= 2 * 86400
            if not close_time: continue
            if n['sek'] > 0 and o['sek'] > 0:
                if abs(o['sek'] - n['sek']) <= 2: dup = True; break
            elif abs(o['amount'] - n['amount']) <= 0.5: dup = True; break
        if dup: dropped += 1
        else: deduped.append(n)
    orders = old_orders + deduped
    for o in orders: o.pop('dt', None)
    print(f"sales merge: {len(old_orders)} zapier + {len(new_orders)} make "
          f"({dropped} duplicates dropped, {old_bad + new_bad} rows skipped for bad dates)")
    orders.sort(key=lambda o: o['ts'])
    fx_rows = [o['sek']/o['amount'] for o in orders if o['currency'] == 'eur' and o['amount'] > 0 and o['sek'] > 0]
    fx = round(statistics.median(fx_rows[-40:]), 4) if fx_rows else payload.get('fx', {}).get('eurSek', 11.0)
    for o in orders:
        if o['currency'] != 'eur' and o['sek'] > 0:
            o['amount'] = round(o['sek']/fx, 2)
        o['sek_val'] = o['sek'] if o['sek'] > 0 else round(o['amount']*fx, 2)
    daily = defaultdict(lambda: {'rev':0.0,'orders':0,'refunds':0,'refund_amt':0.0,'byp':defaultdict(float)})
    for o in orders:
        d = daily[o['date']]
        if o['refunded']:
            d['refunds'] += 1; d['refund_amt'] += o['sek_val']
        else:
            d['rev'] += o['sek_val']; d['orders'] += 1; d['byp'][o['group']] += o['sek_val']
    out, d = [], date.fromisoformat(orders[0]['date'])
    while d <= TODAY:
        k = d.isoformat(); e = daily.get(k)
        out.append({'date': k, 'rev': round(e['rev'],2) if e else 0, 'orders': e['orders'] if e else 0,
                    'refunds': e['refunds'] if e else 0, 'refundAmt': round(e['refund_amt'],2) if e else 0,
                    'byProduct': {k2: round(v,2) for k2,v in e['byp'].items()} if e else {}})
        d += timedelta(days=1)
    recent = [{'date': o['date'], 'name': o['name'] or o['email'].split('@')[0],
               'product': ' + '.join(o['products'])[:80], 'group': o['group'],
               'amount': o['sek_val'], 'eur': o['amount'], 'country': o['country'], 'refunded': o['refunded']}
              for o in orders[-12:]][::-1]
    payload['sales'] = {'daily': out, 'recent': recent}
    payload['fx'] = {'eurSek': fx}
    return len(orders)

# ---------------- merge sections ----------------
def merge_kit(payload):
    kg = load("kit_growth.json")
    if not kg: return
    payload['kit'] = kg
    hist = {h['date']: h for h in payload.get('kitHistory', [])}
    hist[TODAY.isoformat()] = {'date': TODAY.isoformat(), 'subscribers': kg['subscribers_total'],
                               'newToday': kg['growth']['today']['new'],
                               'cancelledToday': kg['growth']['today']['cancelled']}
    payload['kitHistory'] = sorted(hist.values(), key=lambda h: h['date'])[-400:]

def merge_broadcasts(payload):
    raw = load("broadcasts_raw.json")
    if not raw: return
    items = raw.get('broadcasts', raw) if isinstance(raw, dict) else raw
    out = []
    for b in items:
        st = b.get('stats', {})
        if not b.get('send_at'): continue
        out.append({'subject': b.get('subject',''), 'date': b['send_at'][:10],
                    'recipients': st.get('recipients',0), 'open': st.get('open_rate',0),
                    'click': st.get('click_rate',0), 'unsubs': st.get('unsubscribes',0)})
    if out:
        out.sort(key=lambda b: b['date'], reverse=True)
        payload['broadcasts'] = out[:80]

def merge_meta_daily(payload):
    ents = load_entities("meta_daily_raw.json")
    if ents is None: return
    merged = {e['d']: e for e in payload['meta']['daily']}
    fresh_dates = set()
    for e in ents:
        d = meta_date(e['date_start'])
        merged[d] = {'d': d, 'spend': sek(e.get('amount_spent')), 'imp': num(e.get('impressions')), 'clicks': num(e.get('clicks'))}
        fresh_dates.add(d)
    # zero-fill covered window days Meta omitted (no delivery), so old ghost values can't linger
    if fresh_dates:
        lo = min(fresh_dates)
        d = date.fromisoformat(lo)
        while d <= TODAY:
            k = d.isoformat()
            if k not in fresh_dates and k in merged and k >= lo:
                merged[k] = {'d': k, 'spend': 0, 'imp': 0, 'clicks': 0}
            d += timedelta(days=1)
    payload['meta']['daily'] = sorted(merged.values(), key=lambda e: e['d'])

def slim_campaigns(ents):
    out = []
    for c in ents:
        if 'amount_spent' not in c: continue
        out.append({'name': c.get('name',''), 'status': c.get('status',''),
                    'spend': sek(c['amount_spent']), 'imp': num(c.get('impressions')),
                    'clicks': num(c.get('clicks')), 'ctr': pct(c.get('ctr')),
                    'cpc': sek(c.get('cpc')), 'reach': num(c.get('reach'))})
    return sorted(out, key=lambda c: -c['spend'])

def merge_campaigns(payload):
    for key, fname in [('campaigns30','meta_campaigns30_raw.json'), ('campaigns90','meta_campaigns90_raw.json')]:
        ents = load_entities(fname)
        if ents is not None:
            payload['meta'][key] = slim_campaigns(ents)

def merge_adtree(payload):
    adsets = load_entities("meta_adsets_raw.json")
    ads = load_entities("meta_ads_raw.json")
    camps = load_entities("meta_campaigns90_raw.json")
    if not (adsets and ads and camps): return
    cmap = {c['id']: (c.get('name',''), c.get('status','')) for c in camps}
    smap = {s['id']: (s.get('name','Ad set'), s.get('status','')) for s in adsets}
    tree = {}
    for a in ads:
        cid, sid = a.get('campaign_id'), a.get('adset_id')
        if not cid or not sid: continue
        spend, clicks, cpc = sek(a.get('amount_spent')), num(a.get('clicks')), sek(a.get('cpc'))
        if spend > 0 and clicks*cpc > spend*5: spend = round(clicks*cpc, 2)  # API glitch guard
        cpr_raw = (a.get('cost_per_result') or {}).get('value', '') if isinstance(a.get('cost_per_result'), dict) else str(a.get('cost_per_result') or '')
        cpr = sek(cpr_raw) or None
        lbl = (re.search(r'\(([^)]+)\)', cpr_raw or '') or [None]);  lbl = lbl.group(1) if hasattr(lbl,'group') else ''
        lbl = 'Stress guide' if 'stress' in (lbl or '').lower() else ('Registration' if lbl else '')
        results = round(spend/cpr, 1) if cpr else None
        ad = {'name': a.get('name',''), 'status': a.get('status',''), 'spend': spend,
              'imp': num(a.get('impressions')), 'clicks': clicks, 'ctr': pct(a.get('ctr')),
              'cpc': cpc, 'cpr': cpr, 'results': results, 'rlabel': lbl or 'Result'}
        c = tree.setdefault(cid, {'id': cid, 'name': cmap.get(cid, ('Campaign',''))[0],
                                  'status': cmap.get(cid, ('',''))[1], 'spend': 0, 'results': 0, 'adsets': {}})
        s = c['adsets'].setdefault(sid, {'id': sid, 'name': smap.get(sid, ('Ad set',''))[0],
                                         'status': smap.get(sid, ('',''))[1], 'spend': 0, 'imp': 0,
                                         'clicks': 0, 'results': 0, 'ads': []})
        s['spend'] += spend; s['imp'] += ad['imp']; s['clicks'] += clicks; s['results'] += results or 0
        s['ads'].append(ad); c['spend'] += spend; c['results'] += results or 0
    out = []
    for c in sorted(tree.values(), key=lambda x: -x['spend']):
        if c['spend'] <= 0: continue
        alist = []
        for s in sorted(c['adsets'].values(), key=lambda x: -x['spend']):
            s['spend'] = round(s['spend'],2)
            s['ctr'] = round(s['clicks']/s['imp']*100, 2) if s['imp'] else 0
            s['cpc'] = round(s['spend']/s['clicks'], 2) if s['clicks'] else None
            s['cpr'] = round(s['spend']/s['results'], 2) if s['results'] else None
            s['results'] = round(s['results'],1); s['ads'].sort(key=lambda x: -x['spend'])
            alist.append(s)
        c['adsets'] = alist; c['spend'] = round(c['spend'],2); c['results'] = round(c['results'],1)
        c['cpr'] = round(c['spend']/c['results'],2) if c['results'] else None
        c['rlabel'] = alist[0]['ads'][0]['rlabel'] if alist and alist[0]['ads'] else ''
        out.append(c)
    if out: payload['meta']['adTree'] = out

# ---------------- main ----------------
def main():
    html = open(HTML_PATH, encoding='utf-8').read()
    payload = decrypt_payload(html)
    n = rebuild_sales(payload)
    merge_kit(payload)
    merge_broadcasts(payload)
    merge_meta_daily(payload)
    merge_campaigns(payload)
    merge_adtree(payload)
    payload['generatedAt'] = NOW.isoformat(timespec='seconds')
    enc, salt = encrypt_payload(payload)
    html = re.sub(r'const ENC = "[^"]+"', f'const ENC = "{enc}"', html)
    html = re.sub(r'const SALT = "[^"]+"', f'const SALT = "{salt}"', html)
    open(HTML_PATH, 'w', encoding='utf-8').write(html)
    print(f"OK refreshed {NOW.isoformat(timespec='seconds')} | orders: {n} | "
          f"meta days: {len(payload['meta']['daily'])} | broadcasts: {len(payload['broadcasts'])} | "
          f"payload sections updated")

if __name__ == '__main__':
    main()
