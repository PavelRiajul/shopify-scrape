# Backend (Python)

Shopify store detector + enricher + JSON API. Frontend আলাদা: `../frontend`

## Setup
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python test_radar.py               # শেষে "report + web + json api OK" আসা উচিত
```

## Commands
```bash
python radar.py seed domains.txt      # নিজের domain list (প্রতি লাইনে একটা) দিয়ে শুরু
python radar.py enrich                # verify + email/phone/social/theme/country
python radar.py stats
python radar.py report --date 2026-11-10          # country-wise হিসাব + CSV
python radar.py export --days 7 -o last7.csv

python radar.py web --port 8000       # JSON API + পুরনো সরল HTML UI  (frontend এটাই ব্যবহার করে)
```

## Live discovery (Docker লাগবে)
```bash
docker compose up -d                  # certstream server
python radar.py discover              # সারাক্ষণ চলবে
python radar.py enrich --loop 600     # আরেক terminal এ
```

## API (port 8000)
- `GET /api/daily?days=30&basis=launch|seen`
- `GET /api/summary?from=YYYY-MM-DD&to=YYYY-MM-DD&basis=&email=1`
- `GET /api/stores?from&to&basis&email&country&q&offset&limit`
- `GET /api/download?...` (CSV)

## Config (env var)
`RADAR_DB` (default `radar.db`), `CERTSTREAM_URL`, `RADAR_CONTACT` (User-Agent এ তোমার email)

## "Launch date" মানে
- `launch` (default): SSL certificate এর তারিখ, না থাকলে scraper যেদিন প্রথম ধরেছে (অনুমান)
- `seen`: scraper যেদিন প্রথম ধরেছে
- `discover` চালু করার আগের তারিখের data থাকবে না
