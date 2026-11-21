"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import { fmtTime } from "@/components/controls";
import { useToast } from "@/components/Toast";
import { Alert, CategoryTag, Empty, ScoreRing, Spinner, StatusPill } from "@/components/ui";
import { usePoll, waitForJob } from "@/hooks/usePoll";
import { api, fileUrl, Highlight, Video } from "@/lib/api";

const ACTIVE = ["downloading", "queued", "analyzing", "imported"];
const STEPS = ["Téléchargement", "Analyse de l'image et du son", "Transcription", "Choix des moments"];

function stepIndex(v: Video): number {
  if (v.status === "downloading") return 0;
  if (v.status === "queued" || v.status === "imported") return 1;
  const s = v.stats?.progress?.step ?? "";
  if (s.startsWith("Transcription")) return 2;
  if (s.startsWith("Choix") || s.startsWith("Détection")) return 3;
  return 1;
}

export default function VideoPage() {
  const { id } = useParams<{ id: string }>();
  const toast = useToast();
  const video = usePoll<Video>(useCallback(() => api.video(id), [id]),
    useCallback((v?: Video) => !v || ACTIVE.includes(v.status) || v.last_job?.status === "running", []), 2500);
  const highlights = usePoll<Highlight[]>(useCallback(() => api.highlights(id), [id]), () => false);
  const [busy, setBusy] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<Highlight | null>(null);
  const [openText, setOpenText] = useState<Record<string, boolean>>({});
  const previewRef = useRef<HTMLVideoElement>(null);
  const v = video.data;
  const last = useRef<string>(undefined);
  if (v && v.status !== last.current) {
    if (last.current && v.status === "analyzed") {
      highlights.reload();
      toast("Analyse terminée");
    }
    last.current = v.status;
  }

  async function run(key: string, label: string, fn: () => Promise<void>) {
    setBusy((b) => ({ ...b, [key]: label }));
    try {
      await fn();
    } catch (e) {
      toast((e as Error).message, "err");
    }
    setBusy((b) => ({ ...b, [key]: "" }));
  }

  const generate = (h: Highlight) => run(h.id, "En file…", async () => {
    const { job } = await api.generate(h.id);
    const done = await waitForJob(job, (j) => setBusy((b) => ({ ...b, [h.id]: j.status === "running" ? "Rendu…" : "En file…" })));
    if (done.status === "failed") throw new Error(done.error?.split("\n")[0] ?? "Échec du rendu");
    await highlights.reload();
    toast("Clips prêts");
  });

  const compilation = () => run("compilation", "En file…", async () => {
    const { job } = await api.compilation(id, 5, 10);
    const done = await waitForJob(job, (j) => setBusy((b) => ({ ...b, compilation: j.status === "running" ? "Montage…" : "En file…" })));
    if (done.status === "failed") throw new Error(done.error?.split("\n")[0] ?? "Échec de la compilation");
    window.location.href = `/clips/${(done as unknown as { result: { clip: string } }).result.clip}`;
  });

  const reject = (h: Highlight) => run(`r${h.id}`, "…", async () => {
    await api.rejectHighlight(h.id);
    await highlights.reload();
  });

  const reanalyze = () => run("re", "…", async () => {
    await api.analyze(id, true);
    await video.reload();
    toast("Nouvelle analyse lancée");
  });

  function openPreview(h: Highlight) {
    setPreview(h);
    setTimeout(() => {
      const el = previewRef.current;
      if (el) {
        el.currentTime = h.start;
        el.play().catch(() => {});
      }
    }, 80);
  }

  if (!v) return video.error ? <Alert>{video.error}</Alert> : <div className="skeleton" style={{ height: 160 }} />;
  const hls = highlights.data ?? [];
  const cover = hls.find((h) => h.thumbnail_url)?.thumbnail_url;
  const active = ACTIVE.includes(v.status);
  const cur = stepIndex(v);

  return (
    <div className="stack-lg">
      <Link href="/" className="muted small">← Tableau de bord</Link>

      <section className="card card-lg stack">
        <div className="row-nowrap" style={{ alignItems: "flex-start", gap: 14 }}>
          {cover && /* eslint-disable-next-line @next/next/no-img-element */ <img src={fileUrl(cover)} alt="" style={{ width: 64, borderRadius: 10, aspectRatio: "9/16", objectFit: "cover" }} />}
          <div className="grow stack-sm">
            <h1 style={{ fontSize: "1.3rem" }} className="clamp-2">{v.title ?? v.id}</h1>
            <div className="meta-row">
              <StatusPill status={v.status} />
              {v.duration ? <span className="tag mono">⏱ {fmtTime(v.duration)}</span> : null}
              {v.game && <span className="tag">🎮 {v.game.toUpperCase()}</span>}
              {v.language && <span className="tag">🗣 {v.language.toUpperCase()}</span>}
              {v.options?.remote_hd ? <span className="tag" title="Analyse sur une version légère, clips rendus en HD">⚡ HD au rendu</span> : null}
              {v.stats?.download && v.stats.download.reused_seconds > 0 ? (
                <span className="tag" title="Déjà téléchargé pour ce lien : rien à retélécharger">
                  ♻️ {fmtTime(v.stats.download.reused_seconds)} réutilisés
                </span>
              ) : null}
            </div>
          </div>
        </div>

        {active && (
          <div className="card-flat stack">
            <div className="row spread">
              <strong>{v.status === "queued" ? "En file d'attente…" : `${v.stats?.progress?.step ?? STEPS[cur]}…`}</strong>
              {v.stats?.progress?.pct != null && <span className="mono small muted">{v.stats.progress.pct} %</span>}
            </div>
            <div className={`bar ${v.stats?.progress?.pct == null ? "indeterminate" : ""}`}><span style={{ width: `${v.stats?.progress?.pct ?? 30}%` }} /></div>
            <ul className="steps">
              {STEPS.map((label, i) => (
                <li key={label} className={i < cur ? "done" : i === cur ? "current" : ""}>
                  <span className="dot">{i < cur ? "✓" : i + 1}</span>{label}
                </li>
              ))}
            </ul>
            <p className="tiny muted">Vous pouvez fermer cette page : le traitement continue sur le serveur.</p>
          </div>
        )}
        {v.status === "failed" && <Alert>{v.error ?? "Échec"}</Alert>}

        <div className="row">
          {hls.length >= 2 && (
            <button className="btn btn-primary" disabled={!!busy.compilation} onClick={compilation}>
              {busy.compilation ? <><Spinner /> {busy.compilation}</> : "🎬 Compilation top 5"}
            </button>
          )}
          {v.source_url && (
            <Link className="btn btn-sm" href={`/import?url=${encodeURIComponent(v.source_url)}&from=${Math.round(Number(v.options?.section_end ?? 0))}`}>
              ➕ Analyser une autre partie
            </Link>
          )}
          <button className="btn btn-ghost btn-sm" disabled={!!busy.re || active} onClick={reanalyze}>↻ Ré-analyser</button>
        </div>
      </section>

      <section>
        <div className="section-title">
          <h2>Moments forts {hls.length ? <span className="muted">({hls.length})</span> : null}</h2>
          {v.stats?.picker && <span className="tag">{v.stats.picker === "llm" ? "🧠 choisis par l'IA" : "⚙️ règles automatiques"}</span>}
        </div>
        {v.status === "analyzed" && !hls.length && (
          <Empty emoji="🔍" title="Aucun moment assez fort">
            <p className="muted small">Essayez une sélection « Large » à l&apos;import, ou une autre partie de la vidéo.</p>
          </Empty>
        )}
        <div className="grid grid-cards">
          {hls.map((h) => {
            const main = h.clips?.find((c) => c.variant === "main") ?? h.clips?.[0];
            const start = h.source_offset ? h.source_start_ts! : h.start_ts;
            return (
              <article key={h.id} className="card moment" style={{ opacity: h.status === "rejected" ? 0.45 : 1 }}>
                <div className="moment-head">
                  <ScoreRing score={h.score} />
                  <div className="grow stack-sm" style={{ gap: 6 }}>
                    <CategoryTag category={h.category} />
                    <span className="tiny muted mono">▶ {start.slice(0, 8)} · {Math.round(h.duration)} s · confiance {Math.round(h.confidence * 100)} %</span>
                  </div>
                </div>
                <p className="small">{h.reason}</p>
                {h.transcript && (
                  <button type="button" className="quote" style={{ textAlign: "left", cursor: "pointer", border: 0 }}
                    onClick={() => setOpenText((o) => ({ ...o, [h.id]: !o[h.id] }))}>
                    <span className={openText[h.id] ? "" : "clamp-2"}>« {h.transcript} »</span>
                  </button>
                )}
                {(h.clips?.length ?? 0) > 1 && (
                  <div className="chips">
                    {(h.clips ?? []).map((c) => (
                      <Link key={c.id} href={`/clips/${c.id}`} className="chip">
                        {c.variant === "main" ? "🎞 Principal" : c.variant === "alt" ? "🎨 Variante" : c.variant === "compilation" ? "🎬 Compilation" : c.variant}
                        {c.status !== "ready" && <StatusPill status={c.status} />}
                      </Link>
                    ))}
                  </div>
                )}
                <div className="moment-actions">
                  <button className="btn btn-sm" onClick={() => openPreview(h)}>▶ Aperçu</button>
                  {main ? (
                    <Link className="btn btn-sm btn-primary" href={`/clips/${main.id}`}>✏️ Éditer le clip</Link>
                  ) : (
                    <button className="btn btn-sm btn-primary" disabled={!!busy[h.id] || h.status === "rejected"} onClick={() => generate(h)}>
                      {busy[h.id] ? <><Spinner /> {busy[h.id]}</> : "⚡ Créer le clip"}
                    </button>
                  )}
                  {h.status !== "rejected" && (
                    <button className="btn btn-sm btn-ghost icon-btn" title="Rejeter ce moment" onClick={() => reject(h)}>✕</button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {preview && (
        <div className="sheet-backdrop" onClick={() => setPreview(null)}>
          <div className="sheet stack" onClick={(e) => e.stopPropagation()}>
            <div className="row spread">
              <strong>Aperçu · {(preview.source_offset ? preview.source_start_ts! : preview.start_ts).slice(0, 8)} ({Math.round(preview.duration)} s)</strong>
              <button className="btn btn-ghost icon-btn" onClick={() => setPreview(null)}>✕</button>
            </div>
            <video ref={previewRef} className="preview" src={fileUrl(`/videos/${id}/source`)} controls playsInline
              onTimeUpdate={(e) => { if (e.currentTarget.currentTime >= preview.end) e.currentTarget.pause(); }} />
            {!preview.clips?.length && (
              <button className="btn btn-primary btn-block" onClick={() => { setPreview(null); generate(preview); }}>⚡ Créer ce clip</button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
