"use client";
import Link from "next/link";
import { useCallback, useState } from "react";
import { Segmented } from "@/components/controls";
import { Alert, Empty, ExpiryTag, StatusPill } from "@/components/ui";
import { usePoll } from "@/hooks/usePoll";
import { api, Clip, fileUrl } from "@/lib/api";

// chaque filtre regroupe un ou plusieurs statuts du serveur
const FILTERS = [
  { value: "ready,approved,rendering,pending", label: "Prêts" },
  { value: "needs_review,failed", label: "À vérifier" },
  { value: "exported", label: "Enregistrés" },
  { value: "rejected", label: "Écartés" },
];
const EMPTY: Record<string, string> = {
  "ready,approved,rendering,pending": "Aucun clip prêt à publier.",
  "needs_review,failed": "Rien à vérifier 👌",
  exported: "Aucun clip enregistré pour l'instant.",
  rejected: "Aucun clip écarté.",
};

export default function ClipsPage() {
  const [filter, setFilter] = useState(FILTERS[0].value);
  const clips = usePoll<Clip[]>(useCallback(() => api.clips(filter), [filter]),
    useCallback((d?: Clip[]) => !!d?.some((c) => c.status === "rendering"), []), 4000);

  return (
    <div className="stack-lg">
      <div className="page-head">
        <span className="eyebrow">Bibliothèque</span>
        <h1>Mes clips</h1>
      </div>
      <Segmented value={filter} onChange={setFilter} options={FILTERS} />
      <Alert>{clips.error}</Alert>
      {!clips.data ? (
        <div className="grid grid-clips">{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton" style={{ aspectRatio: "9/16" }} />)}</div>
      ) : clips.data.length === 0 ? (
        <Empty emoji="📭" title={EMPTY[filter] ?? "Aucun clip ici"}>
          <p className="muted small">Analyse une vidéo puis crée des clips à partir de ses moments forts.</p>
          <Link href="/import" className="btn btn-primary">Nouvelle vidéo</Link>
        </Empty>
      ) : (
        <div className="grid grid-clips">
          {clips.data.map((c) => (
            <Link key={c.id} href={`/clips/${c.id}`} className="stack-sm card-link" style={{ gap: 8 }}>
              <div className="thumb-wrap">
                {c.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img className="thumb" src={fileUrl(c.thumbnail_url)} alt="" loading="lazy" />
                ) : (
                  <div className="thumb">{c.status === "rendering" ? "⏳ Rendu…" : "🎬"}</div>
                )}
                <div className="overlay">
                  <span className="badge">{c.variant === "compilation" ? "🎬 Top" : c.other_variants?.length ? `🎞 +${c.other_variants.length}` : "🎞"}</span>
                  <span className="badge mono">{Math.round(c.duration)} s</span>
                </div>
              </div>
              <div className="row-nowrap spread">
                <span className="small clamp-1 grow">{c.title ?? c.hook?.text ?? "Clip"}</span>
              </div>
              <div className="row-nowrap spread"><StatusPill status={c.status} /><ExpiryTag at={c.expires_at} compact /></div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
