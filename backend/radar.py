#!/usr/bin/env python3
"""
Shopify Radar - নতুন Shopify store detect + enrich করার ছোট pipeline.

Commands:
  python radar.py discover                 # Certificate Transparency stream থেকে নতুন domain ধরে
  python radar.py seed domains.txt         # নিজের domain list দিয়ে শুরু করো
  python radar.py enrich [--loop 600]      # pending domain verify + data extract
  python radar.py export --days 7 -o last7.csv
  python radar.py report --date 2026-11-09      # ওই তারিখের launch + country হিসাব + CSV
  python radar.py web                           # browser UI: http://127.0.0.1:8000
  python radar.py stats
"""
import argparse
import asyncio
import csv
import datetime as dt
import html as htmllib
import http.server
import io
import ipaddress
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import dns.asyncresolver
import httpx
import phonenumbers
import tldextract
import websockets
from bs4 import BeautifulSoup

# ----------------------------------------------------------------- config
DB_PATH = os.environ.get("RADAR_DB", "radar.db")
# certstream-server-rust (self-hosted) এর "domains-only" endpoint
CERTSTREAM_URL = os.environ.get("CERTSTREAM_URL", "ws://127.0.0.1:8080/domains-only")
# শুধু prefilter: Shopify এর পরিচিত IP range. আসল verify হয় HTTP fingerprint দিয়ে।
SHOPIFY_NETS = [ipaddress.ip_network("23.227.38.0/24")]
SHOPIFY_CNAME_SUFFIX = ("myshopify.com",)
CONTACT_EMAIL = os.environ.get("RADAR_CONTACT", "you@example.com")
UA = f"Mozilla/5.0 (compatible; ShopifyRadar/1.0; +mailto:{CONTACT_EMAIL})"
HTTP_TIMEOUT = 15
ENRICH_CONCURRENCY = 20
DNS_CONCURRENCY = 200
QUEUE_MAX = 50_000

_tld = tldextract.TLDExtract(suffix_list_urls=())  # bundled snapshot, network লাগে না

# ----------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    domain          TEXT PRIMARY KEY,
    first_seen      TEXT,              -- আমাদের pipeline যেদিন প্রথম ধরেছে (YYYY-MM-DD)
    cert_not_before TEXT,              -- CT log এ certificate issue date (থাকলে)
    source          TEXT,
    status          TEXT DEFAULT 'pending',  -- pending|shopify|not_shopify|unreachable
    country         TEXT,
    country_source  TEXT,
    email           TEXT,
    phone           TEXT,
    instagram       TEXT,
    facebook        TEXT,
    linkedin        TEXT,
    twitter         TEXT,
    tiktok          TEXT,
    youtube         TEXT,
    pinterest       TEXT,
    theme           TEXT,
    theme_version   TEXT,
    theme_store_id  INTEGER,
    last_checked    TEXT,
    attempts        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_status ON stores(status);
