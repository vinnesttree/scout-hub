import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, User } from "../api/client";
import { useAuth } from "../auth";

export default function Users() {
  const { user: me } = useAuth();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: api.listUsers });
  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });

  const del = useMutation({ mutationFn: api.deleteUser, onSuccess: refresh });
  const setAdmin = useMutation({
    mutationFn: (v: { id: number; is_admin: boolean }) => api.updateUser(v.id, { is_admin: v.is_admin }),
    onSuccess: refresh,
  });
  const resetPw = useMutation({
    mutationFn: (v: { id: number; password: string }) => api.updateUser(v.id, { password: v.password }),
    onSuccess: () => alert("Password reset. That account has been signed out everywhere."),
  });
  const rowError = del.error || setAdmin.error || resetPw.error;

  return (
    <div>
      <h1>Accounts</h1>
      <p className="muted">
        Anyone with an account can add recordings, manage events and settings, and edit matches.
        Visitors without one can only browse and search the Match Library.
        {me?.is_admin ? " As an admin you can also add and remove accounts." : " Only admins can add or remove accounts."}
      </p>

      {me?.is_admin && <NewUserForm onDone={refresh} />}

      <table>
        <thead>
          <tr>
            <th className="no-sort">Username</th>
            <th className="no-sort">Role</th>
            <th className="no-sort">Last sign-in</th>
            <th className="no-sort"></th>
          </tr>
        </thead>
        <tbody>
          {users.data?.map((u) => (
            <UserRow key={u.id} u={u} isMe={u.id === me?.id} canManage={!!me?.is_admin}
              onToggleAdmin={() => setAdmin.mutate({ id: u.id, is_admin: !u.is_admin })}
              onResetPassword={() => {
                const pw = prompt(`New password for ${u.username} (at least 8 characters):`);
                if (pw) resetPw.mutate({ id: u.id, password: pw });
              }}
              onDelete={() => {
                if (confirm(`Delete the account "${u.username}"?`)) del.mutate(u.id);
              }} />
          ))}
        </tbody>
      </table>
      {users.error && <p className="error">{(users.error as Error).message}</p>}
      {rowError && <p className="error">{(rowError as Error).message}</p>}

      {me && <ChangeOwnPassword userId={me.id} />}
    </div>
  );
}

function UserRow({
  u, isMe, canManage, onToggleAdmin, onResetPassword, onDelete,
}: {
  u: User; isMe: boolean; canManage: boolean;
  onToggleAdmin: () => void; onResetPassword: () => void; onDelete: () => void;
}) {
  return (
    <tr>
      <td>{u.username}{isMe && <span className="muted"> (you)</span>}</td>
      <td>{u.is_admin ? <span className="pill completed">admin</span> : <span className="pill">member</span>}</td>
      <td className="muted">{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}</td>
      <td>
        {canManage && (
          <div className="row" style={{ gap: 6 }}>
            {!isMe && (
              <button className="sm" onClick={onToggleAdmin}>
                {u.is_admin ? "Remove admin" : "Make admin"}
              </button>
            )}
            {!isMe && <button className="sm" onClick={onResetPassword}>Reset password</button>}
            {!isMe && <button className="sm danger" onClick={onDelete}>Delete</button>}
          </div>
        )}
      </td>
    </tr>
  );
}

function NewUserForm({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const create = useMutation({
    mutationFn: () => api.createUser({ username: username.trim(), password, is_admin: isAdmin }),
    onSuccess: () => {
      setUsername("");
      setPassword("");
      setIsAdmin(false);
      onDone();
    },
  });
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Add account</h2>
      <form className="row" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <div className="field">
          <label>Username</label>
          <input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label>Password (at least 8 characters)</label>
          <input type="password" autoComplete="new-password" value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </div>
        <label style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6 }}>
          <input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} />
          Admin (can manage accounts)
        </label>
        <button className="primary" type="submit"
          disabled={!username.trim() || !password || create.isPending}>
          Add account
        </button>
      </form>
      {create.error && <p className="error">{(create.error as Error).message}</p>}
    </div>
  );
}

function ChangeOwnPassword({ userId }: { userId: number }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useMutation({
    mutationFn: () => api.updateUser(userId, { password: next, current_password: current }),
    onSuccess: () => {
      setCurrent("");
      setNext("");
    },
  });
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h2 style={{ marginTop: 0 }}>Change my password</h2>
      <form className="row" onSubmit={(e) => { e.preventDefault(); change.mutate(); }}>
        <div className="field">
          <label>Current password</label>
          <input type="password" autoComplete="current-password" value={current}
            onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="field">
          <label>New password</label>
          <input type="password" autoComplete="new-password" value={next}
            onChange={(e) => setNext(e.target.value)} />
        </div>
        <button type="submit" disabled={!current || !next || change.isPending}>Change password</button>
      </form>
      {change.isSuccess && <p className="badge ok">Password changed. Other sign-ins were signed out.</p>}
      {change.error && <p className="error">{(change.error as Error).message}</p>}
    </div>
  );
}
