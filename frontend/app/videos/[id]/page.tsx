"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import { CategoryTag, ErrorBox, ScoreBadge, StatusPill } from "@/components/ui";
import { usePoll, waitForJob } from "@/hooks/usePoll";
import { api, API_URL, fileUrl, fmt, Highlight, Video } from "@/lib/api";

export default function VideoPage() {
  const { id } = useParams<{ id: string }>();
  const video = usePoll<Video>(
    useCallback(() => api.video(id), [id]),
    useCallback((v?: Video) => !v || ["queued", "analyzing", "imported"].includes(v.status) || v.last_job?.status === "running", []),
    2500,
  );
  const highlights = usePoll<Highlight[]>(useCallback(() => api.highlights(id), [id]), () => false);
  const [busy, setBusy] = useState<Record<string, string>>({});
  const [error, setError] = useState<string>();
  const [preview, setPreview] = useState<Highlight | null>(null);
  const previewRef = useRef<HTMLVideoElement>(null);
  const v = video.data;
  const lastStatus = useRef<string>(undefined);
  if (v && v.status !== lastStatus.current) {
    if (lastStatus.current && v.status === "analyzed") highlights.reload();
    lastStatus.current = v.status;
  }

  async function generate(h: Highlight) {
    setBusy((b) => ({ ...b, [h.id]: "Génération…" }));
    try {
      const { job } = await api.generate(h.id);
      const done = await waitForJob(job, (j) => setBusy((b) => ({ ...b, [h.id]: j.status === "running" ? "Rendu en cours…" : "En file…" })));
      if (done.status === "failed") setError(done.error ?? "Échec de génération");
      await highlights.reload();
    } catch (e) {
      setError((e as Error).message);
    }
    setBusy((b) => ({ ...b, [h.id]: "" }));
  }

  async function reject(h: Highlight) {
    await api.rejectHighlight(h.id);
    highlights.reload();
  }

  async function reanalyze() {
    await api.analyze(id, true);
    video.reload();
  }

  function openPreview(h: Highlight) {
    setPreview(h);
    setTimeout(() => {
      const el = previewRef.current;
      if (el) {
        el.currentTime = h.start;
        el.play().catch(() => {});
      }
    }, 50);
  }

  return (
    <>
      <ErrorBox message={video.error || error} />
      {v && (
        <div className="row spread">
          <div>
            <h1>{v.title ?? v.id}</h1>
            <p className="muted">
              {fmt(v.duration)} · {v.width}×{v.height} · {v.fps} fps{v.game ? ` · jeu détecté : ${v.game.toUpperCase()}` : ""}
              {v.language ? ` · langue : ${v.language}` : ""}
              {v.stats?.analysis_timings?.total ? ` · analysé en ${v.stats.analysis_timings.total}s` : ""}
            </p>
          </div>
          <div className="row">
            <StatusPill status={v.status} />
            <button className="btn btn-sm" onClick={reanalyze}>Ré-analyser</button>
          </div>
        </div>
      )}
      {v?.status === "failed" && <ErrorBox message={v.error ?? "Échec"} />}
      {v && ["queued", "analyzing"].includes(v.status) && (
        <div className="card"><strong>Analyse en cours…</strong> <span className="muted">extraction audio, transcription, détection des moments.</span></div>
      )}

      {preview && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="row spread">
            <strong>Aperçu {preview.start_ts.slice(0, 8)} → {preview.end_ts.slice(0, 8)}</strong>
            <button className="btn btn-sm" onClick={() => setPreview(null)}>Fermer</button>
          </div>
          <video
            ref={previewRef}
            className="preview"
            style={{ marginTop: 10 }}
            src={`${API_URL}/videos/${id}/source`}
            controls
            onTimeUpdate={(e) => {
              if (e.currentTarget.currentTime >= preview.end) e.currentTarget.pause();
            }}
          />
        </div>
      )}

      <h2>Meilleurs moments ({highlights.data?.length ?? 0})</h2>
      {v?.status === "analyzed" && !highlights.data?.length && <p className="muted">Aucun moment au-dessus du seuil. Essayez le scoring « agressif ».</p>}
      <div className="grid grid-cards">
        {highlights.data?.map((h) => {
          const main = h.clips?.find((c) => c.variant === "main") ?? h.clips?.[0];
          return (
            <div key={h.id} className="card hl-card" style={{ opacity: h.status === "rejected" ? 0.5 : 1 }}>
              <div className="hl-media">
                {h.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img className="thumb" src={fileUrl(h.thumbnail_url)} alt="" />
                ) : (
                  <div className="thumb">{h.start_ts.slice(3, 8)}</div>
                )}
                <div style={{ minWidth: 0 }}>
                  <div className="row"><ScoreBadge score={h.score} /><CategoryTag category={h.category} /></div>
                  <div className="muted small" style={{ marginTop: 8 }}>
                    {h.start_ts.slice(0, 8)} → {h.end_ts.slice(0, 8)} · {Math.round(h.duration)}s · confiance {Math.round(h.confidence * 100)}%
                  </div>
                  <p className="small" style={{ margin: "8px 0 0" }}>{h.reason}</p>
                </div>
              </div>
              {h.transcript && <div className="transcript">« {h.transcript} »</div>}
              <div className="row">
                <button className="btn btn-sm" onClick={() => openPreview(h)}>Preview</button>
                <button className="btn btn-sm btn-primary" disabled={!!busy[h.id]} onClick={() => generate(h)}>
                  {busy[h.id] || (h.clips?.length ? "Regénérer" : "Generate Clip")}
                </button>
                {h.status !== "rejected" && <button className="btn btn-sm btn-danger" onClick={() => reject(h)}>Reject</button>}
                {main && <Link className="btn btn-sm" href={`/clips/${main.id}`}>Edit</Link>}
              </div>
              {!!h.clips?.length && (
                <div className="chips">
                  {h.clips.map((c) => (
                    <Link key={c.id} href={`/clips/${c.id}`} className="chip">{c.variant} · <StatusPill status={c.status} /></Link>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}