CREATE INDEX IF NOT EXISTS idx_first_seen ON stores(first_seen);
CREATE INDEX IF NOT EXISTS idx_cert ON stores(cert_not_before);
"""


NEW_COLS = ("twitter", "tiktok", "youtube", "pinterest")


def db_connect(path=None):
    con = sqlite3.connect(path or DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # discover/enrich/web একসাথে চললেও lock কম
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(stores)")}
    for col in NEW_COLS:  # পুরনো DB migrate
        if col not in have:
            con.execute(f"ALTER TABLE stores ADD COLUMN {col} TEXT")
    return con


def today():
    return dt.date.today().isoformat()


def now_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add_candidate(con, domain, source, first_seen=None, cert_not_before=None):
    cur = con.execute(
        "INSERT OR IGNORE INTO stores(domain, first_seen, cert_not_before, source) VALUES (?,?,?,?)",
        (domain, first_seen or today(), cert_not_before, source),
    )
    con.commit()
    return cur.rowcount == 1


# ----------------------------------------------------------------- discovery
def normalize_ct_name(name):
    """CT log এর hostname থেকে registrable domain বের করে; subdomain/wildcard বাদ।"""
    name = name.strip().lower().rstrip(".")
    if not name or name.startswith("*.") or " " in name:
        return None
    ext = _tld(name)
    if not ext.domain or not ext.suffix:
        return None
    if ext.subdomain not in ("", "www"):
        return None
    if ext.registered_domain.endswith("myshopify.com"):
        return None
    return ext.registered_domain


async def points_to_shopify(resolver, domain):
    for host in (domain, "www." + domain):
        try:
            ans = await resolver.resolve(host, "A")
        except Exception:
            continue
        if str(ans.canonical_name).rstrip(".").endswith(SHOPIFY_CNAME_SUFFIX):
            return True
        for r in ans:
            try:
                ip = ipaddress.ip_address(r.address)
            except ValueError:
                continue
            if any(ip in net for net in SHOPIFY_NETS):
                return True
    return False


async def discover(args):
    con = db_connect()
    q = asyncio.Queue(maxsize=QUEUE_MAX)
    seen = set()
    stats = {"names": 0, "dropped": 0, "dns_hits": 0, "new": 0}

    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = ["1.1.1.1", "8.8.8.8"]
    resolver.lifetime = 3

    def handle_names(names, not_before):
        for raw in names:
            d = normalize_ct_name(raw)
            if not d or d in seen:
                continue
            if len(seen) > 2_000_000:
                seen.clear()
            seen.add(d)
            stats["names"] += 1
            try:
                q.put_nowait((d, not_before))
            except asyncio.QueueFull:
                stats["dropped"] += 1

    async def reader():
        async for ws in websockets.connect(args.url, max_size=None):
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    mtype = msg.get("message_type")
                    if mtype == "dns_entries":  # domains-only endpoint
                        handle_names(msg.get("data", []), None)
                    elif mtype == "certificate_update":  # full certstream format
                        leaf = msg["data"]["leaf_cert"]
                        nb = leaf.get("not_before")
                        nb = dt.date.fromtimestamp(nb).isoformat() if nb else None
                        handle_names(leaf.get("all_domains", []), nb)
            except websockets.ConnectionClosed:
                print("[discover] connection closed, reconnecting...", file=sys.stderr)
                continue

    async def worker():
        while True:
            domain, nb = await q.get()
            try:
                if await points_to_shopify(resolver, domain):
                    stats["dns_hits"] += 1
                    if add_candidate(con, domain, "certstream", cert_not_before=nb):
                        stats["new"] += 1
            finally:
                q.task_done()

    async def reporter():
        while True:
            await asyncio.sleep(30)
            print(f"[discover] {time.strftime('%H:%M:%S')} {stats} queue={q.qsize()}", flush=True)

    print(f"[discover] connecting to {args.url}")
    await asyncio.gather(reader(), reporter(), *[worker() for _ in range(DNS_CONCURRENCY)])


def seed(args):
    con = db_connect()
    n = 0
    with open(args.file, encoding="utf-8") as f:
        for line in f:
            d = line.strip().lower()
            if not d or d.startswith("#"):
                continue
            d = re.sub(r"^https?://", "", d).split("/")[0]
            d = d.removeprefix("www.")
            if add_candidate(con, d, "seed"):
                n += 1
    print(f"[seed] {n} new domains added")


# ----------------------------------------------------------------- extraction (pure functions)
EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}", re.I)
JUNK_EMAIL_PARTS = (
    "example.", "sentry", "wixpress", "domain.com", "email.com", "yourdomain",
    "your@", "name@", "user@", "shopify.com", "noreply", "no-reply", "@2x",
)
JUNK_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")

IG_BAD = {"p", "reel", "reels", "explore", "accounts", "share", "sharer", "about", "stories", "tv"}
FB_BAD = {"sharer", "sharer.php", "share", "share.php", "tr", "plugins", "dialog", "l.php",
          "login", "policies", "privacy", "help", "watch", "events", "hashtag"}
X_BAD = {"intent", "share", "home", "i", "hashtag", "search", "login", "privacy", "tos"}
PIN_BAD = {"pin", "pinterest", "createpin", "login", "business"}
GENERIC_CCTLD = {"io", "ai", "co", "tv", "me", "cc", "ws", "fm", "ly", "gg", "to", "sh",
                 "ac", "gl", "la", "vc", "im", "so", "eu", "su"}
THEME_RE = re.compile(r"Shopify\.theme\s*=\s*(\{.*?\})\s*;", re.S)
SHOPIFY_COUNTRY_RE = re.compile(r"Shopify\.country\s*=\s*\"([A-Z]{2})\"")


def decode_cfemail(hexstr):
    try:
        b = bytes.fromhex(hexstr)
        return bytes(c ^ b[0] for c in b[1:]).decode()
    except Exception:
        return None


def looks_like_shopify(html, headers):
    h = {k.lower() for k in headers.keys()}
    if "x-shopid" in h or "x-shopify-stage" in h:
        return True
    chunk = html[:400_000]
    return any(s in chunk for s in ("cdn.shopify.com", "Shopify.theme", "myshopify.com", "shopify-section"))


def jsonld_items(soup):
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                out.append(x)
                stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    return out


def clean_emails(candidates, domain):
    seen, good = set(), []
    for e in candidates:
        e = unquote(e).strip().strip(".,;:<>()[]\"'").lower()
        if e in seen or not EMAIL_RE.fullmatch(e):
            continue
        seen.add(e)
        if any(p in e for p in JUNK_EMAIL_PARTS) or e.endswith(JUNK_EMAIL_SUFFIX):
            continue
        good.append(e)
    base = _tld(domain).registered_domain or domain
    good.sort(key=lambda e: 0 if e.split("@")[1].endswith(base) else 1)  # store এর নিজের domain আগে
    return good


def extract_emails(soup, items, domain):
    found = []
    for a in soup.select('a[href^="mailto:"]'):
        found.append(a["href"][7:].split("?")[0])
    for el in soup.select("[data-cfemail]"):
        d = decode_cfemail(el.get("data-cfemail", ""))
        if d:
            found.append(d)
    for it in items:
        e = it.get("email")
        if isinstance(e, str):
            found.append(e.replace("mailto:", ""))
    found += EMAIL_RE.findall(soup.get_text(" "))
    return clean_emails(found, domain)


def extract_phone(soup, items):
    cands = [unquote(a["href"][4:]) for a in soup.select('a[href^="tel:"]')]
    cands += [it["telephone"] for it in items if isinstance(it.get("telephone"), str)]
    for c in cands:
        digits = re.sub(r"\D", "", c)
        if 7 <= len(digits) <= 15:
            return ("+" if c.strip().startswith("+") else "") + digits
    return None


def _host_is(host, d):
    return host == d or host.endswith("." + d)


def social_from_url(url):
    try:
        u = urlparse(url if "//" in url else "//" + url)
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.")
    segs = [s for s in u.path.split("/") if s]
    if _host_is(host, "instagram.com") and segs and segs[0].lower() not in IG_BAD:
        return "instagram", f"https://www.instagram.com/{segs[0]}/"
    if (_host_is(host, "facebook.com") or host == "fb.com") and segs:
        if segs[0] == "profile.php" and "id=" in (u.query or ""):
            return "facebook", f"https://www.facebook.com/profile.php?{u.query.split('&')[0]}"
        if segs[0].lower() not in FB_BAD:
            return "facebook", f"https://www.facebook.com/{segs[0]}"
    if _host_is(host, "linkedin.com") and len(segs) >= 2 and segs[0] in ("company", "in", "school"):
        return "linkedin", f"https://www.linkedin.com/{segs[0]}/{segs[1]}"
    if (_host_is(host, "twitter.com") or _host_is(host, "x.com")) and segs and segs[0].lower() not in X_BAD:
        return "twitter", f"https://x.com/{segs[0]}"
    if _host_is(host, "tiktok.com") and segs and segs[0].startswith("@"):
        return "tiktok", f"https://www.tiktok.com/{segs[0]}"
    if _host_is(host, "youtube.com") and segs:
        if segs[0].startswith("@"):
            return "youtube", f"https://www.youtube.com/{segs[0]}"
        if segs[0] in ("channel", "c", "user") and len(segs) >= 2:
            return "youtube", f"https://www.youtube.com/{segs[0]}/{segs[1]}"
    if _host_is(host, "pinterest.com") and segs and segs[0].lower() not in PIN_BAD:
        return "pinterest", f"https://www.pinterest.com/{segs[0]}/"
    return None


def extract_socials(soup, items):
    urls = [a["href"] for a in soup.find_all("a", href=True)]
    for it in items:
        sa = it.get("sameAs")
        urls += [sa] if isinstance(sa, str) else (sa if isinstance(sa, list) else [])
    out = {}
    for u in urls:
        if not isinstance(u, str):
            continue
        r = social_from_url(u)
        if r and r[0] not in out:
            out[r[0]] = r[1]
    return out


def extract_theme(html):
    m = THEME_RE.search(html)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
    except Exception:
        d = {}
        n = re.search(r'"schema_name"\s*:\s*"([^"]+)"', m.group(1)) or re.search(r'"name"\s*:\s*"([^"]+)"', m.group(1))
        if n:
            d["schema_name"] = n.group(1)
    return {
        "theme": d.get("schema_name") or d.get("name"),
        "theme_version": d.get("schema_version"),
        "theme_store_id": d.get("theme_store_id"),
    }


def country_from_tld(domain):
    suffix = _tld(domain).suffix
    last = suffix.split(".")[-1] if suffix else ""
    if len(last) != 2 or last in GENERIC_CCTLD:
        return None
    return "GB" if last == "uk" else last.upper()


def country_from_phone(phone):
    if not phone or not phone.startswith("+"):
        return None
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(phone, None))
    except Exception:
        return None


def parse_page(html, domain):
    soup = BeautifulSoup(html, "lxml")
    items = jsonld_items(soup)
    m = SHOPIFY_COUNTRY_RE.search(html)
    return {
        "emails": extract_emails(soup, items, domain),
        "phone": extract_phone(soup, items),
        "socials": extract_socials(soup, items),
        "theme": extract_theme(html),
        "shopify_country": m.group(1) if m else None,
    }


# ----------------------------------------------------------------- enrichment
async def fetch(client, url):
    try:
        return await client.get(url)
    except httpx.HTTPError:
        return None


async def enrich_one(client, domain):
    r = None
    for scheme in ("https", "http"):
        r = await fetch(client, f"{scheme}://{domain}/")
        if r is not None:
            break
    if r is None:
        return {"status": "unreachable"}

    html = r.text
    if not looks_like_shopify(html, r.headers):
        if r.status_code in (403, 429, 503):  # bot-block হতে পারে, পরে আবার চেষ্টা
            return {"status": "unreachable"}
        return {"status": "not_shopify"}

    data = parse_page(html, domain)

    # contact / about page থেকে অতিরিক্ত email, phone, social
    soup = BeautifulSoup(html, "lxml")
    base, links = str(r.url), []
    for a in soup.find_all("a", href=True):
        if re.search(r"contact|about", a["href"], re.I):
            full = urljoin(base, a["href"]).split("#")[0]
            if urlparse(full).hostname == urlparse(base).hostname and full not in links and full != base:
                links.append(full)
    for link in links[:2]:
        r2 = await fetch(client, link)
        if r2 is None or r2.status_code >= 400:
            continue
        extra = parse_page(r2.text, domain)
        data["emails"] += [e for e in extra["emails"] if e not in data["emails"]]
        data["phone"] = data["phone"] or extra["phone"]
        for k, v in extra["socials"].items():
            data["socials"].setdefault(k, v)

    # country: ccTLD > phone prefix > Shopify.country (weak: visitor-based হতে পারে)
    country, source = country_from_tld(domain), "tld"
    if not country:
        country, source = country_from_phone(data["phone"]), "phone"
    if not country and data["shopify_country"]:
        country, source = data["shopify_country"], "shopify_js"
    if not country:
        source = None

    return {
        "status": "shopify",
        "country": country,
        "country_source": source,
        "email": data["emails"][0] if data["emails"] else None,
        "phone": data["phone"],
        "instagram": data["socials"].get("instagram"),
        "facebook": data["socials"].get("facebook"),
        "linkedin": data["socials"].get("linkedin"),
        "twitter": data["socials"].get("twitter"),
        "tiktok": data["socials"].get("tiktok"),
        "youtube": data["socials"].get("youtube"),
        "pinterest": data["socials"].get("pinterest"),
        **data["theme"],
    }


def save(con, domain, res):
    res = dict(res)
    res["last_checked"] = now_utc()
    cols = ", ".join(f"{k}=?" for k in res)
    extra = ", attempts=attempts+1" if res.get("status") == "unreachable" else ""
    con.execute(f"UPDATE stores SET {cols}{extra} WHERE domain=?", (*res.values(), domain))
    con.commit()


async def enrich_once(limit):
    con = db_connect()
    rows = con.execute(
        """SELECT domain FROM stores
           WHERE status='pending'
              OR (status='unreachable' AND attempts<3
                  AND (last_checked IS NULL OR last_checked < datetime('now','-1 day')))
           ORDER BY first_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        print("[enrich] কিছু pending নেই")
        return
    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
    counts = {}
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "en"},
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100),
    ) as client:

        async def job(domain):
            async with sem:
                return domain, await enrich_one(client, domain)

        tasks = [asyncio.create_task(job(r["domain"])) for r in rows]
        for fut in asyncio.as_completed(tasks):
            domain, res = await fut
            save(con, domain, res)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
    print(f"[enrich] {len(rows)} domain processed -> {counts}")


