"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import CountryList from "@/components/CountryList";
import DayStrip from "@/components/DayStrip";
import StoreTable from "@/components/StoreTable";
import {
  downloadUrl, fetchDaily, fetchStores, fetchSummary,
  type Basis, type Day, type Filters, type Store, type Summary,
} from "@/lib/api";
import { fmtLong } from "@/lib/format";

const PAGE_SIZE = 50;
const STRIP_DAYS = 30;

function useDebounced<T>(value: T, ms: number) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

const isAbort = (e: unknown) => e instanceof DOMException && e.name === "AbortError";

export default function Page() {
  const [basis, setBasis] = useState<Basis>("launch");
  const [days, setDays] = useState<Day[] | null>(null);
  const [range, setRange] = useState<{ from: string; to: string } | null>(null);
  const [onlyEmail, setOnlyEmail] = useState(false);
  const [country, setCountry] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 300);

  const [summary, setSummary] = useState<Summary | null>(null);
  const [rows, setRows] = useState<Store[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const filters: Filters | null = useMemo(
    () => (range ? { ...range, basis, email: onlyEmail, country, q } : null),
    [range, basis, onlyEmail, country, q],
  );

  // 1) দিনভিত্তিক bar chart
  useEffect(() => {
    const ac = new AbortController();
    fetchDaily(basis, STRIP_DAYS, ac.signal)
      .then((d) => {
        setError(null);
        setDays(d);
        const latest = [...d].reverse().find((x) => x.count > 0) ?? d[d.length - 1];
        setRange((r) => r ?? { from: latest.date, to: latest.date });
      })
      .catch((e) => !isAbort(e) && setError(e.message));
    return () => ac.abort();
  }, [basis]);

  // 2) country summary (country filter আর search ছাড়া)
  const summaryKey = filters && `${filters.from}|${filters.to}|${filters.basis}|${filters.email}`;
  useEffect(() => {
    if (!filters) return;
    const ac = new AbortController();
    fetchSummary(filters, ac.signal)
      .then(setSummary)
      .catch((e) => !isAbort(e) && setError(e.message));
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summaryKey]);

  // 3) store table (প্রথম পাতা)
  useEffect(() => {
    if (!filters) return;
    const ac = new AbortController();
    setLoading(true);
    fetchStores(filters, 0, PAGE_SIZE, ac.signal)
      .then((p) => {
        setRows(p.rows);
        setTotal(p.total);
        setLoading(false);
      })
      .catch((e) => {
        if (!isAbort(e)) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => ac.abort();
  }, [filters]);

  const loadMore = useCallback(() => {
    if (!filters) return;
    setLoading(true);
    fetchStores(filters, rows.length, PAGE_SIZE)
      .then((p) => setRows((prev) => [...prev, ...p.rows]))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [filters, rows.length]);

  function pick(date: string, extend: boolean) {
    setCountry(null);
    setRange((r) =>
      extend && r
        ? { from: date < r.from ? date : r.from, to: date > r.to ? date : r.to }
        : { from: date, to: date },
    );
  }

  function preset(n: number) {
    if (!days) return;
    setCountry(null);
    setRange({ from: days[days.length - n].date, to: days[days.length - 1].date });
  }

  function setDate(key: "from" | "to", value: string) {
    if (!value || !range) return;
    setCountry(null);
    const next = { ...range, [key]: value };
    if (next.from > next.to) next[key === "from" ? "to" : "from"] = value;
    setRange(next);
  }

  const heading = range
    ? range.from === range.to ? fmtLong(range.from) : `${fmtLong(range.from)} to ${fmtLong(range.to)}`
    : "";
  const empty = summary !== null && summary.total === 0 && !loading;

  return (
    <main className="shell">
      <header className="top">
        <h1>New Shopify stores</h1>
        {filters && total > 0 && (
          <a className="btn" href={downloadUrl(filters)}>
            Export {total} to CSV
          </a>
        )}
      </header>

      {error && (
        <div className="notice notice--error" role="alert">
          Can&rsquo;t reach the scraper API ({error}). Start it with <code>python radar.py web</code>, then reload.
        </div>
      )}

      <section aria-label="Launches in the last 30 days" className="stripwrap">
        {days ? (
          <DayStrip
            days={days}
            from={range?.from ?? ""}
            to={range?.to ?? ""}
            onPick={pick}
          />
        ) : (
          <div className="strip strip--loading">Loading days…</div>
        )}
        <p className="hint">Click a day to see its stores. Shift-click another day to select a range.</p>
      </section>

      <section className="controls" aria-label="Date and counting options">
        <label>
          From
          <input type="date" value={range?.from ?? ""} onChange={(e) => setDate("from", e.target.value)} />
        </label>
        <label>
          To
          <input type="date" value={range?.to ?? ""} onChange={(e) => setDate("to", e.target.value)} />
        </label>
        <div className="presets">
          <button type="button" className="btn btn--quiet" onClick={() => preset(7)} disabled={!days}>
            Last 7 days
          </button>
          <button type="button" className="btn btn--quiet" onClick={() => preset(STRIP_DAYS)} disabled={!days}>
            Last 30 days
          </button>
        </div>
        <label>
          Count a store on
          <select
            value={basis}
            onChange={(e) => {
              setRange(null);
              setSummary(null);
              setBasis(e.target.value as Basis);
            }}
          >
            <option value="launch">its certificate date (best guess of launch)</option>
            <option value="seen">the day my scraper first saw it</option>
          </select>
        </label>
      </section>

      <div className="layout">
        <aside aria-label="Countries">
          <h2>{heading || "Choose a day"}</h2>
          <p className="total">
            {summary ? summary.total : "–"} <span>{summary?.total === 1 ? "store" : "stores"}{onlyEmail ? " with email" : ""}</span>
          </p>
          {summary && summary.pending > 0 && (
            <p className="notice notice--warn">
              {summary.pending} more domains are still waiting to be checked. Run <code>python radar.py enrich</code>.
            </p>
          )}
          {summary && (
            <CountryList countries={summary.countries} total={summary.total} selected={country} onSelect={setCountry} />
          )}
        </aside>

        <section aria-label="Stores">
          <div className="filters">
            <label className="search">
              <span className="sr-only">Search stores</span>
              <input
                type="search"
                placeholder="Search domain or email"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <label className="check">
              <input type="checkbox" checked={onlyEmail} onChange={(e) => setOnlyEmail(e.target.checked)} />
              Only stores with an email
            </label>
          </div>

          {empty ? (
            <div className="empty">
              <h3>No stores recorded for {heading}</h3>
              <p>
                The scraper only records stores it saw while running, so days before you started <code>discover</code> are
                empty. Pick a day with a taller bar, or check that <code>discover</code> and <code>enrich</code> are running.
              </p>
            </div>
          ) : (
            <StoreTable rows={rows} total={total} loading={loading} onMore={loadMore} />
          )}
        </section>
      </div>
    </main>
  );
}
