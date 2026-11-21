"use client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ChoiceCards, RangeSlider, Segmented, TimeField, fmtTime } from "@/components/controls";
import { useToast } from "@/components/Toast";
import { Alert, CategoryTag, ScoreRing, Spinner, StatusPill } from "@/components/ui";
import { usePoll, waitForJob } from "@/hooks/usePoll";
import { api, Clip, downloadUrl, fileUrl, getToken, SystemInfo, Video } from "@/lib/api";

type Tab = "cut" | "frame" | "text" | "publish";

const LAYOUTS: { value: string; title: string; sub: string; ico: React.ReactNode }[] = [
  { value: "", title: "Auto", sub: "Choisi selon la vidéo", ico: <div className="layout-ico"><div className="phone" style={{ display: "grid", placeItems: "center", fontSize: 18 }}>✨</div></div> },
  { value: "gameplay", title: "Gameplay", sub: "Suit l'action", ico: <div className="layout-ico"><div className="phone"><div className="game" /></div></div> },
  { value: "split", title: "Webcam + jeu", sub: "Facecam en haut", ico: <div className="layout-ico"><div className="phone"><div className="cam" style={{ height: "36%" }} /><div className="game" /></div></div> },
  { value: "streamer", title: "Streamer", sub: "Webcam dominante", ico: <div className="layout-ico"><div className="phone"><div className="cam" style={{ height: "60%" }} /><div className="game" /></div></div> },
  { value: "tracking", title: "Suivi visage", sub: "Plein cadre", ico: <div className="layout-ico"><div className="phone"><div className="cam" style={{ flex: 1 }} /></div></div> },
  { value: "blur_fit", title: "Fond flou", sub: "Image entière", ico: <div className="layout-ico"><div className="phone"><div className="blur"><i /></div></div></div> },
];

const STYLE_CARDS: Record<string, { title: string; preview: React.ReactNode }> = {
  gaming: { title: "Gaming", preview: <div className="sub-preview">C&apos;EST <span style={{ color: "#3DFF6E" }}>FOU</span></div> },
  high_energy: { title: "Énergie", preview: <div className="sub-preview" style={{ fontSize: "1.2rem" }}>NON <span style={{ color: "#FFD400" }}>NON</span></div> },
  clean: { title: "Sobre", preview: <div className="sub-preview" style={{ fontWeight: 600 }}>C&apos;est <span style={{ color: "#FFE14D" }}>fou</span></div> },
};

type Form = { start: number; end: number; layout: string; cropX: number | null; style: string; hook: string; hookDur: number; title: string; tags: string[] };