async def enrich(args):
    while True:
        await enrich_once(args.limit)
        if not args.loop:
            return
        await asyncio.sleep(args.loop)


# ----------------------------------------------------------------- export / stats
FIELDS = [
    ("launch_date", "Launch Date"), ("domain", "Store Domain"), ("country", "Country"),
    ("email", "Email"), ("phone", "Phone"), ("facebook", "Facebook"),
    ("instagram", "Instagram"), ("linkedin", "LinkedIn"), ("twitter", "X / Twitter"),
    ("tiktok", "TikTok"), ("youtube", "YouTube"), ("pinterest", "Pinterest"),
    ("theme", "Shopify Theme"),
]
SOCIAL_COLS = ("facebook", "instagram", "linkedin", "twitter", "tiktok", "youtube", "pinterest")


def query_by_date(con, start, end, basis="launch"):
    """[start, end] এর মধ্যে launch হওয়া Shopify store.
    basis=launch: certificate date (না থাকলে first_seen) - launch এর সবচেয়ে কাছের অনুমান
    basis=seen  : আমাদের scraper যেদিন প্রথম ধরেছে
    Returns (rows, pending_count)."""
    expr = "COALESCE(cert_not_before, first_seen)" if basis == "launch" else "first_seen"
    rows = con.execute(
        f"""SELECT *, {expr} AS launch_date FROM stores
            WHERE status='shopify' AND {expr} BETWEEN ? AND ?
            ORDER BY country IS NULL, country, domain""",
        (start, end),
    ).fetchall()
    pending = con.execute(
        f"SELECT COUNT(*) FROM stores WHERE status IN ('pending','unreachable') AND {expr} BETWEEN ? AND ?",
        (start, end),
    ).fetchone()[0]
    return rows, pending


