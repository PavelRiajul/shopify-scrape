"use client";

import { useEffect, useRef } from "react";
import type { Day } from "@/lib/api";
import { fmtDay } from "@/lib/format";

type Props = {
  days: Day[];
  from: string;
  to: string;
  onPick: (date: string, extend: boolean) => void;
};

const BAR_MAX = 112; // px

export default function DayStrip({ days, from, to, onPick }: Props) {
  const max = Math.max(1, ...days.map((d) => d.count));
  const ref = useRef<HTMLDivElement>(null);

  // ছোট screen এ selected দিনটা যেন দেখা যায়
  useEffect(() => {
    ref.current?.querySelector(".is-selected")?.scrollIntoView({ inline: "center", block: "nearest" });
  }, [from, days]);

  return (
    <div className="strip" ref={ref} role="group" aria-label="Stores launched per day">
      {days.map((d, i) => {
        const selected = d.date >= from && d.date <= to;
        const h = d.count === 0 ? 2 : Math.max(6, Math.round((d.count / max) * BAR_MAX));
        const showMonth = i === 0 || d.date.endsWith("-01");
        return (
          <button
            key={d.date}
            type="button"
            className={`strip__day${selected ? " is-selected" : ""}`}
            aria-pressed={selected}
            aria-label={`${fmtDay(d.date)}: ${d.count} stores`}
            title={`${fmtDay(d.date)}: ${d.count} stores. Shift-click to select a range.`}
            onClick={(e) => onPick(d.date, e.shiftKey)}
          >
            <span className="strip__count">{d.count > 0 ? d.count : ""}</span>
            <span className="strip__bar" style={{ height: h, animationDelay: `${i * 16}ms` }} />
            <span className="strip__label">{showMonth ? fmtDay(d.date) : Number(d.date.slice(8))}</span>
          </button>
        );
      })}
    </div>
  );
}
