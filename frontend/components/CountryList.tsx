import type { CSSProperties } from "react";
import type { CountryCount } from "@/lib/api";
import { flag } from "@/lib/format";

type Props = {
  countries: CountryCount[];
  total: number;
  selected: string | null;
  onSelect: (code: string | null) => void;
};

export default function CountryList({ countries, total, selected, onSelect }: Props) {
  const max = Math.max(1, ...countries.map((c) => c.count));
  return (
    <div className="countries">
      <button
        type="button"
        className={`countries__row${selected === null ? " is-selected" : ""}`}
        aria-pressed={selected === null}
        onClick={() => onSelect(null)}
      >
        <span className="countries__name">All countries</span>
        <span className="countries__n">{total}</span>
      </button>
      {countries.map((c) => (
        <button
          key={c.code}
          type="button"
          className={`countries__row countries__row--bar${selected === c.code ? " is-selected" : ""}`}
          style={{ "--w": `${(c.count / max) * 100}%` } as CSSProperties}
          aria-pressed={selected === c.code}
          onClick={() => onSelect(selected === c.code ? null : c.code)}
        >
          <span className="countries__name">
            <span aria-hidden="true">{c.code === "unknown" ? "🌐" : flag(c.code)}</span> {c.name}
          </span>
          <span className="countries__n">{c.count}</span>
        </button>
      ))}
    </div>
  );
}