def country_label(code):
    if not code:
        return "Unknown"
    try:
        import pycountry
        c = pycountry.countries.get(alpha_2=code)
        return f"{c.name} ({code})" if c else code
    except Exception:
        return code


def country_summary(rows):
    cnt = Counter(r["country"] for r in rows)
    return sorted(((country_label(k), n) for k, n in cnt.items()), key=lambda x: (-x[1], x[0]))


def write_csv(rows, fileobj):
    w = csv.writer(fileobj)
    w.writerow([label for _, label in FIELDS])
    for r in rows:
        w.writerow([r[col] or "" for col, _ in FIELDS])


def parse_date(s):
    try:
        return dt.date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise SystemExit(f"তারিখ ভুল: {s!r} (format: YYYY-MM-DD)")


def report(args):
    con = db_connect()
    start = parse_date(args.date)
    end = parse_date(args.to) if args.to else start
    rows, pending = query_by_date(con, start, end, args.basis)
    if args.with_email:
        rows = [r for r in rows if r["email"]]
    print(f"\n=== {start}" + (f" → {end}" if end != start else "") + f" | মোট {len(rows)} Shopify store launch ===")
    for name, n in country_summary(rows):
        print(f"  {name:<40} {n}")
    if pending:
        print(f"\n(আরও {pending} domain এখনও enrich হয়নি; `enrich` চালালে বাড়তে পারে)")
    out = args.output or f"shopify_launches_{start}" + (f"_to_{end}" if end != start else "") + ".csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:  # Excel-friendly
        write_csv(rows, f)
    print(f"\nCSV -> {out}")


