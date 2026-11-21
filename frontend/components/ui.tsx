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
