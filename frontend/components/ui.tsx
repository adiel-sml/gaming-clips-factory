import { CATEGORY_FR } from "@/lib/api";

export const CATEGORY_EMOJI: Record<string, string> = {
  unexpected_reaction: "😱",
  funny: "😂",
  rage: "😤",
  victory_clutch: "🏆",
  fail: "💀",
  action: "💥",
  quote: "💬",
  rare_event: "✨",
  tension_release: "⚡",
};

export function StatCard({ label, value, hint, tone }: { label: string; value: string | number; hint?: string; tone?: "good" | "warn" | "bad" }) {
  return (
    <div className={`card stat ${tone ?? ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint && <div className="muted tiny">{hint}</div>}
    </div>
  );
}

export function ScoreRing({ score, size }: { score: number; size?: "sm" }) {
  const tone = score >= 80 ? "hot" : score >= 60 ? "warm" : "cool";
  return (
    <div className={`score-ring ${tone} ${size ?? ""}`} style={{ ["--v" as string]: Math.round(score) }} title={`Score ${Math.round(score)}/100`}>
      <span>{Math.round(score)}</span>
    </div>
  );
}

const STATUS_FR: Record<string, string> = {
  downloading: "Téléchargement", imported: "Importée", queued: "En file", analyzing: "Analyse", analyzed: "Analysée",
  failed: "Échec", pending: "À rendre", rendering: "Rendu", ready: "Prêt", needs_review: "À vérifier", approved: "Gardé",
  rejected: "Écarté", exported: "Enregistré", candidate: "Candidat", generated: "Généré", duplicate: "Doublon",
  running: "En cours", done: "Terminé",
};

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status}`}>{STATUS_FR[status] ?? status}</span>;
}

export function CategoryTag({ category }: { category: string }) {
  return (
    <span className="tag">
      {CATEGORY_EMOJI[category] ?? "🎬"} {CATEGORY_FR[category] ?? category}
    </span>
  );
}

export function Alert({ kind = "error", children }: { kind?: "error" | "warn" | "info" | "ok"; children: React.ReactNode }) {
  if (!children) return null;
  const icon = { error: "⚠️", warn: "⚠️", info: "💡", ok: "✅" }[kind];
  return (
    <div className={`alert alert-${kind}`}>
      <span>{icon}</span>
      <div>{children}</div>
    </div>
  );
}

export function ErrorBox({ message }: { message?: string }) {
  return message ? <Alert>{message}</Alert> : null;
}

export function Empty({ emoji, title, children }: { emoji: string; title: string; children?: React.ReactNode }) {
  return (
    <div className="card empty">
      <div className="empty-emoji">{emoji}</div>
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" aria-hidden />;
}

/** Compte à rebours avant la purge automatique du serveur (48 h sans activité). */
export function ExpiryTag({ at, compact }: { at?: string | null; compact?: boolean }) {
  if (!at) return null;
  const hours = (new Date(at).getTime() - Date.now()) / 3_600_000;
  if (!isFinite(hours)) return null;
  const label = hours <= 1 ? "< 1 h" : hours < 24 ? `${Math.floor(hours)} h` : `${Math.floor(hours / 24)} j ${Math.floor(hours % 24)} h`;
  const urgent = hours < 12;
  return (
    <span className={`tag mono${urgent ? " tag-warn" : ""}`}
      title="Supprimé automatiquement du serveur 48 h après la dernière activité. Enregistrez la vidéo sur votre téléphone pour la garder.">
      🗑 {compact ? label : `supprimé dans ${label}`}
    </span>
  );
}
