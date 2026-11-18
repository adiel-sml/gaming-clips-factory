import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "GamingClips Factory",
  description: "Clips gaming verticaux automatiques pour TikTok, Shorts et Reels",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fr">
      <body>
        <header className="topbar">
          <Link href="/" className="brand">
            <span className="brand-mark">▶</span> GamingClips Factory
          </Link>
          <nav>
            <Link href="/">Dashboard</Link>
            <Link href="/import">Importer</Link>
            <Link href="/clips">Clips</Link>
          </nav>
        </header>
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
