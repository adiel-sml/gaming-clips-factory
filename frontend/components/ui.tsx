import { CATEGORY_FR } from "@/lib/api";

export function StatCard({ label, value, hint, tone }: { label: string; value: string | number; hint?: string; tone?: "good" | "warn" | "bad" }) {
  return (
    <div className={`card stat ${tone ?? ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint && <div className="muted small">{hint}</div>}
    </div>
  );
}

export function ScoreBadge({ score }: { score: number }) {
  const tone = score >= 80 ? "hot" : score >= 60 ? "warm" : "cool";
  return <span className={`score ${tone}`}>{Math.round(score)}</span>;
}

const STATUS_FR: Record<string, string> = {
  imported: "Importée", queued: "En file", analyzing: "Analyse…", analyzed: "Analysée", failed: "Échec",
  pending: "À rendre", rendering: "Rendu…", ready: "Prêt", needs_review: "À vérifier", approved: "Validé",
  rejected: "Rejeté", exported: "Exporté", candidate: "Candidat", generated: "Généré", duplicate: "Doublon",
  running: "En cours", done: "Terminé",
};

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status}`}>{STATUS_FR[status] ?? status}</span>;
}

export function CategoryTag({ category }: { category: string }) {
  return <span className="tag">{CATEGORY_FR[category] ?? category}</span>;
}

export function ErrorBox({ message }: { message?: string }) {
  if (!message) return null;
  return <div className="error-box">{message}</div>;
}
