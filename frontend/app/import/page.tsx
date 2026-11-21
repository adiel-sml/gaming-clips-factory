"use client";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ErrorBox } from "@/components/ui";
import { api, SystemInfo } from "@/lib/api";

export default function ImportPage() {
  const router = useRouter();
  const [system, setSystem] = useState<SystemInfo>();
  const [source, setSource] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState(false);
  const [mode, setMode] = useState<"analyze" | "pipeline">("analyze");
  const [range, setRange] = useState({ start: "", end: "" });
  const [opts, setOpts] = useState({
    profile: "stream", max_video_duration: "", clips_per_video: "10", min_clip_duration: "", max_clip_duration: "",
    language: "auto", subtitle_style: "gaming", aggressiveness: "normal",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    api.system().then(setSystem).catch((e) => setError(`API injoignable : ${e.message}`));
  }, []);

  const set = (k: keyof typeof opts) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setOpts({ ...opts, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(undefined);
    if (!rights) return setError("Cochez la confirmation des droits d'utilisation.");
    setBusy(true);
    try {
      let res;
      if (file) {
        const form = new FormData();
        form.append("file", file);
        form.append("rights_confirmed", "true");
        form.append("auto", mode);
        form.append("profile", opts.profile);
        if (range.start) form.append("start", range.start);
        if (range.end) form.append("end", range.end);
        res = await api.uploadVideo(form);
      } else {
        const num = (v: string) => (v === "" ? undefined : Number(v));
        res = await api.createVideo({
          source: source.trim(),
          rights_confirmed: true,
          auto: mode,
          start: range.start || undefined,
          end: range.end || undefined,
          options: {
            profile: opts.profile,
            max_video_duration: num(opts.max_video_duration),
            clips_per_video: num(opts.clips_per_video),
            min_clip_duration: num(opts.min_clip_duration),
            max_clip_duration: num(opts.max_clip_duration),
            language: opts.language,
            subtitle_style: opts.subtitle_style,
            aggressiveness: opts.aggressiveness,
          },
        });
      }
      router.push(`/videos/${res.video.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} style={{ maxWidth: 820 }}>
      <h1>Importer une vidéo</h1>
      <p className="muted">
        URL YouTube/Twitch, chemin d&apos;un fichier sur le serveur, ou envoi depuis votre ordinateur. Seuls les contenus dont vous
        détenez les droits (ou sous licence) doivent être traités.
      </p>
      <ErrorBox message={error} />

      <div className="card">
        <div className="field">
          <label htmlFor="src">YouTube URL (ou chemin de fichier serveur)</label>
          <input id="src" placeholder="https://www.youtube.com/watch?v=…" value={source} onChange={(e) => setSource(e.target.value)} disabled={!!file} />
        </div>
        <div className="field">
          <label htmlFor="file">… ou fichier vidéo</label>
          <input id="file" type="file" accept="video/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </div>

        <h2 style={{ marginTop: 8 }}>Extrait (optionnel)</h2>
        <p className="muted small" style={{ marginTop: -4 }}>
          Pour un long stream, n&apos;importez qu&apos;une partie : seul cet extrait est téléchargé et analysé.
          Formats : 6:00:00, 6h, 6h30, 90m.
        </p>
        <div className="fields">
          <div><label>Début</label><input placeholder="ex. 6:00:00" value={range.start} onChange={(e) => setRange({ ...range, start: e.target.value })} /></div>
          <div><label>Fin</label><input placeholder="ex. 7:00:00" value={range.end} onChange={(e) => setRange({ ...range, end: e.target.value })} /></div>
        </div>

        <h2>Options</h2>
        <div className="field">
          <label>Type de contenu</label>
          <select value={opts.profile} onChange={set("profile")}>
            {Object.entries(system?.profiles ?? { stream: "Stream / gaming", sport: "Match / sport", talk: "Émission / podcast / YouTubeur" }).map(([k, label]) => (
              <option key={k} value={k}>{label}</option>
            ))}
          </select>
          {opts.profile === "sport" && (
            <p className="muted small">
              ⚠️ Les images de matchs officiels appartiennent aux ligues et diffuseurs : elles sont presque toujours
              bloquées (Content ID). Utilisez des matchs dont vous avez les droits (amateur, votre club, accord du diffuseur).
            </p>
          )}
          {system && !system.llm_picker && (
            <p className="muted small">
              Astuce : ajoutez une clé ANTHROPIC_API_KEY sur le serveur pour que l&apos;IA choisisse les moments (bien meilleur
              résultat, quelques centimes par heure de vidéo).
            </p>
          )}
        </div>
        <div className="fields">
          <div><label>Durée max analysée (s)</label><input type="number" min={0} placeholder="tout" value={opts.max_video_duration} onChange={set("max_video_duration")} /></div>
          <div><label>Nombre de clips</label><input type="number" min={1} max={50} value={opts.clips_per_video} onChange={set("clips_per_video")} /></div>
          <div><label>Durée min (s)</label><input type="number" min={5} max={180} placeholder="selon le type" value={opts.min_clip_duration} onChange={set("min_clip_duration")} /></div>
          <div><label>Durée max (s)</label><input type="number" min={5} max={180} placeholder="selon le type" value={opts.max_clip_duration} onChange={set("max_clip_duration")} /></div>
          <div>
            <label>Langue</label>
            <select value={opts.language} onChange={set("language")}>
              <option value="auto">Auto</option><option value="fr">Français</option><option value="en">Anglais</option>
              <option value="es">Espagnol</option><option value="de">Allemand</option>
            </select>
          </div>
          <div>
            <label>Preset sous-titres</label>
            <select value={opts.subtitle_style} onChange={set("subtitle_style")}>
              {(system?.subtitle_presets ?? ["clean", "gaming", "high_energy"]).map((p) => <option key={p}>{p}</option>)}
            </select>
          </div>
          <div>
            <label>Agressivité du scoring</label>
            <select value={opts.aggressiveness} onChange={set("aggressiveness")}>
              <option value="conservative">Conservateur</option><option value="normal">Normal</option><option value="aggressive">Agressif</option>
            </select>
          </div>
          <div>
            <label>Après l&apos;import</label>
            <select value={mode} onChange={(e) => setMode(e.target.value as "analyze" | "pipeline")}>
              <option value="analyze">Analyser (je choisis les clips)</option>
              <option value="pipeline">Pipeline complet (clips auto)</option>
            </select>
          </div>
        </div>

        <label className="row" style={{ marginTop: 16, color: "var(--text)" }}>
          <input type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} />
          Je confirme avoir le droit d&apos;utiliser cette vidéo (mon contenu, licence ou autorisation du créateur).
        </label>
        <div className="row" style={{ marginTop: 16 }}>
          <button className="btn btn-primary" disabled={busy || (!source.trim() && !file)}>
            {busy ? "Import en cours…" : "Analyser la vidéo"}
          </button>
          {busy && <span className="muted small">Le téléchargement d&apos;une URL peut prendre quelques minutes.</span>}
        </div>
      </div>
    </form>
  );
}
