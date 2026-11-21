"use client";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ChipGroup, ChoiceCards, RangeSlider, Segmented, TimeField, fmtTime } from "@/components/controls";
import { useToast } from "@/components/Toast";
import { Alert, Spinner } from "@/components/ui";
import { api, Probe, SystemInfo } from "@/lib/api";

const PROFILES = [
  { value: "stream", emoji: "🎮", title: "Stream / gaming", sub: "Réactions, clutch, fails, fous rires" },
  { value: "sport", emoji: "⚽", title: "Match / sport", sub: "Buts, arrêts, cartons, public en feu" },
  { value: "talk", emoji: "🎙️", title: "Émission / podcast", sub: "Punchlines, anecdotes, débats" },
];

const DURATIONS: Record<string, { label: string; min?: number; max?: number }> = {
  auto: { label: "Auto" },
  short: { label: "Court · 10-25 s", min: 10, max: 25 },
  medium: { label: "Moyen · 15-40 s", min: 15, max: 40 },
  long: { label: "Long · 25-60 s", min: 25, max: 60 },
};

const STYLES = [
  { value: "gaming", title: "Gaming", sub: "Majuscules, vert/jaune, pop", preview: <div className="sub-preview" style={{ color: "#fff" }}>C&apos;EST <span style={{ color: "#3DFF6E" }}>FOU</span></div> },
  { value: "high_energy", title: "Énergie", sub: "Très gros, rouge, secousses", preview: <div className="sub-preview" style={{ color: "#fff", fontSize: "1.2rem" }}>NON <span style={{ color: "#FFD400" }}>NON</span></div> },
  { value: "clean", title: "Sobre", sub: "Discret, lisible", preview: <div className="sub-preview" style={{ color: "#fff", fontWeight: 600, textTransform: "none" }}>C&apos;est <span style={{ color: "#FFE14D" }}>fou</span></div> },
];

const MAX_SECONDS = 4 * 3600;

