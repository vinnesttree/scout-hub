import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Recording } from "../api/client";
import FolderPicker from "../components/FolderPicker";

function fmtDur(s: number | null) {
  if (!s) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
          : `${m}:${String(sec).padStart(2, "0")}`;
}

export default function Recordings() {
  const qc = useQueryClient();
  const recordings = useQuery({
    queryKey: ["recordings"],
    queryFn: api.listRecordings,
    refetchInterval: 3000,
  });
  const events = useQuery({ queryKey: ["events"], queryFn: api.listEvents });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.listJobs(), refetchInterval: 4000 });

  const [showForm, setShowForm] = useState(false);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["recordings"] });
    qc.invalidateQueries({ queryKey: ["jobs"] });
  };

  const stop = useMutation({ mutationFn: api.stopRecording, onSuccess: invalidate });
  const reprocess = useMutation({ mutationFn: api.processRecording, onSuccess: invalidate });
  const del = useMutation({
    mutationFn: (v: { id: number; files: boolean }) => api.deleteRecording(v.id, v.files),
    onSuccess: invalidate,
  });

  return (
    <div>
      <div className="row">
        <h1>Recordings</h1>
        <div className="spacer" />
        <button className="primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "Add recording"}
        </button>
      </div>

      {showForm && (
        <NewRecordingForm
          events={events.data ?? []}
          onDone={() => {
            setShowForm(false);
            invalidate();
          }}
        />
      )}

      <table>
        <thead>
          <tr>
            <th className="no-sort">Source</th>
            <th className="no-sort">Event</th>
            <th className="no-sort">Status</th>
            <th className="no-sort">Duration</th>
            <th className="no-sort">Matches</th>
            <th className="no-sort">Progress</th>
            <th className="no-sort"></th>
          </tr>
        </thead>
        <tbody>
          {recordings.data?.map((r) => (
            <RecordingRow
              key={r.id}
              r={r}
              eventName={events.data?.find((e) => e.id === r.event_id)?.name}
              onStop={() => stop.mutate(r.id)}
              onReprocess={() => reprocess.mutate(r.id)}
              onDelete={(files) => {
                if (confirm(`Delete recording #${r.id}${files ? " and its files" : ""}?`))
                  del.mutate({ id: r.id, files });
              }}
            />
          ))}
          {recordings.data?.length === 0 && (
            <tr><td colSpan={7} className="muted">No recordings yet.</td></tr>
          )}
        </tbody>
      </table>

      <h2>Recent processing jobs</h2>
      <table>
        <thead>
          <tr><th className="no-sort">#</th><th className="no-sort">Recording</th>
          <th className="no-sort">Status</th><th className="no-sort">Detail</th></tr>
        </thead>
        <tbody>
          {jobs.data?.slice(0, 10).map((j) => (
            <tr key={j.id}>
              <td>{j.id}</td>
              <td>{j.recording_id ?? "—"}</td>
              <td><span className={`pill ${j.status}`}>{j.status}</span></td>
              <td className="muted">{j.error ? <span className="error">{j.error}</span> : j.detail}</td>
            </tr>
          ))}
          {jobs.data?.length === 0 && <tr><td colSpan={4} className="muted">No jobs yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function RecordingRow({
  r, eventName, onStop, onReprocess, onDelete,
}: {
  r: Recording; eventName?: string;
  onStop: () => void; onReprocess: () => void; onDelete: (files: boolean) => void;
}) {
  return (
    <tr>
      <td>
        <div style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }}>
          <a href={r.source_url} target="_blank" rel="noreferrer">{r.title || r.source_url}</a>
        </div>
        <div className="muted" style={{ fontSize: 12 }}>
          {r.source_type}
          {r.is_live === false && " · archived video"}
          {r.is_live === true && " · live"}
          {r.sections && ` · section ${r.sections}`}
          {" · "}{r.archive_dir}
        </div>
      </td>
      <td>{eventName ?? (r.event_id ? `#${r.event_id}` : "—")}</td>
      <td><span className={`pill ${r.status}`}>{r.status}</span>
        {r.error && <div className="error" style={{ fontSize: 12 }}>{r.error}</div>}
      </td>
      <td>{fmtDur(r.duration_s)}</td>
      <td>{r.match_count}</td>
      <td className="muted" style={{ fontSize: 12, maxWidth: 240 }}>{r.progress}</td>
      <td>
        <div className="stack">
          {r.status === "recording" && (
            <button className="sm" onClick={onStop}>Stop</button>
          )}
          {r.status === "completed" && (
            <button className="sm" onClick={onReprocess}>Re-process</button>
          )}
          <button className="sm danger" onClick={() => onDelete(false)}>Delete</button>
        </div>
      </td>
    </tr>
  );
}

function NewRecordingForm({
  events, onDone,
}: {
  events: { id: number; name: string }[];
  onDone: () => void;
}) {
  const [url, setUrl] = useState("");
  const [dir, setDir] = useState("");
  const [eventId, setEventId] = useState<string>("");
  const [lfs, setLfs] = useState(true);
  const [sections, setSections] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.createRecording({
        source_url: url.trim(),
        archive_dir: dir,
        event_id: eventId ? Number(eventId) : null,
        live_from_start: lfs,
        sections: sections.trim() || null,
      }),
    onSuccess: onDone,
  });

  return (
    <div className="panel">
      <div className="stack">
        <div className="field">
          <label>Video or stream URL — a live stream <em>or</em> an archived YouTube/Twitch
            video (YouTube / Twitch / .m3u8, or a vexworlds.tv page)</label>
          <input value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="https://www.youtube.com/watch?v=…" />
        </div>
        <div className="field">
          <label>Archive folder (under the media root)</label>
          <FolderPicker value={dir} onChange={setDir} />
          {dir && <div className="muted" style={{ fontSize: 12 }}>→ {dir}</div>}
        </div>
        <div className="row">
          <div className="field">
            <label>Link event (optional — enables schedule/score matching)</label>
            <select value={eventId} onChange={(e) => setEventId(e.target.value)}>
              <option value="">— none —</option>
              {events.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Section (archived video only, e.g. <code>1:03:00-1:21:30</code>)</label>
            <input value={sections} onChange={(e) => setSections(e.target.value)}
              placeholder="whole video" style={{ width: 200 }} />
          </div>
          <label style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6 }}>
            <input type="checkbox" checked={lfs} onChange={(e) => setLfs(e.target.checked)} />
            If live, capture from the start
          </label>
        </div>
        {create.error && <div className="error">{String((create.error as Error).message)}</div>}
        <div className="row">
          <button className="primary" disabled={!url.trim() || !dir || create.isPending}
            onClick={() => create.mutate()}>
            {create.isPending ? "Starting…" : "Start recording"}
          </button>
        </div>
      </div>
    </div>
  );
}