def export(args):
    con = db_connect()
    start = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    rows, _ = query_by_date(con, start, today(), "launch")
    if args.with_email:
        rows = [r for r in rows if r["email"]]
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        write_csv(rows, f)
    print(f"[export] {len(rows)} stores ({start} → {today()}) -> {args.output}")


# ----------------------------------------------------------------- JSON API (Next.js frontend এর জন্য)
def country_name(code):
    if not code:
        return "Unknown"
    try:
        import pycountry
        c = pycountry.countries.get(alpha_2=code)
        return c.name if c else code
    except Exception:
        return code


def _api_filters(q):
    g = lambda k, d="": (q.get(k) or [d])[0]
    start = dt.date.fromisoformat(g("from")).isoformat()
    end = dt.date.fromisoformat(g("to") or g("from")).isoformat()
    basis = "seen" if g("basis") == "seen" else "launch"
    return start, end, basis, bool(g("email")), g("country"), g("q").strip().lower()


def _filtered(q, with_country=True):
    start, end, basis, email, country, text = _api_filters(q)
    rows, pending = query_by_date(db_connect(), start, end, basis)
    if email:
        rows = [r for r in rows if r["email"]]
    if with_country and country:
        rows = [r for r in rows if (r["country"] or "unknown") == country]
    if text:
        rows = [r for r in rows if text in r["domain"] or text in (r["email"] or "").lower()]
    return rows, pending, start, end


