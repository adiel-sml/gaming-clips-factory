"use client";
import { useEffect, useState } from "react";
import { API_URL, getToken, setToken } from "@/lib/api";

type State = "checking" | "ok" | "locked";

/** Demande le jeton d'accès si l'API est protégée (GCF_API_TOKEN côté serveur). */
export default function TokenGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<State>("checking");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");

  async function check(token: string): Promise<boolean> {
    try {
      const res = await fetch(`${API_URL}/stats`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        cache: "no-store",
      });
      return res.status !== 401;
    } catch {
      return true; // API injoignable : les pages affichent leur propre message d'erreur
    }
  }

  useEffect(() => {
    check(getToken()).then((ok) => setState(ok ? "ok" : "locked"));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (await check(value.trim())) {
      setToken(value.trim());
      setState("ok");
    } else {
      setError("Jeton invalide.");
    }
  }

  if (state === "checking") return <p className="muted">Connexion…</p>;
  if (state === "ok") return <>{children}</>;
  return (
    <form onSubmit={submit} className="card" style={{ maxWidth: 420, margin: "60px auto" }}>
      <h1>Accès</h1>
      <p className="muted">Saisissez le jeton d&apos;accès de votre serveur (variable GCF_API_TOKEN).</p>
      <div className="field">
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoFocus placeholder="Jeton d'accès" />
      </div>
      {error && <div className="error-box">{error}</div>}
      <button className="btn btn-primary" disabled={!value.trim()}>Entrer</button>
    </form>
  );
}
