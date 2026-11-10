"use client";

import { useState } from "react";
import { SOCIALS, type Store } from "@/lib/api";
import { flag } from "@/lib/format";

function CopyEmail({ email }: { email: string }) {
  const [done, setDone] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(email);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    } catch {
      /* clipboard blocked: user can still select the text */
    }
  }
  return (
    <span className="email">
      <a href={`mailto:${email}`}>{email}</a>
      <button type="button" className="textbtn" onClick={copy} aria-label={`Copy ${email}`}>
        {done ? "Copied" : "Copy"}
      </button>
    </span>
  );
}

type Props = {
  rows: Store[];
  total: number;
  loading: boolean;
  onMore: () => void;
};

export default function StoreTable({ rows, total, loading, onMore }: Props) {
  return (
    <>
      <div className="tablewrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Store</th>
              <th scope="col">Country</th>
              <th scope="col">Email</th>
              <th scope="col">Phone</th>
              <th scope="col">Social</th>
              <th scope="col">Theme</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.domain}>
                <td>
                  <a href={`https://${s.domain}`} target="_blank" rel="noopener noreferrer" className="domain">
                    {s.domain}
                  </a>
                </td>
                <td>
                  <span aria-hidden="true">{flag(s.country)}</span> {s.country_name}
                </td>
                <td>{s.email ? <CopyEmail email={s.email} /> : <span className="none">–</span>}</td>
                <td>{s.phone ?? <span className="none">–</span>}</td>
                <td>
                  <span className="chips">
                    {SOCIALS.filter(([key]) => s.socials[key]).map(([key, short, full]) => (
                      <a
                        key={key}
                        className="chip"
                        href={s.socials[key]}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={`${full} for ${s.domain}`}
                        title={full}
                      >
                        {short}
                      </a>
                    ))}
                    {Object.keys(s.socials).length === 0 && <span className="none">–</span>}
                  </span>
                </td>
                <td>{s.theme ?? <span className="none">–</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="tablefoot">
        <span>{total === 0 && !loading ? "No stores match these filters." : `Showing ${rows.length} of ${total}`}</span>
        {rows.length < total && (
          <button type="button" className="btn btn--quiet" onClick={onMore} disabled={loading}>
            {loading ? "Loading…" : "Show 50 more"}
          </button>
        )}
      </div>
    </>
  );
}