def _row_json(r):
    return {
        "launch_date": r["launch_date"], "domain": r["domain"],
        "country": r["country"], "country_name": country_name(r["country"]),
        "email": r["email"], "phone": r["phone"], "theme": r["theme"],
        "socials": {c: r[c] for c in SOCIAL_COLS if r[c]},
    }


def api_summary(q):
    rows, pending, _, _ = _filtered(q, with_country=False)
    cnt = Counter(r["country"] or "unknown" for r in rows)
    countries = [
        {"code": c, "name": country_name(None if c == "unknown" else c), "count": n}
        for c, n in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))
    ]
    return {"total": len(rows), "pending": pending, "countries": countries}


def api_stores(q):
    rows, _, _, _ = _filtered(q)
    g = lambda k, d: (q.get(k) or [d])[0]
    offset = max(0, int(g("offset", "0")))
    limit = max(1, min(int(g("limit", "50")), 200))
    return {"total": len(rows), "rows": [_row_json(r) for r in rows[offset:offset + limit]]}


def api_daily(q):
    g = lambda k, d: (q.get(k) or [d])[0]
    days = max(1, min(int(g("days", "30")), 365))
    expr = "COALESCE(cert_not_before, first_seen)" if g("basis", "launch") != "seen" else "first_seen"
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    found = {
        r["d"]: r["c"]
        for r in db_connect().execute(
            f"SELECT {expr} AS d, COUNT(*) AS c FROM stores WHERE status='shopify' "
            f"AND {expr} BETWEEN ? AND ? GROUP BY d",
            (start.isoformat(), end.isoformat()),
        )
    }
    out = []
    for i in range(days):
        d = (start + dt.timedelta(days=i)).isoformat()
        out.append({"date": d, "count": found.get(d, 0)})
    return out


