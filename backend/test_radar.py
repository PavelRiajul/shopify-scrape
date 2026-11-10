"""Offline test: local mock Shopify store + non-Shopify site দিয়ে পুরো pipeline যাচাই।
চালাও:  python test_radar.py
"""
import asyncio
import csv
import http.server
import os
import tempfile
import threading
import types

SHOP_HTML = """<html><head>
<script>var Shopify = Shopify || {}; Shopify.country = "US";
Shopify.theme = {"name":"Copy of Dawn","id":123,"schema_name":"Dawn","schema_version":"15.2.0","theme_store_id":887,"role":"main"};</script>
<script src="https://cdn.shopify.com/s/files/1/x.js"></script>
<script type="application/ld+json">{"@type":"Organization","telephone":"+44 20 7946 0958","sameAs":["https://www.linkedin.com/company/nordic-wool/"]}</script>
</head><body>
<a href="/pages/contact">Contact</a>
<a href="https://www.instagram.com/nordicwool/">IG</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
<a href="https://www.facebook.com/nordicwool">FB</a>
<a href="https://twitter.com/intent/tweet?x=1">tw</a><a href="https://twitter.com/nordicwool">X</a>
<a href="https://www.tiktok.com/@nordicwool">tt</a><a href="https://www.youtube.com/@nordicwool">yt</a>
<a href="https://www.pinterest.com/pin/create/button/">p</a><a href="https://www.pinterest.com/nordicwool/">pin</a>
<img src="logo@2x.png"> <a href="mailto:hello@nordic-wool.com?subject=hi">mail</a>
</body></html>"""

CONTACT_HTML = """<html><body>Email us: support@nordic-wool.com
<a class="__cf_email__" data-cfemail="%s">[email&#160;protected]</a></body></html>"""


def cf_encode(email, key=0x42):
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/plain":
            body, headers = "<html>just a blog</html>", {}
        elif self.path == "/pages/contact":
            body, headers = CONTACT_HTML % cf_encode("sales@nordic-wool.com"), {}
        else:
            body, headers = SHOP_HTML, {"X-ShopId": "1"}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


