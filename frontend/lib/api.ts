export type Basis = "launch" | "seen";

export type Day = { date: string; count: number };
export type CountryCount = { code: string; name: string; count: number };
export type Summary = { total: number; pending: number; countries: CountryCount[] };

export const SOCIALS = [
  ["facebook", "fb", "Facebook"],
  ["instagram", "ig", "Instagram"],
  ["linkedin", "in", "LinkedIn"],
  ["twitter", "x", "X / Twitter"],
  ["tiktok", "tt", "TikTok"],
  ["youtube", "yt", "YouTube"],
  ["pinterest", "pin", "Pinterest"],
] as const;

export type SocialKey = (typeof SOCIALS)[number][0];

export type Store = {
  launch_date: string;
  domain: string;
  country: string | null;
  country_name: string;
  email: string | null;
  phone: string | null;
  theme: string | null;
  socials: Partial<Record<SocialKey, string>>;
};

export type StoresPage = { total: number; rows: Store[] };

export type Filters = {
  from: string;
  to: string;
  basis: Basis;
  email: boolean;
  country: string | null; // country code, or "unknown"
  q: string;
};

function qs(f: Filters, extra: Record<string, string | number> = {}) {
  const p = new URLSearchParams({ from: f.from, to: f.to, basis: f.basis });
  if (f.email) p.set("email", "1");
  if (f.country) p.set("country", f.country);
  if (f.q) p.set("q", f.q);
  for (const [k, v] of Object.entries(extra)) p.set(k, String(v));
  return p.toString();
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal, cache: "no-store" });
  if (!res.ok) throw new Error(`Server replied ${res.status}`);
  return res.json() as Promise<T>;
}

export const fetchDaily = (basis: Basis, days: number, signal?: AbortSignal) =>
  get<Day[]>(`/api/daily?basis=${basis}&days=${days}`, signal);

// Summary country list-কে country filter আর search থেকে স্বাধীন রাখা হয়
export const fetchSummary = (f: Filters, signal?: AbortSignal) =>
  get<Summary>(`/api/summary?${qs({ ...f, country: null, q: "" })}`, signal);

export const fetchStores = (f: Filters, offset: number, limit: number, signal?: AbortSignal) =>
  get<StoresPage>(`/api/stores?${qs(f, { offset, limit })}`, signal);

export const downloadUrl = (f: Filters) => `/api/download?${qs(f)}`;