export default function ClipEditor() {
  const { id } = useParams<{ id: string }>();
  const toast = useToast();
  const router = useRouter();
  const clip = usePoll<Clip>(useCallback(() => api.clip(id), [id]), useCallback((c?: Clip) => c?.status === "rendering", []), 2000);
  const [system, setSystem] = useState<SystemInfo>();
  const [video, setVideo] = useState<Video>();
  const [form, setForm] = useState<Form>();
  const [tab, setTab] = useState<Tab>("cut");
  const [busy, setBusy] = useState("");
  const [newTag, setNewTag] = useState("");
  const [shareFile, setShareFile] = useState<File | null>(null);
  const [savedText, setSavedText] = useState(false);
  const c = clip.data;

  useEffect(() => { api.system().then(setSystem).catch(() => {}); }, []);
  useEffect(() => { if (c?.video_id && !video) api.video(c.video_id).then(setVideo).catch(() => {}); }, [c?.video_id, video]);
  useEffect(() => {
    if (c && !form) {
      setForm({
        start: c.start, end: c.end, layout: c.layout ?? "", cropX: c.crop?.x ?? null, style: c.subtitle_style ?? "gaming",
        hook: c.hook?.text ?? "", hookDur: c.hook?.duration ?? 2.5, title: c.title ?? "", tags: c.hashtags,
      });
    }
  }, [c, form]);

  // Le menu de partage iPhone exige que le fichier soit déjà en mémoire au moment du toucher :
  // on le précharge dès que le clip est prêt.
  const readyKey = c && ["ready", "needs_review", "approved", "exported"].includes(c.status) && c.video_url ? `${c.id}-${c.render_seconds}` : "";
  useEffect(() => {
    if (!readyKey || !c) return;
    let cancelled = false;
    setShareFile(null);
    const token = getToken();
    fetch(downloadUrl(c.id), { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((blob) => {
        if (!cancelled) setShareFile(new File([blob], `${(c.title || "clip").replace(/[^\w\- ]+/g, "").trim().slice(0, 40) || "clip"}.mp4`, { type: "video/mp4" }));
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [readyKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // titre et hashtags : sauvegarde automatique (pas besoin de bouton)
  useEffect(() => {
    if (!c || !form) return;
    if (form.title === (c.title ?? "") && JSON.stringify(form.tags) === JSON.stringify(c.hashtags)) return;
    const t = setTimeout(async () => {
      try {
        const updated = await api.patchClip(c.id, { title: form.title, hashtags: form.tags });
        clip.setData({ ...c, title: updated.title, hashtags: updated.hashtags });
        setSavedText(true);
        setTimeout(() => setSavedText(false), 1500);
      } catch { /* réessayé à la prochaine modification */ }
    }, 700);
    return () => clearTimeout(t);
  }, [form?.title, form?.tags]); // eslint-disable-line react-hooks/exhaustive-deps

  if (c && c.variant === "compilation" && (tab === "cut" || tab === "frame")) setTab("publish");
  if (!c || !form) return clip.error ? <Alert>{clip.error}</Alert> : <div className="skeleton" style={{ height: 420 }} />;
  const isCompilation = c.variant === "compilation";
  const set = <K extends keyof Form>(k: K, val: Form[K]) => setForm({ ...form, [k]: val });
  const h = c.highlight;
  const winMin = Math.max(0, Math.min(form.start, (h?.start ?? c.start) - 20));
  const winMax = Math.min(video?.duration || Infinity, Math.max(form.end, (h?.end ?? c.end) + 20));
  const dirty = !isCompilation && (Math.abs(form.start - c.start) > 0.01 || Math.abs(form.end - c.end) > 0.01 || form.layout !== (c.layout ?? "")
    || form.cropX !== (c.crop?.x ?? null) || form.style !== (c.subtitle_style ?? "gaming") || form.hook !== (c.hook?.text ?? "")
    || form.hookDur !== (c.hook?.duration ?? 2.5));

  async function save(): Promise<Clip | undefined> {
    if (!c || !form) return;
    const body: Record<string, unknown> = { title: form.title, hashtags: form.tags };
    if (!isCompilation) {
      Object.assign(body, { start: form.start, end: form.end, layout: form.layout, subtitle_style: form.style,
        hook_text: form.hook, hook_duration: form.hookDur });
      body.crop = form.cropX == null ? Object.fromEntries(Object.entries(c.crop ?? {}).filter(([k]) => k !== "x")) : { ...c.crop, x: form.cropX };
    }
    try {
      const updated = await api.patchClip(c.id, body);
      clip.setData({ ...c, ...updated });
      return updated;
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function saveAndRender() {
    const saved = await save();
    if (!saved) return;
    if (saved.status !== "pending") { toast("Enregistré"); return; }
    setBusy("render");
    try {
      const { job } = await api.renderClip(saved.id);
      const done = await waitForJob(job, () => clip.reload());
      await clip.reload();
      if (done.status === "failed") toast(done.error?.split("\n")[0] ?? "Échec du rendu", "err");
      else toast("Clip mis à jour");
    } catch (e) {
      toast((e as Error).message, "err");
    }
    setBusy("");
  }

  async function saveVideo() {
    if (!c) return;
    const nav = navigator as Navigator & { canShare?: (d: ShareData) => boolean };
    try {
      if (shareFile && nav.canShare?.({ files: [shareFile] })) {
        // iPhone / Android : menu de partage → « Enregistrer la vidéo » (Photos), TikTok, Instagram…
        await nav.share({ files: [shareFile], title: c.title ?? "Clip" });
      } else {
        window.location.href = downloadUrl(c.id);  // ordinateur : téléchargement classique
      }
      api.exportClip(c.id, true).then((r) => clip.setData({ ...c, status: r.clip.status })).catch(() => {});
    } catch (e) {
      if ((e as Error).name !== "AbortError") toast("Impossible d'ouvrir le partage : téléchargement direct", "err");
      if ((e as Error).name !== "AbortError") window.location.href = downloadUrl(c.id);
    }
  }

  async function setStatus(status: "approved" | "rejected") {
    if (!c) return;
    const updated = await api.patchClip(c.id, { status });
    clip.setData({ ...c, ...updated });
    if (status === "rejected") {
      toast("Clip écarté (visible dans le filtre « Écartés »)");
      router.push(`/videos/${c.video_id}`);
    } else {
      toast("Clip gardé");
    }
  }

  async function remove() {
    if (!c || !window.confirm("Supprimer définitivement ce clip ?")) return;
    try {
      await api.deleteClip(c.id);
      toast("Clip supprimé");
      router.push("/clips");
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); toast("Copié"); } catch { toast("Copie impossible", "err"); }
  }

  const rendering = c.status === "rendering";
  const crop = c.qc?.plan as { layout?: string; dead_air_removed?: number; hd_source?: string } | undefined;

  return (
    <div className="stack-lg">
      <div className="row spread">
        <Link href={`/videos/${c.video_id}`} className="muted small">← Moments de la vidéo</Link>
        <div className="row"><StatusPill status={c.status} />{h && <ScoreRing score={h.score} size="sm" />}</div>
      </div>

      <div className="grid grid-split">
        {/* ---- lecteur ---- */}
        <div className="stack sticky-desk">
          {rendering ? (
            <div className="player player-empty"><Spinner /><strong>{c.qc?.progress ?? "Rendu en cours"}…</strong><span className="tiny muted">Ça prend en général moins d&apos;une minute.</span></div>
          ) : c.video_url ? (
            <video key={c.video_url + c.render_seconds} className="player" src={fileUrl(c.video_url, c.render_seconds)} controls playsInline loop />
          ) : (
            <div className="player player-empty"><span style={{ fontSize: "2rem" }}>🎬</span>Pas encore rendu</div>
          )}
          <div className="meta-row" style={{ justifyContent: "center" }}>
            {h && <CategoryTag category={h.category} />}
            <span className="tag mono">⏱ {c.duration.toFixed(1)} s</span>
            {isCompilation && <span className="tag">🎬 Compilation</span>}
            {!!crop?.dead_air_removed && <span className="tag">✂️ {crop.dead_air_removed} s de blancs coupés</span>}
          </div>
          {!!c.qc?.reasons?.length && <Alert kind="warn">{c.qc.reasons.join(" · ")}</Alert>}
          {!!c.qc?.warnings?.length && <Alert kind="info">{c.qc.warnings.join(" · ")}</Alert>}
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <button className={`btn ${c.status === "approved" ? "btn-primary" : ""}`} onClick={() => setStatus("approved")} title="Marquer comme bon clip">👍 Je garde</button>
            <button className="btn btn-danger" onClick={() => setStatus("rejected")} title="Écarter ce clip">👎 J&apos;écarte</button>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={remove}>🗑 Supprimer ce clip</button>
        </div>

        {/* ---- réglages ---- */}
        <div className="stack">
          <div className="tabs">
            {([["cut", "✂️ Découpe"], ["frame", "🖼 Cadrage"], ["text", "💬 Texte"], ["publish", "📤 Publier"]] as [Tab, string][])
              .filter(([t]) => !isCompilation || t === "publish" || t === "text")
              .map(([t, label]) => <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{label}</button>)}
          </div>

          {tab === "cut" && !isCompilation && (
            <section className="card stack">
              <div className="row spread"><h3>Début et fin</h3><span className="tag mono">{(form.end - form.start).toFixed(1)} s</span></div>
              <RangeSlider min={winMin} max={Number.isFinite(winMax) ? winMax : form.end + 20} step={0.1} start={form.start} end={form.end}
                onChange={(s, e) => setForm({ ...form, start: Math.round(s * 10) / 10, end: Math.round(e * 10) / 10 })} />
              <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
                <TimeField label="Début" precise value={form.start} steps={[-1, -0.5, 0.5, 1]}
                  onChange={(v) => set("start", Math.min(Math.round(v * 10) / 10, form.end - 3))} />
                <TimeField label="Fin" precise value={form.end} max={video?.duration} steps={[-1, -0.5, 0.5, 1]}
                  onChange={(v) => set("end", Math.max(Math.round(v * 10) / 10, form.start + 3))} />
              </div>
              <p className="hint">Les blancs (pauses calmes) sont coupés automatiquement au rendu.</p>
            </section>
          )}

          {tab === "frame" && !isCompilation && (
            <section className="card stack">
              <h3>Cadrage vertical</h3>
              <ChoiceCards value={form.layout} onChange={(v) => set("layout", v)}
                options={LAYOUTS.filter((l) => !system || l.value === "" || system.layouts.includes(l.value)).map((l) => ({ value: l.value, title: l.title, sub: l.sub, visual: l.ico }))} />
              {["", "gameplay", "tracking", "split"].includes(form.layout) && (
                <div>
                  <div className="row spread">
                    <span className="label">Position horizontale</span>
                    <Segmented value={form.cropX == null ? "auto" : "manual"} onChange={(v) => set("cropX", v === "auto" ? null : 0.5)}
                      options={[{ value: "auto", label: "Auto" }, { value: "manual", label: "Manuel" }]} />
                  </div>
                  {form.cropX != null && (
                    <>
                      <input className="single-range" type="range" min={0} max={1} step={0.01} value={form.cropX} onChange={(e) => set("cropX", Number(e.target.value))} />
                      <div className="row spread tiny muted"><span>◀ gauche</span><span>{Math.round(form.cropX * 100)} %</span><span>droite ▶</span></div>
                    </>
                  )}
                </div>
              )}
            </section>
          )}

          {tab === "text" && (
            <section className="card stack">
              {!isCompilation && (
                <div>
                  <span className="label">Style des sous-titres</span>
                  <ChoiceCards value={form.style} onChange={(v) => set("style", v)}
                    options={(system?.subtitle_presets ?? Object.keys(STYLE_CARDS)).map((p) => ({ value: p, title: STYLE_CARDS[p]?.title ?? p, visual: STYLE_CARDS[p]?.preview }))} />
                </div>
              )}
              <div>
                <span className="label">Hook (accroche des premières secondes)</span>
                <input className="input" value={form.hook} onChange={(e) => set("hook", e.target.value)} disabled={isCompilation} maxLength={60} />
                {!!c.hook?.alternatives?.length && !isCompilation && (
                  <div className="chips" style={{ marginTop: 10 }}>
                    {c.hook.alternatives.map((alt) => (
                      <button key={alt} className={`chip ${alt === form.hook ? "active" : ""}`} onClick={() => set("hook", alt)}>{alt}</button>
                    ))}
                  </div>
                )}
              </div>
              {!isCompilation && (
                <div>
                  <span className="label">Durée du hook</span>
                  <Segmented value={form.hookDur} onChange={(v) => set("hookDur", v)}
                    options={[{ value: 1.5, label: "1,5 s" }, { value: 2.5, label: "2,5 s" }, { value: 3.5, label: "3,5 s" }]} />
                </div>
              )}
            </section>
          )}

          {tab === "publish" && (
            <section className="card stack">
              <div>
                <span className="label">Titre</span>
                <input className="input" value={form.title} onChange={(e) => set("title", e.target.value)} />
                <div className="chips" style={{ marginTop: 10 }}>
                  {c.titles.map((t) => <button key={t} className={`chip ${t === form.title ? "active" : ""}`} onClick={() => set("title", t)}>{t}</button>)}
                </div>
              </div>
              <div>
                <span className="label">Hashtags <span className="muted">({form.tags.length}/10) · touchez pour retirer</span></span>
                <div className="chips">
                  {form.tags.map((t) => (
                    <button key={t} className="chip active" onClick={() => set("tags", form.tags.filter((x) => x !== t))}>{t}<span className="x">✕</span></button>
                  ))}
                </div>
                {form.tags.length < 10 && (
                  <div className="input-group" style={{ marginTop: 10 }}>
                    <input className="input" placeholder="#ajouter" value={newTag} onChange={(e) => setNewTag(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && newTag.trim()) {
                          const tag = "#" + newTag.trim().replace(/^#/, "").replace(/\s+/g, "");
                          if (!form.tags.includes(tag)) set("tags", [...form.tags, tag]);
                          setNewTag("");
                        }
                      }} />
                  </div>
                )}
              </div>
              <div>
                <span className="label">Captions prêtes à copier</span>
                <div className="stack-sm">
                  {c.captions.map((cap) => (
                    <button key={cap} className="card-flat row-nowrap" style={{ border: 0, textAlign: "left", cursor: "pointer" }} onClick={() => copy(cap)}>
                      <span className="grow small">{cap}</span><span className="tag">📋</span>
                    </button>
                  ))}
                </div>
              </div>
              {savedText && <p className="hint">✓ Titre et hashtags enregistrés</p>}
            </section>
          )}

          {h && tab === "cut" && (
            <details className="card adv">
              <summary>🧠 Pourquoi ce moment ?</summary>
              <div className="stack-sm" style={{ marginTop: 12 }}>
                <p className="small">{h.reason}</p>
                {Object.entries(h.components).map(([k, val]) => (
                  <div key={k} className="comp-row"><span>{k.replace(/_/g, " ")}</span><div className="bar"><span style={{ width: `${Math.round(val * 100)}%` }} /></div><span className="mono">{Math.round(val * 100)}</span></div>
                ))}
              </div>
            </details>
          )}

          <div className="action-bar">
            {dirty ? (
              <button className="btn btn-primary btn-block" disabled={!!busy || rendering} onClick={saveAndRender}>
                {busy === "render" ? <><Spinner /> Rendu…</> : "✨ Appliquer les changements"}
              </button>
            ) : (
              <>
                <button className="btn btn-primary grow" disabled={rendering || !c.video_url} onClick={saveVideo}>
                  {rendering ? <><Spinner /> Rendu…</> : "📥 Enregistrer la vidéo"}
                </button>
                {!!c.captions.length && (
                  <button className="btn icon-btn" title="Copier la légende" onClick={() => copy(c.captions[0])}>📋</button>
                )}
              </>
            )}
          </div>
          <p className="hint" style={{ textAlign: "center" }}>
            {dirty ? `Modifications à appliquer · ${fmtTime(form.start)} → ${fmtTime(form.end)}`
              : "Sur iPhone : choisissez « Enregistrer la vidéo » dans le menu pour l'ajouter à Photos. 📋 copie la légende."}
          </p>
        </div>
      </div>
    </div>
  );
}