def main():
    tmp = tempfile.mkdtemp()
    os.environ["RADAR_DB"] = os.path.join(tmp, "t.db")
    import radar

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # --- unit checks
    assert radar.normalize_ct_name("www.Example.com.") == "example.com"
    assert radar.normalize_ct_name("shop.example.com") is None
    assert radar.normalize_ct_name("*.example.com") is None
    assert radar.normalize_ct_name("x.myshopify.com") is None
    assert radar.country_from_tld("brand.co.uk") == "GB"
    assert radar.country_from_tld("brand.de") == "DE"
    assert radar.country_from_tld("brand.com") is None
    assert radar.country_from_tld("brand.io") is None
    assert radar.country_from_phone("+442079460958") == "GB"
    assert radar.social_from_url("https://facebook.com/sharer/sharer.php?u=1") is None
    assert radar.social_from_url("https://www.instagram.com/p/abc") is None
    print("unit checks OK")

    # --- end-to-end (seed -> enrich -> export)
    con = radar.db_connect()
    radar.add_candidate(con, f"127.0.0.1:{port}", "test")
    radar.add_candidate(con, f"127.0.0.1:{port}/plain".split("/")[0] + "", "test")  # same host, ignored
    asyncio.run(radar.enrich_once(10))
    row = con.execute("SELECT * FROM stores").fetchone()
    print(dict(row))
    assert row["status"] == "shopify"
    assert row["theme"] == "Dawn" and row["theme_version"] == "15.2.0"
    assert row["email"] == "hello@nordic-wool.com"
    assert row["phone"] == "+442079460958"
    assert row["country"] == "GB" and row["country_source"] == "phone"
    assert row["instagram"] == "https://www.instagram.com/nordicwool/"
    assert row["facebook"] == "https://www.facebook.com/nordicwool"
    assert row["linkedin"] == "https://www.linkedin.com/company/nordic-wool"

    assert row["twitter"] == "https://x.com/nordicwool"
    assert row["tiktok"] == "https://www.tiktok.com/@nordicwool"
    assert row["youtube"] == "https://www.youtube.com/@nordicwool"
    assert row["pinterest"] == "https://www.pinterest.com/nordicwool/"

    out = os.path.join(tmp, "out.csv")
    radar.export(types.SimpleNamespace(days=7, output=out, with_email=False))
    with open(out, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["Shopify Theme"] == "Dawn"
    print("end-to-end OK ->", rows[0])

    # --- date report: আরও কয়েকটা store বানিয়ে country হিসাব যাচাই
    for dom, nb, st, ctry in [("a.de", "2026-11-09", "shopify", "DE"), ("b.de", "2026-11-09", "shopify", "DE"),
                              ("c.com", "2026-11-09", "shopify", "US"), ("d.com", "2026-11-08", "shopify", "US"),
                              ("e.com", "2026-11-09", "shopify", None), ("f.com", "2026-11-09", "pending", None)]:
        con.execute("INSERT INTO stores(domain, first_seen, cert_not_before, status, country, email) VALUES (?,?,?,?,?,?)",
                    (dom, "2026-11-10", nb, st, ctry, f"hi@{dom}"))
    con.commit()
    rows, pending = radar.query_by_date(con, "2026-11-09", "2026-11-09")
    assert len(rows) == 4 and pending == 1, (len(rows), pending)
    summ = dict(radar.country_summary(rows))
    assert summ["Germany (DE)"] == 2 and summ["United States (US)"] == 1 and summ["Unknown"] == 1, summ
    radar.report(types.SimpleNamespace(date="2026-11-09", to=None, basis="launch", with_email=False,
                                       output=os.path.join(tmp, "r.csv")))
    try:
        radar.parse_date("09-11-2026"); raise AssertionError("should fail")
    except SystemExit:
        pass

    # --- web UI
    import urllib.request
    web = radar.http.server.ThreadingHTTPServer(("127.0.0.1", 0), radar.WebHandler)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{web.server_address[1]}"
    page = urllib.request.urlopen(base + "/?date=2026-11-09&basis=launch").read().decode()
    assert "Germany (DE)" in page and "a.de" in page and "Total" not in page
    csv_body = urllib.request.urlopen(base + "/download?date=2026-11-09&basis=launch").read().decode("utf-8-sig")
    assert csv_body.splitlines()[0].startswith("Launch Date,Store Domain") and "b.de" in csv_body
    assert urllib.request.urlopen(base + "/").status == 200
    import json as _json
    get = lambda p: _json.loads(urllib.request.urlopen(base + p).read())
    summ = get("/api/summary?from=2026-11-09&to=2026-11-09")
    assert summ["total"] == 4 and summ["pending"] == 1
    assert summ["countries"][0] == {"code": "DE", "name": "Germany", "count": 2}, summ
    assert any(c["code"] == "unknown" for c in summ["countries"])
    st = get("/api/stores?from=2026-11-09&to=2026-11-09&country=DE&limit=1&offset=1")
    assert st["total"] == 2 and len(st["rows"]) == 1 and st["rows"][0]["country_name"] == "Germany"
    assert get("/api/stores?from=2026-11-09&q=c.com")["total"] == 1
    assert get("/api/stores?from=2026-11-09&country=unknown")["total"] == 1
    daily = get("/api/daily?days=30")
    assert len(daily) == 30 and daily[-1]["date"] == radar.today()
    try:
        urllib.request.urlopen(base + "/api/stores?from=nope")
        raise AssertionError("should 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    dl = urllib.request.urlopen(base + "/api/download?from=2026-11-09&country=DE").read().decode("utf-8-sig")
    assert len(dl.strip().splitlines()) == 3
    web.shutdown()
    print("report + web + json api OK")
    srv.shutdown()


if __name__ == "__main__":
    main()
