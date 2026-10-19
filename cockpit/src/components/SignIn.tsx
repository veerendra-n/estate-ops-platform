import { useEffect, useState, type FormEvent } from "react";
import { KeyRound, LogIn, ShieldCheck } from "lucide-react";
import { api, ApiError, setToken } from "../api";
import type { Me } from "../types";
import { Logo } from "./Layout";

export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [token, setTok] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Me>("/auth/me").then(setMe, () => setMe(null));
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    setToken(token.trim());
    try {
      await api("/api/v1/tenants");
      onSignedIn();
    } catch (err) {
      setToken(null);
      setError(err instanceof ApiError && err.status === 401 ? "That token was not accepted." : (err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="signin">
      <div className="signin-card">
        <div className="signin-brand">
          <Logo />
          <div>
            <div className="signin-title">Estate Ops</div>
            <div className="muted">Operations cockpit</div>
          </div>
        </div>
        <h2>Sign in to continue</h2>
        <p className="muted">Cases, approvals and the estate graph for every customer you operate.</p>
        {me === undefined ? null : me?.sso ? (
          <a className="btn primary block" href="/auth/login">
            <LogIn size={16} /> Sign in with SSO
          </a>
        ) : (
          <form onSubmit={submit} className="signin-form">
            <label className="field">
              <span>Console token</span>
              <div className="input-icon">
                <KeyRound size={14} />
                <input
                  type="password"
                  autoFocus
                  autoComplete="current-password"
                  value={token}
                  onChange={(e) => setTok(e.target.value)}
                  placeholder="Paste your CONSOLE_TOKEN"
                />
              </div>
            </label>
            {error && <div className="error-note">{error}</div>}
            <button className="btn primary block" disabled={busy || !token.trim()}>
              {busy ? "Checking…" : "Sign in"}
            </button>
          </form>
        )}
        <div className="signin-foot muted">
          <ShieldCheck size={13} /> Every action is recorded in the hash-chained case ledger.
        </div>
      </div>
    </div>
  );
}