export default function ImportPage() {
  const router = useRouter();
  const toast = useToast();
  const [system, setSystem] = useState<SystemInfo>();
  const [tab, setTab] = useState<"link" | "file">("link");
  const [url, setUrl] = useState("");
  const [probe, setProbe] = useState<Probe | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState<string>();
  const [file, setFile] = useState<File | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [whole, setWhole] = useState(true);
  const [range, setRange] = useState({ start: 0, end: 3600 });
  const [profile, setProfile] = useState("stream");
  const [count, setCount] = useState(10);
  const [dur, setDur] = useState("auto");
  const [style, setStyle] = useState("gaming");
  const [language, setLanguage] = useState("auto");
  const [aggr, setAggr] = useState("normal");
  const [mode, setMode] = useState<"analyze" | "pipeline">("analyze");
  const [rights, setRights] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const probeTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => {
    api.system().then(setSystem).catch((e) => setError(`Serveur injoignable : ${e.message}`));
  }, []);

  // vérifie le lien automatiquement après la saisie / le collage
  useEffect(() => {
    setProbe(null);
    setProbeError(undefined);
    clearTimeout(probeTimer.current);
    if (!/^https?:\/\/\S+\.\S+/.test(url.trim())) return;
    probeTimer.current = setTimeout(async () => {
      setProbing(true);
      try {
        const p = await api.probe(url.trim());
        setProbe(p);
        if (p.duration) {
          setDuration(p.duration);
          const long = p.duration > MAX_SECONDS;
          setWhole(!long);
          setRange({ start: 0, end: Math.min(p.duration, 3600) });
        }
      } catch (e) {
        setProbeError((e as Error).message);
      }
      setProbing(false);
    }, 600);
  }, [url]);

  function pickFile(f: File | null) {
    setFile(f);
    setDuration(null);
    if (!f) return;
    const v = document.createElement("video");
    v.preload = "metadata";
    v.onloadedmetadata = () => {
      const src = v.src;
      if (Number.isFinite(v.duration)) {
        setDuration(v.duration);
        setRange({ start: 0, end: Math.min(v.duration, 3600) });
        setWhole(v.duration <= MAX_SECONDS);
      }
      v.removeAttribute("src");
      v.load();
      URL.revokeObjectURL(src);
    };
    v.src = URL.createObjectURL(f);
  }

  const hasSource = tab === "link" ? !!url.trim() && !probeError : !!file;
  const max = duration ?? Math.max(range.end, 4 * 3600);
  const extractLen = range.end - range.start;
  const tooLong = (whole ? duration ?? 0 : extractLen) > MAX_SECONDS;

  async function submit() {
    setError(undefined);
    if (!rights) return setError("Confirmez d'abord que vous avez le droit d'utiliser cette vidéo.");
    setBusy(true);
    const start = whole ? undefined : fmtTime(range.start);
    const end = whole ? undefined : fmtTime(range.end);
    const d = DURATIONS[dur];
    try {
      let res;
      if (tab === "file" && file) {
        const form = new FormData();
        form.append("file", file);
        form.append("rights_confirmed", "true");
        form.append("auto", mode);
        form.append("profile", profile);
        form.append("options", JSON.stringify({ clips_per_video: count, min_clip_duration: d.min, max_clip_duration: d.max,
          language, subtitle_style: style, aggressiveness: aggr }));
        if (start) form.append("start", start);
        if (end) form.append("end", end);
        res = await api.uploadVideo(form);
      } else {
        res = await api.createVideo({
          source: url.trim(), rights_confirmed: true, auto: mode, start, end,
          options: {
            profile, clips_per_video: count, min_clip_duration: d.min, max_clip_duration: d.max,
            language, subtitle_style: style, aggressiveness: aggr,
          },
        });
      }
      toast(res.already_imported ? "Vidéo déjà importée : ouverture" : "C'est parti ! Le traitement démarre");
      router.push(`/videos/${res.video.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="stack-lg" style={{ maxWidth: 760, margin: "0 auto" }}>
      <div className="page-head">
        <span className="eyebrow">Nouvelle vidéo</span>
        <h1>Créer des clips</h1>
        <p className="muted">Collez un lien ou envoyez un fichier : l&apos;outil trouve les meilleurs moments et les met au format vertical.</p>
      </div>
      <Alert>{error}</Alert>

      {/* 1. Source */}
      <section className="card card-lg stack">
        <div className="section-title" style={{ marginBottom: 0 }}><h2>1 · La vidéo</h2></div>
        <Segmented value={tab} onChange={setTab} options={[{ value: "link", label: "🔗 Lien" }, { value: "file", label: "📁 Fichier" }]} />
        {tab === "link" ? (
          <div className="stack-sm">
            <input className="input" inputMode="url" placeholder="https://www.twitch.tv/videos/…" value={url}
              onChange={(e) => setUrl(e.target.value)} autoCapitalize="off" autoCorrect="off" spellCheck={false} />
            <p className="hint">Twitch, YouTube, Vimeo. YouTube refuse souvent les serveurs : préférez Twitch ou un fichier.</p>
            {probing && <div className="row muted small"><Spinner /> Lecture du lien…</div>}
            {probeError && <Alert kind="warn">{probeError}</Alert>}
            {probe && (
              <div className="card-flat src-card">
                {probe.thumbnail && /* eslint-disable-next-line @next/next/no-img-element */ <img src={probe.thumbnail} alt="" />}
                <div className="grow stack-sm" style={{ gap: 4 }}>
                  <strong className="clamp-2">{probe.title ?? "Vidéo"}</strong>
                  <div className="meta-row">
                    <span className="tag">{probe.platform}</span>
                    {probe.duration ? <span className="tag mono">⏱ {fmtTime(probe.duration)}</span> : null}
                    {probe.uploader && <span className="tag clamp-1" style={{ maxWidth: 160 }}>{probe.uploader}</span>}
                  </div>
                </div>
              </div>
            )}
          </div>
        ) : (
          <label className="check" style={{ justifyContent: "center", flexDirection: "column", alignItems: "center", padding: 24, borderStyle: "dashed", textAlign: "center" }}>
            <span style={{ fontSize: "2rem" }}>📤</span>
            <strong>{file ? file.name : "Choisir une vidéo"}</strong>
            <span className="muted small">{file ? `${(file.size / 1e6).toFixed(0)} Mo${duration ? ` · ${fmtTime(duration)}` : ""}` : "MP4, MOV… depuis votre téléphone ou ordinateur"}</span>
            <input type="file" accept="video/*" style={{ display: "none" }} onChange={(e) => pickFile(e.target.files?.[0] ?? null)} />
          </label>
        )}
      </section>

      {/* 2. Extrait */}
      {hasSource && (
        <section className="card card-lg stack">
          <div className="section-title" style={{ marginBottom: 0 }}>
            <h2>2 · Quelle partie ?</h2>
            {!whole && <span className="tag mono">{fmtTime(extractLen)}</span>}
          </div>
          <Segmented value={whole ? "all" : "part"} onChange={(v) => setWhole(v === "all")}
            options={[{ value: "all", label: "Toute la vidéo" }, { value: "part", label: "Un extrait" }]} />
          {whole && duration && duration > MAX_SECONDS && (
            <Alert kind="warn">Vidéo de {fmtTime(duration)} : au-delà de 4 h, choisissez un extrait.</Alert>
          )}
          {!whole && (
            <div className="stack">
              {duration && (
                <>
                  <RangeSlider min={0} max={duration} step={Math.max(1, Math.round(duration / 2000))} start={range.start} end={range.end}
                    onChange={(s, e) => setRange({ start: s, end: e })} />
                  <div className="row spread tiny muted mono"><span>0:00:00</span><span>{fmtTime(duration)}</span></div>
                </>
              )}
              <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
                <TimeField label="Début" value={range.start} max={max} steps={[-60, -600, 600, 60]}
                  onChange={(v) => setRange((r) => ({ start: Math.min(v, r.end - 10), end: r.end }))} />
                <TimeField label="Fin" value={range.end} max={max} steps={[-60, -600, 600, 60]}
                  onChange={(v) => setRange((r) => ({ start: r.start, end: Math.max(v, r.start + 10) }))} />
              </div>
              <div>
                <span className="label">Durée de l&apos;extrait</span>
                <ChipGroup value={-1} onChange={(len) => setRange((r) => ({ start: r.start, end: Math.min(max, r.start + len) }))}
                  options={[{ value: 600, label: "10 min" }, { value: 1800, label: "30 min" }, { value: 3600, label: "1 h" }, { value: 7200, label: "2 h" }]} />
              </div>
              <p className="hint">Seul cet extrait est téléchargé et analysé : beaucoup plus rapide sur les longs streams.</p>
            </div>
          )}
          {tooLong && !whole && <Alert kind="warn">Extrait trop long (4 h maximum).</Alert>}
        </section>
      )}

      {/* 3. Réglages */}
      {hasSource && (
        <section className="card card-lg stack-lg">
          <div className="section-title" style={{ marginBottom: -12 }}><h2>3 · Le style de clips</h2></div>
          <div>
            <span className="label">Type de contenu</span>
            <ChoiceCards value={profile} onChange={setProfile} options={PROFILES} columns={3} />
            {profile === "sport" && (
              <p className="hint">⚠️ Les images de matchs officiels appartiennent aux ligues et diffuseurs (souvent bloquées). Utilisez des matchs dont vous avez les droits.</p>
            )}
          </div>
          <div>
            <span className="label">Nombre de clips</span>
            <ChipGroup value={count} onChange={setCount} options={[3, 5, 10, 15].map((n) => ({ value: n, label: String(n) }))} />
          </div>
          <div>
            <span className="label">Durée des clips</span>
            <ChipGroup value={dur} onChange={setDur} options={Object.entries(DURATIONS).map(([k, v]) => ({ value: k, label: v.label }))} />
            {dur === "auto" && <p className="hint">Selon le type de contenu (ex. 10-35 s pour le sport, 12-45 s pour le gaming).</p>}
          </div>
          <div>
            <span className="label">Sous-titres</span>
            <ChoiceCards value={style} onChange={setStyle}
              options={STYLES.filter((x) => !system || system.subtitle_presets.includes(x.value)).map((x) => ({ value: x.value, title: x.title, sub: x.sub, visual: x.preview }))} />
          </div>
          <details className="adv">
            <summary>Options avancées</summary>
            <div className="stack" style={{ marginTop: 14 }}>
              <div>
                <span className="label">Langue parlée</span>
                <ChipGroup value={language} onChange={setLanguage}
                  options={[{ value: "auto", label: "Auto" }, { value: "fr", label: "🇫🇷 Français" }, { value: "en", label: "🇬🇧 Anglais" }, { value: "es", label: "🇪🇸 Espagnol" }]} />
              </div>
              <div>
                <span className="label">Sélection des moments</span>
                <Segmented value={aggr} onChange={setAggr}
                  options={[{ value: "conservative", label: "Exigeante" }, { value: "normal", label: "Normale" }, { value: "aggressive", label: "Large" }]} />
              </div>
              <div>
                <span className="label">Après l&apos;analyse</span>
                <Segmented value={mode} onChange={setMode}
                  options={[{ value: "analyze", label: "Je choisis les clips" }, { value: "pipeline", label: "Tout en automatique" }]} />
              </div>
            </div>
          </details>
          {system && !system.llm_picker && (
            <Alert kind="info">Les moments sont choisis par des règles automatiques. Ajoutez une clé ANTHROPIC_API_KEY sur le serveur pour que l&apos;IA les choisisse (bien meilleur, quelques centimes par heure).</Alert>
          )}
        </section>
      )}

      {hasSource && (
        <>
          <label className="check">
            <input type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} />
            <span className="small">J&apos;ai le droit d&apos;utiliser cette vidéo : c&apos;est mon contenu, elle est sous licence, ou j&apos;ai l&apos;accord du créateur.</span>
          </label>
          <div className="action-bar">
            <button className="btn btn-primary btn-lg btn-block" disabled={busy || !rights || tooLong} onClick={submit}>
              {busy ? <><Spinner /> Envoi…</> : "✨ Trouver les meilleurs moments"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
