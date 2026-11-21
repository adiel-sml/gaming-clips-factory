"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const Icon = {
  home: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 10.5 12 3l9 7.5V21a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z" /></svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
  ),
  clips: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="6" y="2" width="12" height="20" rx="3" /><path d="m10.5 9 4 3-4 3z" /></svg>
  ),
};

export function TopNav() {
  const path = usePathname();
  const is = (p: string) => (p === "/" ? path === "/" : path.startsWith(p));
  return (
    <header className="topbar">
      <div className="topbar-in">
        <Link href="/" className="brand">
          <span className="brand-logo">▶</span>
          <span>GamingClips<span className="muted" style={{ fontWeight: 500 }}> Factory</span></span>
        </Link>
        <nav className="topnav">
          <Link href="/" className={is("/") ? "active" : ""}>Tableau de bord</Link>
          <Link href="/clips" className={is("/clips") ? "active" : ""}>Clips</Link>
          <Link href="/import" className="btn btn-primary btn-sm" style={{ color: "var(--accent-ink)" }}>+ Nouvelle vidéo</Link>
        </nav>
      </div>
    </header>
  );
}

export function BottomNav() {
  const path = usePathname();
  const is = (p: string) => (p === "/" ? path === "/" : path.startsWith(p));
  return (
    <nav className="bottomnav">
      <Link href="/" className={is("/") ? "active" : ""}>{Icon.home}<span>Accueil</span></Link>
      <Link href="/import" aria-label="Nouvelle vidéo"><span className="fab">{Icon.plus}</span></Link>
      <Link href="/clips" className={is("/clips") ? "active" : ""}>{Icon.clips}<span>Clips</span></Link>
    </nav>
  );
}
