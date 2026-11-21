"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { fmtTime } from "@/components/controls";
import { Alert, CategoryTag, Empty, ScoreRing, StatCard, StatusPill } from "@/components/ui";
import { usePoll } from "@/hooks/usePoll";
import { api, Stats, SystemInfo, Video } from "@/lib/api";

const ACTIVE = ["downloading", "queued", "analyzing"];

export default function Dashboard() {
  const stats = usePoll<Stats>(api.stats, () => true, 6000);
  const videos = usePoll<Video[]>(api.videos, useCallback((d?: Video[]) => !!d?.some((v) => ACTIVE.includes(v.status)), []), 3000);
  const s = stats.data;
  const list = videos.data ?? [];
  const [system, setSystem] = useState<SystemInfo>();
  useEffect(() => { api.system().then(setSystem).catch(() => {}); }, []);
  const storage = system?.storage;

  return (
    <div className="stack-lg">
      <section className="card card-lg hero stack">
        <span className="eyebrow">GamingClips Factory</span>
        <h1>Tes meilleurs moments,<br />prêts pour TikTok.</h1>
        <p className="text-2 small" style={{ maxWidth: 460 }}>Un lien ou un fichier → les moments forts, recadrés en vertical, sous-titrés, avec hook et hashtags.</p>
        <div className="row" style={{ position: "relative", zIndex: 1 }}>
          <Link href="/import" className="btn btn-primary btn-lg">✨ Nouvelle vidéo</Link>
          <Link href="/clips" className="btn btn-lg">Mes clips</Link>
        </div>
      </section>

      <Alert>{stats.error && `Serveur injoignable : ${stats.error}`}</Alert>
      {storage?.persistent === false && (
        <Alert kind="error">
          <strong>Stockage non permanent :</strong> vidéos et clips sont effacés à chaque redéploiement du serveur.
          Sur Railway : clic droit sur le service (ou bouton +) → <em>Volume</em> → point de montage <code>/data</code>,
          avec les variables GCF_DATA_DIR=/data/data et GCF_OUTPUT_DIR=/data/output.
        </Alert>
      )}
      {storage && storage.free_gb < 3 && (
        <Alert kind="warn">Plus que {storage.free_gb} Go libres sur le serveur : agrandissez le volume ou supprimez d&apos;anciennes vidéos.</Alert>
      )}

      <div className="grid grid-stats">
        <StatCard label="Vidéos" value={s?.videos ?? "–"} />
        <StatCard label="Clips créés" value={s?.clips_total ?? "–"} />
        <StatCard label="À vérifier" value={s?.clips_needs_review ?? "–"} tone={s?.clips_needs_review ? "warn" : undefined} />
        <StatCard label="Validés / exportés" value={s?.clips_validated ?? "–"} tone="good" />
      </div>

      <section>
        <div className="section-title"><h2>Vidéos récentes</h2></div>
        {!videos.data ? (
          <div className="stack-sm">{[0, 1].map((i) => <div key={i} className="skeleton" style={{ height: 76 }} />)}</div>
        ) : list.length === 0 ? (
          <Empty emoji="🎬" title="Aucune vidéo pour l'instant">
            <p className="muted small">Importe ton premier stream pour générer des clips.</p>
            <Link href="/import" className="btn btn-primary">Importer une vidéo</Link>
          </Empty>
        ) : (
          <div className="stack-sm">
            {list.slice(0, 8).map((v) => {
              const p = v.stats?.progress;
              return (
                <Link key={v.id} href={`/videos/${v.id}`} className="card card-link stack-sm">
                  <div className="row-nowrap spread">
                    <strong className="clamp-1 grow">{v.title ?? v.id}</strong>
                    <StatusPill status={v.status} />
                  </div>
                  <div className="meta-row tiny muted">
                    {v.duration ? <span className="mono">⏱ {fmtTime(v.duration)}</span> : null}
                    <span>· {v.highlight_count ?? 0} moments</span>
                    <span>· {v.clip_count ?? 0} clips</span>
                    {v.game && <span>· 🎮 {v.game.toUpperCase()}</span>}
                  </div>
                  {ACTIVE.includes(v.status) && (
                    <div className={`bar ${p?.pct == null ? "indeterminate" : ""}`}><span style={{ width: `${p?.pct ?? 30}%` }} /></div>
                  )}
                  {v.status === "failed" && v.error && <p className="tiny" style={{ color: "var(--bad)" }}>{v.error.slice(0, 140)}</p>}
                </Link>
              );
            })}
          </div>
        )}
      </section>

      {!!s?.best_highlights.length && (
        <section>
          <div className="section-title"><h2>🔥 Meilleurs moments</h2></div>
          <div className="stack-sm">
            {s.best_highlights.map((h) => (
              <Link key={h.id} href={`/videos/${h.video_id}`} className="card card-link row-nowrap">
                <ScoreRing score={h.score} size="sm" />
                <div className="grow stack-sm" style={{ gap: 4 }}>
                  <div className="row"><CategoryTag category={h.category} /><span className="tiny muted mono">{(h.source_offset ? h.source_start_ts : h.start_ts)?.slice(0, 8)} · {Math.round(h.duration)} s</span></div>
                  <p className="small text-2 clamp-1">{h.transcript || h.reason}</p>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {!!s?.errors.length && (
        <details className="card adv">
          <summary>⚠️ {s.errors.length} erreur(s) récente(s)</summary>
          <ul className="stack-sm small" style={{ margin: "12px 0 0", paddingLeft: 18 }}>
            {s.errors.map((e, i) => (
              <li key={i}><strong>{e.title ?? e.type ?? e.video_id ?? e.job_id}</strong> — <span className="muted">{e.error}</span></li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
