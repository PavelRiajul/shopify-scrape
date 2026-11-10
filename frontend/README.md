# Frontend (Next.js 15, App Router, TypeScript)

Browser → Next.js (`/api/*` rewrite) → Python backend (`../backend`, port 8000)

## Run
```bash
# Terminal 1: backend folder এ (আলাদা project, JSON দেয়)
cd ../backend && python radar.py web --port 8000

# Terminal 2: frontend
cd frontend
npm install
npm run dev          # http://localhost:3000
```
Backend অন্য জায়গায় থাকলে: `RADAR_API=http://your-server:8000 npm run dev`

Production: `npm run build && npm start`

## Files
- `app/page.tsx`: state, data fetching, layout
- `components/DayStrip.tsx`: ৩০ দিনের bar chart (click = দিন, shift+click = range)
- `components/CountryList.tsx`: country অনুযায়ী count, click করলে table filter
- `components/StoreTable.tsx`: domain, email (copy), phone, social links, theme
- `lib/api.ts`: API types ও fetch helpers · `lib/format.ts`: তারিখ ও flag
- `app/globals.css`: সব style (design tokens `:root` এ)

## Backend API (../backend/radar.py)
`GET /api/daily?days=30&basis=launch|seen` · `/api/summary?from&to&basis&email` ·
`/api/stores?from&to&basis&email&country&q&offset&limit` · `/api/download?…` (CSV)
