const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const longFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });

// "YYYY-MM-DD" কে local midnight ধরা হয় যাতে timezone এ দিন সরে না যায়
const toDate = (iso: string) => new Date(`${iso}T00:00:00`);

export const fmtDay = (iso: string) => dayFmt.format(toDate(iso));
export const fmtLong = (iso: string) => longFmt.format(toDate(iso));

export function flag(code: string | null): string {
  if (!code || code.length !== 2) return "🌐";
  return String.fromCodePoint(...[...code.toUpperCase()].map((c) => 127397 + c.charCodeAt(0)));
}
