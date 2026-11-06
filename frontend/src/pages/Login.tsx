import { FormEvent, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useAuth } from "../auth";

export default function Login() {
  const { user, setupRequired, isLoading } = useAuth();
  const qc = useQueryClient();
  const nav = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from || "/admin";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  const submit = useMutation({
    mutationFn: () =>
      setupRequired ? api.setup(username.trim(), password) : api.login(username.trim(), password),
    onSuccess: (me) => {
      qc.setQueryData(["me"], me);
      nav(from, { replace: true });
    },
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (user) return <Navigate to={from} replace />;

  const mismatch = setupRequired && confirm !== "" && confirm !== password;
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!mismatch) submit.mutate();
  };

  return (
    <div className="panel auth-panel">
      <h1>{setupRequired ? "Create the first admin account" : "Sign in"}</h1>
      <p className="muted">
        {setupRequired
          ? "No accounts exist yet. The account you create here becomes the admin and can add others."
          : "An account is needed to add recordings or change anything. Without one you can still browse and search the Match Library."}
      </p>
      <form className="stack" onSubmit={onSubmit}>
        <div className="field">
          <label>Username</label>
          <input autoFocus autoComplete="username" value={username}
            onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label>Password{setupRequired && " (at least 8 characters)"}</label>
          <input type="password" autoComplete={setupRequired ? "new-password" : "current-password"}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {setupRequired && (
          <div className="field">
            <label>Confirm password</label>
            <input type="password" autoComplete="new-password" value={confirm}
              onChange={(e) => setConfirm(e.target.value)} />
            {mismatch && <span className="error" style={{ fontSize: 12 }}>Passwords don't match</span>}
          </div>
        )}
        {submit.error && <div className="error">{(submit.error as Error).message}</div>}
        <div className="row">
          <button className="primary" type="submit"
            disabled={!username.trim() || !password || mismatch || (setupRequired && !confirm) || submit.isPending}>
            {submit.isPending ? "…" : setupRequired ? "Create admin account" : "Sign in"}
          </button>
        </div>
      </form>
    </div>
  );
}
