import type { Metadata, Viewport } from "next";
import { Inter, Space_Grotesk } from "next/font/google";
import { BottomNav, TopNav } from "@/components/Nav";
import { ToastProvider } from "@/components/Toast";
import TokenGate from "@/components/TokenGate";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const grotesk = Space_Grotesk({ subsets: ["latin"], variable: "--font-grotesk", display: "swap", weight: ["500", "700"] });

export const metadata: Metadata = {
  title: "GamingClips Factory",
  description: "Clips verticaux automatiques pour TikTok, Shorts et Reels",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#0a0b10",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fr" className={`${inter.variable} ${grotesk.variable}`}>
      <body>
        <ToastProvider>
          <TopNav />
          <main className="container">
            <TokenGate>{children}</TokenGate>
          </main>
          <BottomNav />
        </ToastProvider>
      </body>
    </html>
  );
}
