import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, KeyFrame, KeyFrameStatus, Recording } from "../api/client";

function hms(s: number) {
  const t = Math.max(0, Math.floor(s));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

const pct = (x: number | undefined) => (x === undefined ? "—" : `${Math.round(x * 100)}%`);

const TABS: { key: KeyFrameStatus | ""; label: string }[] = [
  { key: "candidate", label: "To review" },
  { key: "confirmed", label: "Confirmed" },
  { key: "rejected", label: "Rejected" },
  { key: "", label: "All" },
];

/**
 * Review the pre-match team-intro frames found in a recording, then train the
 * intro detector on the ones confirmed here. Nothing is trained on frames that
 * haven't been reviewed.
 */
export default function KeyFrames() {
  const qc = useQueryClient();
  const recordings = useQuery({ queryKey: ["recordings"], queryFn: api.listRecordings });
  const summary = useQuery({
    queryKey: ["keyframe-summary"], queryFn: api.keyFrameSummary, refetchInterval: 5000,
  });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.listJobs(), refetchInterval: 3000 });

  const [recId, setRecId] = useState<number | "">("");
  const [tab, setTab] = useState<KeyFrameStatus | "">("candidate");
  const frames = useQuery({
    queryKey: ["keyframes", recId, tab],
    queryFn: () => api.listKeyFrames({ recording_id: recId || undefined, status: tab || undefined }),
    refetchInterval: 5000,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["keyframes"] });
    qc.invalidateQueries({ queryKey: ["keyframe-summary"] });
    qc.invalidateQueries({ queryKey: ["jobs"] });
  };
  const find = useMutation({ mutationFn: api.findKeyFrames, onSuccess: invalidate });
  const patch = useMutation({
    mutationFn: (v: { id: number; status?: KeyFrameStatus; t?: number }) =>
      api.patchKeyFrame(v.id, { status: v.status, t: v.t }),
    onSuccess: invalidate,
  });
  const bulk = useMutation({
    mutationFn: (v: { ids: number[]; status: KeyFrameStatus }) => api.bulkKeyFrames(v.ids, v.status),
    onSuccess: invalidate,
  });
  const train = useMutation({ mutationFn: api.trainKeyFrames, onSuccess: invalidate });
  const dropModel = useMutation({ mutationFn: api.deleteKeyFrameModel, onSuccess: invalidate });

  const searchable = (recordings.data ?? []).filter((r) => r.status === "completed" && r.file_path);
  const recName = (id: number) => {
    const r = recordings.data?.find((x) => x.id === id);
    return r ? `#${r.id} ${r.title || r.source_url}` : `#${id}`;
  };
  const findJob = useMemo(
    () => jobs.data?.find((j) => j.kind === "find_keyframes" && (!recId || j.recording_id === recId)),
    [jobs.data, recId],
  );
  const busy = findJob && (findJob.status === "queued" || findJob.status === "running");

  const totals = summary.data?.totals;
  const minConf = summary.data?.min_confirmed ?? 3;
  const model = summary.data?.model;
  const pending = (frames.data ?? []).filter((k) => k.status === "candidate");

  return (
    <div>
      <h1>Key frames</h1>
      <p className="muted" style={{ marginTop: -8 }}>
        Most streams show a card with both alliances' teams just before each match's countdown.
        Check the frames found below, confirming the ones that really are that intro card. The
        detector is trained only on frames you confirm or reject here. Once trained, it reads
        the teams off each match's intro, and it can split matches on streams that have no
        readable clock.
      </p>

      <div className="grid2">
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>1 · Find key frames</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            Looks at the 90 s before every match of a recording (the matches already split from
            it) and picks the frame most likely to be the team intro. Matches you've already
            reviewed are left alone.
          </p>
          <div className="row">
            <div className="field" style={{ flex: 1, minWidth: 200 }}>
              <label>Recording</label>
              <select value={recId} onChange={(e) => setRecId(e.target.value ? Number(e.target.value) : "")}>
                <option value="">All recordings</option>
                {recordings.data?.map((r: Recording) => (
                  <option key={r.id} value={r.id}>
                    {recName(r.id)} ({r.match_count} matches)
                  </option>
                ))}
              </select>
            </div>
            <button
              className="primary"
              disabled={!recId || !searchable.some((r) => r.id === recId) || !!busy || find.isPending}
              title={recId && !searchable.some((r) => r.id === recId)
                ? "Needs a completed recording whose full video was kept" : ""}
              onClick={() => recId && find.mutate(recId)}
            >
              {busy ? "Searching…" : "Find key frames"}
            </button>
          </div>
          {findJob && (
            <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
              Last search ({recName(findJob.recording_id ?? 0)}):{" "}
              <span className={`pill ${findJob.status}`}>{findJob.status}</span>{" "}
              {findJob.error ? <span className="error">{findJob.error}</span> : findJob.detail}
            </p>
          )}
          {find.error && <p className="error">{String((find.error as Error).message)}</p>}
        </div>

        <div className="panel">
          <h2 style={{ marginTop: 0 }}>2 · Train the intro detector</h2>
          <div className="row" style={{ gap: 20, marginBottom: 10 }}>
            <div className="stat"><b>{totals?.candidate ?? 0}</b><span className="muted">to review</span></div>
            <div className="stat"><b style={{ color: "var(--green)" }}>{totals?.confirmed ?? 0}</b><span className="muted">confirmed</span></div>
            <div className="stat"><b style={{ color: "var(--red)" }}>{totals?.rejected ?? 0}</b><span className="muted">rejected</span></div>
          </div>
          {model?.trained ? (
            <div style={{ fontSize: 13 }} className="stack">
              <div>
                <span className="badge ok">trained</span>{" "}
                {model.trained_at && new Date(model.trained_at).toLocaleString()} on {model.confirmed} confirmed
                {" "}/ {model.rejected} rejected key frames
              </div>
              <div className="muted">
                Held-out check: found {model.cv?.intros_found}/{model.cv?.intros_total} intros ·
                frame precision {pct(model.cv?.precision)} · recall {pct(model.cv?.recall)}
              </div>
              {model.warning && <div className="badge warn" style={{ alignSelf: "flex-start" }}>{model.warning}</div>}
            </div>
          ) : (
            <p className="muted" style={{ fontSize: 13 }}>
              Not trained yet. Needs at least {minConf} confirmed key frames.
            </p>
          )}
          <div className="row" style={{ marginTop: 10 }}>
            <button className="primary" disabled={(totals?.confirmed ?? 0) < minConf || train.isPending}
              onClick={() => train.mutate()}>
              {train.isPending ? "Training…" : `Train on ${totals?.confirmed ?? 0} confirmed`}
            </button>
            {model?.trained && (
              <button className="danger" disabled={dropModel.isPending}
                onClick={() => confirm("Delete the trained intro detector? Detection goes back to timer / label only.")
                  && dropModel.mutate()}>
                Delete model
              </button>
            )}
          </div>
          {(totals?.candidate ?? 0) > 0 && (
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              {totals?.candidate} frame(s) still waiting for review are not used for training.
            </p>
          )}
          {train.error && <p className="error">{String((train.error as Error).message)}</p>}
        </div>
      </div>

      <div className="row" style={{ marginBottom: 12, alignItems: "center" }}>
        <div className="tabs">
          {TABS.map((t) => (
            <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="spacer" />
        {pending.length > 1 && (
          <button disabled={bulk.isPending}
            onClick={() => confirm(`Mark all ${pending.length} shown frames as correct team intros?`)
              && bulk.mutate({ ids: pending.map((k) => k.id), status: "confirmed" })}>
            Confirm all {pending.length} shown
          </button>
        )}
      </div>

      {frames.data?.length === 0 && (
        <p className="muted">
          {tab === "candidate"
            ? "Nothing to review. Pick a recording and click Find key frames."
            : "No key frames here."}
        </p>
      )}
      <div className="kf-grid">
        {frames.data?.map((k) => (
          <KeyFrameCard key={k.id} k={k} recLabel={recId ? undefined : recName(k.recording_id)}
            conflict={k.status === "rejected" && !!model?.conflicts?.includes(k.id)}
            busy={patch.isPending}
            onStatus={(status) => patch.mutate({ id: k.id, status })}
            onPick={(t) => patch.mutate({ id: k.id, t })} />
        ))}
      </div>
    </div>
  );
}

function KeyFrameCard({
  k, recLabel, conflict, busy, onStatus, onPick,
}: {
  k: KeyFrame; recLabel?: string; conflict: boolean; busy: boolean;
  onStatus: (s: KeyFrameStatus) => void; onPick: (t: number) => void;
}) {
  const chosen = k.alternatives.find((a) => Math.abs(a.t - k.t) < 0.05);
  return (
    <div className={`kf-card ${k.status}`}>
      <img className="kf-main" src={api.keyFrameImageUrl(k.id, k.t)} alt={`frame at ${hms(k.t)}`} />
      <div className="kf-body">
        <div className="row" style={{ alignItems: "center", gap: 8 }}>
          <b>{hms(k.t)}</b>
          <span className="muted">before {k.match_name || "match"} ({hms(k.anchor_start_s)})</span>
          <div className="spacer" />
          <span className={`pill ${k.status === "confirmed" ? "completed" : k.status === "rejected" ? "failed" : ""}`}>
            {k.status === "candidate" ? "to review" : k.status}
          </span>
        </div>
        {recLabel && <div className="muted" style={{ fontSize: 12 }}>{recLabel}</div>}
        {conflict && (
          <div className="badge warn" style={{ alignSelf: "flex-start" }}>
            Rejected, but looks just like the confirmed intros. Double-check it.
          </div>
        )}
        <div className="muted" style={{ fontSize: 12 }}>
          Same card on screen {hms(k.run_start_s)}–{hms(k.run_end_s)} (these frames are used for training)
          {chosen?.timer != null && <span className="error"> · match clock visible on this frame</span>}
        </div>
        {chosen && chosen.tokens.length > 0 && (
          <div className="team-list">
            {chosen.tokens.map((t) => <span key={t} className="team">{t}</span>)}
          </div>
        )}
        {k.alternatives.length > 1 && (
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 3 }}>Wrong frame? Pick another:</div>
            <div className="kf-alts">
              {k.alternatives.map((a) => (
                <button key={a.t} className={`kf-alt ${Math.abs(a.t - k.t) < 0.05 ? "selected" : ""}`}
                  title={`${hms(a.t)}${a.timer != null ? " · match clock visible" : ""}${a.tokens.length ? " · " + a.tokens.join(" ") : ""}`}
                  disabled={busy} onClick={() => onPick(a.t)}>
                  <img src={api.keyFrameImageUrl(k.id, a.t)} alt="" loading="lazy" />
                  <span className={`kf-tag ${a.timer != null ? "clock" : ""}`}>{hms(a.t).slice(2)}</span>
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="row" style={{ marginTop: 4 }}>
          {k.status !== "confirmed" && (
            <button className="primary sm" disabled={busy} onClick={() => onStatus("confirmed")}>✓ Team intro</button>
          )}
          {k.status !== "rejected" && (
            <button className="danger sm" disabled={busy} onClick={() => onStatus("rejected")}>✗ Not the intro</button>
          )}
          {k.status !== "candidate" && (
            <button className="sm" disabled={busy} onClick={() => onStatus("candidate")}>Undo</button>
          )}
        </div>
      </div>
    </div>
  );
}
