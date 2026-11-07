import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, AppSettings, EventSearchOut } from "../api/client";

export default function Settings() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const events = useQuery({ queryKey: ["events"], queryFn: api.listEvents });

  const [form, setForm] = useState<Partial<AppSettings>>({});
  const [token, setToken] = useState("");
  useEffect(() => {
    if (settings.data) setForm(settings.data);
  }, [settings.data]);

  const saveSettings = useMutation({
    mutationFn: () => {
      const body: Partial<AppSettings> = {
        clip_pad_start_s: form.clip_pad_start_s,
        clip_pad_end_s: form.clip_pad_end_s,
        sample_fps: form.sample_fps,
        live_sample_fps: form.live_sample_fps,
        keep_full_recording: form.keep_full_recording,
        overlay_profile: form.overlay_profile,
      };
      if (token.trim()) (body as Record<string, unknown>).vexevents_token = token.trim();
      return api.putSettings(body);
    },
    onSuccess: () => {
      setToken("");
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const testVE = useMutation({ mutationFn: api.testVexEvents });

  const [sku, setSku] = useState("");
  const [found, setFound] = useState<EventSearchOut | null>(null);
  const search = useMutation({
    mutationFn: () => api.searchEvent(sku.trim()),
    onSuccess: setFound,
  });
  const importEv = useMutation({
    mutationFn: () => api.importEvent(sku.trim()),
    onSuccess: () => {
      setFound(null);
      setSku("");
      qc.invalidateQueries({ queryKey: ["events"] });
    },
  });
  const delEv = useMutation({
    mutationFn: (id: number) => api.deleteEvent(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["events"] }),
  });

  const s = settings.data;
  const set = (patch: Partial<AppSettings>) => setForm((f) => ({ ...f, ...patch }));

  return (
    <div>
      <h1>Settings</h1>

      <div className="panel">
        <h2>VEX Events API</h2>
        <p className="muted">
          Free API token from your <a href="https://events.vex.com" target="_blank" rel="noreferrer">events.vex.com</a> account.
          Enables authoritative team lists and final scores.
        </p>
        <div className="row">
          {s?.vexevents_token_source === "env" ? (
            <div className="field" style={{ flex: 1 }}>
              <label>API key <span className="badge ok">from environment</span></label>
              <div className="muted" style={{ fontSize: 13 }}>
                Using the key from the <code>SCOUTHUB_VEXEVENTS_TOKEN</code> /{" "}
                <code>VEX_EVENTS_API_KEY</code> environment variable. Unset it to manage the key here.
              </div>
            </div>
          ) : (
            <div className="field" style={{ flex: 1 }}>
              <label>API key {s?.vexevents_token_set && <span className="badge ok">stored</span>}</label>
              <input type="password" value={token} placeholder={s?.vexevents_token_set ? "•••••••• (leave blank to keep)" : "paste key"}
                onChange={(e) => setToken(e.target.value)} />
              <div className="muted" style={{ fontSize: 12 }}>
                Or set <code>SCOUTHUB_VEXEVENTS_TOKEN</code> in the environment (docker-compose.yml) — it overrides this.
              </div>
            </div>
          )}
          <button onClick={() => testVE.mutate()} disabled={testVE.isPending}>Test connection</button>
        </div>
        {testVE.isSuccess && <p className="badge ok">VEX Events reachable ✓</p>}
        {testVE.error && <p className="error">{String((testVE.error as Error).message)}</p>}
      </div>

      <div className="panel">
        <h2>Processing</h2>
        <div className="row">
          <div className="field">
            <label>Clip pad before (s)</label>
            <input type="number" step="0.5" style={{ width: 100 }} value={form.clip_pad_start_s ?? ""}
              onChange={(e) => set({ clip_pad_start_s: Number(e.target.value) })} />
          </div>
          <div className="field">
            <label>Clip pad after (s)</label>
            <input type="number" step="0.5" style={{ width: 100 }} value={form.clip_pad_end_s ?? ""}
              onChange={(e) => set({ clip_pad_end_s: Number(e.target.value) })} />
          </div>
          <div className="field">
            <label>Detection sample rate (fps)</label>
            <input type="number" step="0.25" style={{ width: 100 }} value={form.sample_fps ?? ""}
              onChange={(e) => set({ sample_fps: Number(e.target.value) })} />
          </div>
          <div className="field">
            <label title="Frames per second read while a stream is still being captured. Lower uses less CPU so splitting keeps up with a live stream.">
              Live splitting rate (fps)</label>
            <input type="number" step="0.25" min="0.1" style={{ width: 100 }} value={form.live_sample_fps ?? ""}
              onChange={(e) => set({ live_sample_fps: Number(e.target.value) })} />
          </div>
          <div className="field">
            <label>Overlay profile</label>
            <select value={form.overlay_profile ?? ""} onChange={(e) => set({ overlay_profile: e.target.value })}>
              {s?.overlay_profiles.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <label style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6 }}>
            <input type="checkbox" checked={form.keep_full_recording ?? true}
              onChange={(e) => set({ keep_full_recording: e.target.checked })} />
            Keep full recording after clipping
          </label>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>Archive root (set at deploy time): <code>{s?.archive_root}</code></p>
        {saveSettings.error && <p className="error">{String((saveSettings.error as Error).message)}</p>}
        <button className="primary" onClick={() => saveSettings.mutate()} disabled={saveSettings.isPending}>
          {saveSettings.isPending ? "Saving…" : "Save settings"}
        </button>
      </div>

      <div className="panel">
        <h2>Events</h2>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label>Import by events.vex.com link or event code (e.g. RE-V5RC-26-4058)</label>
            <input value={sku} onChange={(e) => setSku(e.target.value)}
              placeholder="https://events.vex.com/…/RE-V5RC-26-4058.html" />
          </div>
          <button onClick={() => search.mutate()} disabled={!sku.trim() || search.isPending}>Look up</button>
        </div>
        {search.error && <p className="error">{String((search.error as Error).message)}</p>}
        {found && (
          <div className="panel" style={{ background: "var(--panel-2)" }}>
            <b>{found.name}</b> · {found.program} · {found.season}
            <div className="muted">{found.divisions.map((d) => d.name).join(", ") || "single division"}</div>
            <button className="primary sm" style={{ marginTop: 8 }}
              onClick={() => importEv.mutate()} disabled={importEv.isPending}>Import</button>
          </div>
        )}
        <table style={{ marginTop: 12 }}>
          <thead><tr><th className="no-sort">Name</th><th className="no-sort">Event code</th>
            <th className="no-sort">Program</th><th className="no-sort"></th></tr></thead>
          <tbody>
            {events.data?.map((e) => (
              <tr key={e.id}>
                <td>{e.name}</td>
                <td className="muted">{e.sku}</td>
                <td>{e.program}</td>
                <td><button className="sm danger" onClick={() => delEv.mutate(e.id)}>Remove</button></td>
              </tr>
            ))}
            {events.data?.length === 0 && <tr><td colSpan={4} className="muted">No events imported.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
