"use client";
import Link from "next/link";
import { useCallback, useState } from "react";
import { ErrorBox, StatusPill } from "@/components/ui";
import { usePoll } from "@/hooks/usePoll";
import { api, Clip, fileUrl } from "@/lib/api";

const FILTERS = ["", "ready", "needs_review", "approved", "exported", "rejected"];
const LABELS: Record<string, string> = { "": "Tous", ready: "Prêts", needs_review: "À vérifier", approved: "Validés", exported: "Exportés", rejected: "Rejetés" };

export default function ClipsPage() {
  const [filter, setFilter] = useState("");
  const clips = usePoll<Clip[]>(useCallback(() => api.clips(filter || undefined), [filter]), () => false);

  return (
    <>
      <h1>Clips</h1>
      <div className="chips" style={{ margin: "12px 0 18px" }}>
        {FILTERS.map((f) => (
          <button key={f} className={`chip ${f === filter ? "active" : ""}`} onClick={() => setFilter(f)}>{LABELS[f]}</button>
        ))}
      </div>
      <ErrorBox message={clips.error} />
      {!clips.data?.length && <p className="muted">Aucun clip.</p>}
      <div className="grid grid-clips">
        {clips.data?.map((c) => (
          <Link key={c.id} href={`/clips/${c.id}`} className="card" style={{ padding: 10 }}>
            {c.thumbnail_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img className="thumb" src={fileUrl(c.thumbnail_url)} alt="" />
            ) : (
              <div className="thumb">—</div>
            )}
            <div className="row spread" style={{ marginTop: 8 }}>
              <span className="small muted">{c.variant} · {c.duration.toFixed(0)}s</span>
              <StatusPill status={c.status} />
            </div>
            <div className="small" style={{ marginTop: 6, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.title ?? c.hook?.text}</div>
          </Link>
        ))}
      </div>
    </>
  );
}
