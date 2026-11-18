"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { CategoryTag, ErrorBox, ScoreBadge, StatusPill } from "@/components/ui";
import { usePoll, waitForJob } from "@/hooks/usePoll";
import { api, Clip, fileUrl, SystemInfo } from "@/lib/api";

type Form = { start: string; end: string; layout: string; cropX: string; subtitle_style: string; hook_text: string; title: string; hashtags: string };

export default function ClipEditor() {
  const { id } = useParams<{ id: string }>();
  const clip = usePoll<Clip>(useCallback(() => api.clip(id), [id]), useCallback((c?: Clip) => c?.status === "rendering", []), 2500);
  const [system, setSystem] = useState<SystemInfo>();
  const [form, setForm] = useState<Form>();
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<string>();
  const [error, setError] = useState<string>();
  const c = clip.data;

  useEffect(() => {
    api.system().then(setSystem).catch(() => {});
  }, []);

  useEffect(() => {
    if (c && !form) {
      setForm({
        start: c.start.toFixed(2), end: c.end.toFixed(2), layout: c.layout ?? "", cropX: c.crop?.x?.toString() ?? "",
        subtitle_style: c.subtitle_style ?? "gaming", hook_text: c.hook?.text ?? "", title: c.title ?? "", hashtags: c.hashtags.join(" "),
      });
    }
  }, [c, form]);

  if (!c || !form) return <ErrorBox message={clip.error} />;
  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setForm({ ...form, [k]: e.target.value });

  async function save(): Promise<Clip | undefined> {
    if (!c || !form) return;
    setError(undefined);
    try {
      const body: Record<string, unknown> = {
        start: Number(form.start), end: Number(form.end), layout: form.layout, subtitle_style: form.subtitle_style,
        hook_text: form.hook_text, title: form.title, hashtags: form.hashtags.split(/\s+/).filter(Boolean),
      };
      if (form.cropX !== "") body.crop = { ...c.crop, x: Number(form.cropX) };
      const updated = await api.patchClip(c.id, body);
      clip.setData({ ...c, ...updated });
      setMsg(updated.status === "pending" ? "Modifications enregistrées — relancez le rendu pour les voir." : "Enregistré.");
      return updated;
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function rerender() {
    const saved = await save();
    if (!saved) return;
    setBusy("Rendu en cours…");
    try {
      const { job } = await api.renderClip(saved.id);
      const done = await waitForJob(job);
      if (done.status === "failed") setError(done.error ?? "Échec du rendu");
      await clip.reload();
      setMsg("Rendu terminé.");
    } catch (e) {
      setError((e as Error).message);
    }
    setBusy("");
  }

  async function exportClip(force = false) {
    if (!c) return;
    setBusy("Export…");
    setError(undefined);
    try {
      const res = await api.exportClip(c.id, force);
      clip.setData({ ...c, ...res.clip });
      setMsg(`Exporté : ${res.path}`);
    } catch (e) {
      setError((e as Error).message);
    }
    setBusy("");
  }

  async function setStatus(status: "approved" | "rejected") {
    if (!c) return;
    const updated = await api.patchClip(c.id, { status });
    clip.setData({ ...c, ...updated });
  }

  const h = c.highlight;
  return (
    <>
      <div className="row spread">
        <div>
          <h1>Éditeur de clip</h1>
          <p className="muted">
            <Link href={`/videos/${c.video_id}`}>← vidéo</Link> · variante {c.variant} · {c.duration.toFixed(1)}s
            {c.render_seconds ? ` · rendu en ${c.render_seconds}s` : ""}
          </p>
        </div>
        <div className="row"><StatusPill status={c.status} />{h && <><ScoreBadge score={h.score} /><CategoryTag category={h.category} /></>}</div>
      </div>
      <ErrorBox message={error} />
      {msg && <div className="ok-box">{msg}</div>}
      {!!c.qc?.reasons?.length && <ErrorBox message={`Contrôle qualité : ${c.qc.reasons.join(" · ")}`} />}

      <div className="grid grid-2">
        <div>
          {c.video_url ? (
            <video key={c.video_url + c.render_seconds} className="player" src={fileUrl(c.video_url, c.render_seconds)} controls playsInline />
          ) : (
            <div className="player thumb">Pas encore rendu</div>
          )}
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn btn-sm" onClick={() => setStatus("approved")}>✓ Valider</button>
            <button className="btn btn-sm btn-danger" onClick={() => setStatus("rejected")}>✕ Rejeter</button>
            {c.metadata_url && <a className="btn btn-sm" href={fileUrl(c.metadata_url)} target="_blank">metadata.json</a>}
          </div>
        </div>

        <div className="card">
          <div className="fields">
            <div><label>Début (s)</label><input type="number" step="0.1" value={form.start} onChange={set("start")} /></div>
            <div><label>Fin (s)</label><input type="number" step="0.1" value={form.end} onChange={set("end")} /></div>
            <div>
              <label>Cadrage</label>
              <select value={form.layout} onChange={set("layout")}>
                <option value="">auto</option>
                {(system?.layouts ?? []).filter((l) => l !== "auto").map((l) => <option key={l}>{l}</option>)}
              </select>
            </div>
            <div>
              <label>Sous-titres</label>
              <select value={form.subtitle_style} onChange={set("subtitle_style")}>
                {(system?.subtitle_presets ?? [form.subtitle_style]).map((p) => <option key={p}>{p}</option>)}
              </select>
            </div>
          </div>
          <div className="field">
            <label>Position horizontale du crop {form.cropX === "" ? "(automatique)" : `(${Math.round(Number(form.cropX) * 100)} %)`}</label>
            <div className="row">
              <input type="range" min={0} max={1} step={0.01} value={form.cropX === "" ? 0.5 : form.cropX} onChange={set("cropX")} style={{ flex: 1 }} />
              <button className="btn btn-sm" onClick={() => setForm({ ...form, cropX: "" })}>Auto</button>
            </div>
          </div>
          <div className="field">
            <label>Hook (2-3 premières secondes)</label>
            <input value={form.hook_text} onChange={set("hook_text")} />
            <div className="chips" style={{ marginTop: 6 }}>
              {c.hook?.alternatives?.map((alt) => (
                <button key={alt} className={`chip ${alt === form.hook_text ? "active" : ""}`} onClick={() => setForm({ ...form, hook_text: alt })}>{alt}</button>
              ))}
            </div>
          </div>
          <div className="field">
            <label>Titre</label>
            <input value={form.title} onChange={set("title")} />
            <div className="chips" style={{ marginTop: 6 }}>
              {c.titles.map((t) => (
                <button key={t} className={`chip ${t === form.title ? "active" : ""}`} onClick={() => setForm({ ...form, title: t })}>{t}</button>
              ))}
            </div>
          </div>
          <div className="field">
            <label>Hashtags</label>
            <input value={form.hashtags} onChange={set("hashtags")} />
          </div>
          <div className="field">
            <label>Captions proposées</label>
            <ul className="list small">{c.captions.map((cap) => <li key={cap}>{cap}</li>)}</ul>
          </div>
          <div className="row">
            <button className="btn" onClick={save} disabled={!!busy}>Enregistrer</button>
            <button className="btn" onClick={rerender} disabled={!!busy}>{busy === "Rendu en cours…" ? busy : "Enregistrer + rendre"}</button>
            <button className="btn btn-primary" onClick={() => exportClip(false)} disabled={!!busy}>Exporter</button>
            {c.status === "needs_review" && <button className="btn btn-sm" onClick={() => exportClip(true)} disabled={!!busy}>Exporter quand même</button>}
          </div>
        </div>
      </div>

      {h && (
        <>
          <h2>Pourquoi ce moment ?</h2>
          <div className="card">
            <p style={{ marginTop: 0 }}>{h.reason}</p>
            <div className="grid" style={{ gap: 6 }}>
              {Object.entries(h.components).map(([k, val]) => (
                <div key={k} className="comp-row">
                  <span>{k}</span>
                  <div className="bar"><span style={{ width: `${Math.round(val * 100)}%` }} /></div>
                  <span>{Math.round(val * 100)}</span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </>
  );
}