# ----------------------------------------------------------------- web UI (stdlib only, localhost)
PAGE = """<!doctype html><html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shopify Launch Finder</title>
<style>
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24}
 header{background:#1b1f24;color:#fff;padding:14px 24px;font-size:18px;font-weight:600}
 main{padding:20px 24px;max-width:1400px;margin:auto}
 form{display:flex;flex-wrap:wrap;gap:12px;align-items:end;background:#fff;padding:16px;border-radius:10px;border:1px solid #e2e5ea}
 label{display:flex;flex-direction:column;font-size:12px;color:#5b6572;gap:4px}
 input,select,button{font:inherit;padding:8px 10px;border:1px solid #c9ced6;border-radius:6px;background:#fff}
 button{background:#2563eb;color:#fff;border-color:#2563eb;cursor:pointer}
 a.btn{padding:8px 12px;border-radius:6px;background:#16a34a;color:#fff;text-decoration:none}
 .grid{display:grid;grid-template-columns:300px 1fr;gap:16px;margin-top:16px}
 @media(max-width:900px){.grid{grid-template-columns:1fr}}
 .card{background:#fff;border:1px solid #e2e5ea;border-radius:10px;padding:14px;overflow:auto}
 table{border-collapse:collapse;width:100%%}
 th,td{padding:6px 8px;border-bottom:1px solid #eef0f3;text-align:left;white-space:nowrap}
 th{position:sticky;top:0;background:#fafbfc}
 .total{font-size:28px;font-weight:700}.muted{color:#6b7480;font-size:12px}.err{color:#b91c1c}
</style></head><body><header>Shopify Launch Finder</header><main>
<form method="get" action="/">
 <label>তারিখ (From)<input type="date" name="date" value="%(date)s" required></label>
 <label>To (optional)<input type="date" name="to" value="%(to)s"></label>
 <label>তারিখের ভিত্তি<select name="basis">
  <option value="launch" %(sel_launch)s>Certificate date (launch অনুমান)</option>
  <option value="seen" %(sel_seen)s>Scraper যেদিন প্রথম ধরেছে</option></select></label>
 <label>Filter<select name="email"><option value="">সব store</option>
  <option value="1" %(sel_email)s>শুধু email আছে</option></select></label>
 <button>খুঁজো</button>%(download)s
</form>%(body)s</main></body></html>"""


def _link(url):
    if not url:
        return ""
    short = htmllib.escape(url.replace("https://www.", "").replace("https://", "").rstrip("/"))
    return f'<a href="{htmllib.escape(url)}" target="_blank" rel="noopener noreferrer">{short}</a>'


def render_page(q):
    g = lambda k, d="": (q.get(k) or [d])[0]
    date, to, basis, only_email = g("date"), g("to"), g("basis", "launch"), g("email")
    ctx = {"date": htmllib.escape(date or today()), "to": htmllib.escape(to), "download": "", "body": "",
           "sel_launch": "selected" if basis == "launch" else "", "sel_seen": "selected" if basis == "seen" else "",
           "sel_email": "selected" if only_email else ""}
    if not date:
        ctx["body"] = '<p class="muted">তারিখ দিয়ে "খুঁজো" চাপো।</p>'
        return PAGE % ctx
    try:
        start = dt.date.fromisoformat(date).isoformat()
        end = dt.date.fromisoformat(to).isoformat() if to else start
    except ValueError:
        ctx["body"] = '<p class="err">তারিখ ভুল।</p>'
        return PAGE % ctx
    con = db_connect()
    rows, pending = query_by_date(con, start, end, basis)
    if only_email:
        rows = [r for r in rows if r["email"]]
    summ = country_summary(rows)
    qs = htmllib.escape(f"date={start}&to={end}&basis={basis}" + ("&email=1" if only_email else ""))
    ctx["download"] = f'<a class="btn" href="/download?{qs}">CSV download</a>'
    shown = rows[:500]
    trs = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>{}<td>{}</td></tr>".format(
            htmllib.escape(r["launch_date"] or ""),
            _link("https://" + r["domain"]),
            htmllib.escape(r["country"] or "—"),
            htmllib.escape(r["email"] or ""),
            htmllib.escape(r["phone"] or ""),
            "".join(f"<td>{_link(r[c])}</td>" for c in SOCIAL_COLS),
            htmllib.escape(r["theme"] or ""),
        )
        for r in shown
    )
    crows = "".join(f"<tr><td>{htmllib.escape(n)}</td><td><b>{c}</b></td></tr>" for n, c in summ)
    note = f'<p class="muted">প্রথম 500 টি দেখানো হচ্ছে, বাকিগুলো CSV তে।</p>' if len(rows) > 500 else ""
    pend = f'<p class="muted">আরও {pending} domain এখনও enrich হয়নি।</p>' if pending else ""
    ctx["body"] = f"""<div class="grid">
<div class="card"><div class="muted">মোট store launch</div><div class="total">{len(rows)}</div>
<h4>Country অনুযায়ী</h4><table>{crows or '<tr><td class="muted">কোনো data নেই</td></tr>'}</table>{pend}</div>
<div class="card"><table><thead><tr><th>Launch</th><th>Domain</th><th>Country</th><th>Email</th><th>Phone</th>
<th>Facebook</th><th>Instagram</th><th>LinkedIn</th><th>X</th><th>TikTok</th><th>YouTube</th><th>Pinterest</th><th>Theme</th></tr></thead>
<tbody>{trs}</tbody></table>{note}</div></div>"""
    return PAGE % ctx


class WebHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, extra=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/api/summary", "/api/stores", "/api/daily"):
            try:
                fn = {"/api/summary": api_summary, "/api/stores": api_stores, "/api/daily": api_daily}[u.path]
                return self._send(200, json.dumps(fn(q)), "application/json; charset=utf-8")
            except (ValueError, KeyError) as e:
                return self._send(400, json.dumps({"error": f"bad request: {e}"}), "application/json")
        if u.path == "/api/download":
            try:
                rows, _, start, end = _filtered(q)
            except ValueError:
                return self._send(400, "bad date", "text/plain")
            buf = io.StringIO()
            write_csv(rows, buf)
            return self._send(200, "\ufeff" + buf.getvalue(), "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="shopify_launches_{start}_{end}.csv"'})
        if u.path == "/":
            self._send(200, render_page(q), "text/html; charset=utf-8")
        elif u.path == "/download":
            try:
                start = dt.date.fromisoformat((q.get("date") or [""])[0]).isoformat()
                to = (q.get("to") or [""])[0]
                end = dt.date.fromisoformat(to).isoformat() if to else start
            except ValueError:
                return self._send(400, "bad date", "text/plain")
            rows, _ = query_by_date(db_connect(), start, end, (q.get("basis") or ["launch"])[0])
            if (q.get("email") or [""])[0]:
                rows = [r for r in rows if r["email"]]
            buf = io.StringIO()
            write_csv(rows, buf)
            self._send(200, "\ufeff" + buf.getvalue(), "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="shopify_launches_{start}_{end}.csv"'})
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


def web(args):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), WebHandler)  # শুধু তোমার PC থেকে
    print(f"Open: http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    srv.serve_forever()


def stats(_args):
    con = db_connect()
    for row in con.execute("SELECT status, COUNT(*) c FROM stores GROUP BY status"):
        print(f"{row['status']:<12} {row['c']}")
    r = con.execute(
        """SELECT COUNT(*) n, SUM(email IS NOT NULL) e, SUM(phone IS NOT NULL) p,
                  SUM(instagram IS NOT NULL) i, SUM(country IS NOT NULL) c
           FROM stores WHERE status='shopify'"""
    ).fetchone()
    if r["n"]:
        print(f"\nShopify stores: {r['n']}  email={r['e']}  phone={r['p']}  instagram={r['i']}  country={r['c']}")


# ----------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description="Shopify Radar")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="CT log stream থেকে candidate domain ধরো")
    d.add_argument("--url", default=CERTSTREAM_URL)

    s = sub.add_parser("seed", help="text file থেকে domain import")
    s.add_argument("file")

    e = sub.add_parser("enrich", help="pending domain verify + data extract")
    e.add_argument("--limit", type=int, default=500)
    e.add_argument("--loop", type=int, default=0, help="প্রতি N সেকেন্ডে repeat")

    x = sub.add_parser("export", help="CSV export")
    x.add_argument("--days", type=int, default=7)
    x.add_argument("-o", "--output", default="shopify_stores.csv")
    x.add_argument("--with-email", action="store_true")

    r = sub.add_parser("report", help="নির্দিষ্ট তারিখে launch হওয়া store + country হিসাব")
    r.add_argument("--date", required=True, help="YYYY-MM-DD")
    r.add_argument("--to", help="range এর শেষ তারিখ (optional)")
    r.add_argument("--basis", choices=["launch", "seen"], default="launch")
    r.add_argument("--with-email", action="store_true")
    r.add_argument("-o", "--output")

    wb = sub.add_parser("web", help="date-picker সহ local web UI")
    wb.add_argument("--port", type=int, default=8000)

    sub.add_parser("stats")

    args = p.parse_args()
    try:
        if args.cmd == "discover":
            asyncio.run(discover(args))
        elif args.cmd == "enrich":
            asyncio.run(enrich(args))
        else:
            {"seed": seed, "export": export, "stats": stats, "report": report, "web": web}[args.cmd](args)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
