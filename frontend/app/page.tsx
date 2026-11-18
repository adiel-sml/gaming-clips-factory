"use client";
import Link from "next/link";
import { useCallback } from "react";
import { CategoryTag, ErrorBox, ScoreBadge, StatCard, StatusPill } from "@/components/ui";
import { usePoll } from "@/hooks/usePoll";
import { api, fmt, Stats, Video } from "@/lib/api";

export default function Dashboard() {
  const stats = usePoll<Stats>(api.stats, () => true, 5000);
  const videos = usePoll<Video[]>(
    api.videos,
    useCallback((d?: Video[]) => !!d?.some((v) => ["queued", "analyzing"].includes(v.status)), []),
    3000,
  );
  const s = stats.data;

  return (
    <>
      <div className="row spread">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">Vue d&apos;ensemble de la production de clips.</p>
        </div>
        <Link href="/import" className="btn btn-primary">+ Importer une vidéo</Link>
      </div>
      <ErrorBox message={stats.error && `API injoignable : ${stats.error}`} />

      <div className="grid grid-stats">
        <StatCard label="Vidéos importées" value={s?.videos ?? "–"} />
        <StatCard label="Clips générés" value={s?.clips_total ?? "–"} />
        <StatCard label="En attente" value={s?.clips_pending ?? "–"} hint="prêts, à valider" />
        <StatCard label="À vérifier" value={s?.clips_needs_review ?? "–"} tone={s?.clips_needs_review ? "warn" : undefined} />
        <StatCard label="Validés" value={s?.clips_validated ?? "–"} tone="good" />
        <StatCard label="Rejetés" value={s?.clips_rejected ?? "–"} />
        <StatCard label="Analyse moyenne" value={s ? `${s.avg_analysis_seconds}s` : "–"} hint={s ? `rendu moyen ${s.avg_render_seconds}s` : undefined} />
        <StatCard label="Erreurs" value={s?.errors.length ?? "–"} tone={s?.errors.length ? "bad" : undefined} />
      </div>

      <div className="grid grid-2" style={{ marginTop: 8 }}>
        <section>
          <h2>Meilleurs moments</h2>
          <div className="card">
            {!s?.best_highlights.length && <p className="muted">Aucun moment détecté pour l&apos;instant.</p>}
            <ul className="list">
              {s?.best_highlights.map((h) => (
                <li key={h.id}>
                  <Link href={`/videos/${h.video_id}`} className="row">
                    <ScoreBadge score={h.score} />
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <CategoryTag category={h.category} />{" "}
                      <span className="muted small">{h.start_ts.slice(0, 8)} · {Math.round(h.duration)}s</span>
                      <div className="small" style={{ marginTop: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{h.transcript}</div>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
          {!!s?.errors.length && (
            <>
              <h2>Erreurs</h2>
              <div className="card">
                <ul className="list">
                  {s.errors.map((e, i) => (
                    <li key={i} className="small">
                      <strong>{e.title ?? e.type ?? e.video_id ?? e.job_id}</strong> — <span className="muted">{e.error}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </>
          )}
        </section>

        <section>
          <h2>Vidéos</h2>
          <div className="card">
            {!videos.data?.length && <p className="muted">Aucune vidéo. Commencez par en importer une.</p>}
            <ul className="list">
              {videos.data?.map((v) => (
                <li key={v.id}>
                  <Link href={`/videos/${v.id}`} className="row spread">
                    <div style={{ minWidth: 0 }}>
                      <strong>{v.title ?? v.id}</strong>
                      <div className="muted small">
                        {fmt(v.duration)} · {v.width}×{v.height} · {v.highlight_count ?? 0} moments · {v.clip_count ?? 0} clips
                        {v.game ? ` · ${v.game.toUpperCase()}` : ""}
                      </div>
                    </div>
                    <StatusPill status={v.status} />
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </section>
      </div>
    </>
  );
}
