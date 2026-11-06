import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Match, TeamRef } from "../api/client";
import { useAuth } from "../auth";

function fmtTime(s: number | null) {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

function Teams({ teams, side }: { teams: TeamRef[]; side: "red" | "blue" }) {
  return (
    <div className={`team-list alliance-${side}`}>
      {teams.length ? teams.map((t) => (
        <Link key={t.number} className="team" to={`/matches?team=${encodeURIComponent(t.number)}`}
          title={t.name || undefined}>{t.number}</Link>
      )) : <span className="muted">—</span>}
    </div>
  );
}

/** What visitors without an account see: the clip and the match facts, no editing. */
function ReadOnlyMatch({ m, onClose }: { m: Match; onClose: () => void }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="row">
          <h1 style={{ margin: 0 }}>{m.name || `Match ${m.id}`}</h1>
          <span className="pill">{m.round}</span>
          <div className="spacer" />
          <button className="sm" onClick={onClose}>Close</button>
        </div>
        {m.has_clip ? (
          <video src={api.clipUrl(m.id)} controls preload="metadata" />
        ) : (
          <div className="panel muted">No clip for this match.</div>
        )}
        <div className="grid2" style={{ marginTop: 12 }}>
          <div className="field">
            <label>Red alliance</label>
            <Teams teams={m.red_teams} side="red" />
          </div>
          <div className="field">
            <label>Blue alliance</label>
            <Teams teams={m.blue_teams} side="blue" />
          </div>
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <div className="field">
            <label>Score</label>
            <div>
              <span className={`score red ${m.winner === "red" ? "win" : ""}`}>{m.red_score ?? "?"}</span>
              <span className="muted"> – </span>
              <span className={`score blue ${m.winner === "blue" ? "win" : ""}`}>{m.blue_score ?? "?"}</span>
            </div>
          </div>
          {m.scheduled_at && (
            <div className="field">
              <label>Scheduled</label>
              <div>{new Date(m.scheduled_at).toLocaleString()}</div>
            </div>
          )}
          <div className="field">
            <label>In recording</label>
            <div>{fmtTime(m.video_start_s)} – {fmtTime(m.video_end_s)}</div>
          </div>
        </div>
        <p className="muted" style={{ marginTop: 14, fontSize: 12 }}>
          <Link to="/login">Sign in</Link> to correct teams, scores or clip boundaries.
        </p>
      </div>
    </div>
  );
}

export default function MatchDetail() {
  const { id } = useParams();
  const matchId = Number(id);
  const nav = useNavigate();
  const qc = useQueryClient();
  const { user } = useAuth();

  const match = useQuery({
    queryKey: ["match", matchId],
    queryFn: () => api.getMatch(matchId),
    enabled: Number.isFinite(matchId),
  });

  const [form, setForm] = useState<Partial<Match> & { red?: string; blue?: string }>({});
  useEffect(() => {
    if (match.data) {
      setForm({
        name: match.data.name,
        match_num: match.data.match_num ?? undefined,
        round: match.data.round,
        video_start_s: match.data.video_start_s ?? undefined,
        video_end_s: match.data.video_end_s ?? undefined,
        red_score: match.data.red_score ?? undefined,
        blue_score: match.data.blue_score ?? undefined,
        red: match.data.red_teams.map((t) => t.number).join(", "),
        blue: match.data.blue_teams.map((t) => t.number).join(", "),
      });
    }
  }, [match.data]);

  const save = useMutation({
    mutationFn: (reclip: boolean) =>
      api.patchMatch(matchId, {
        name: form.name,
        match_num: form.match_num ?? null,
        round: form.round,
        video_start_s: form.video_start_s ?? null,
        video_end_s: form.video_end_s ?? null,
        red_score: form.red_score ?? null,
        blue_score: form.blue_score ?? null,
        red_teams: (form.red ?? "").split(",").map((s) => s.trim()).filter(Boolean),
        blue_teams: (form.blue ?? "").split(",").map((s) => s.trim()).filter(Boolean),
        reclip,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["match", matchId] });
      qc.invalidateQueries({ queryKey: ["matches"] });
    },
  });

  const reclip = useMutation({
    mutationFn: () => api.reclipMatch(matchId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["match", matchId] }),
  });

  if (match.isLoading) return <div className="modal-backdrop"><div className="modal">Loading…</div></div>;
  if (match.error || !match.data)
    return (
      <div className="modal-backdrop" onClick={() => nav(-1)}>
        <div className="modal error">Match not found.</div>
      </div>
    );

  const m = match.data;
  if (!user) return <ReadOnlyMatch m={m} onClose={() => nav("/matches")} />;
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  return (
    <div className="modal-backdrop" onClick={() => nav("/matches")}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="row">
          <h1 style={{ margin: 0 }}>{m.name || `Match ${m.id}`}</h1>
          <span className="pill">{m.detection_method}</span>
          {m.detection_confidence != null && (
            <span className="muted">conf {(m.detection_confidence * 100).toFixed(0)}%</span>
          )}
          <div className="spacer" />
          <button className="sm" onClick={() => nav("/matches")}>Close</button>
        </div>

        {m.has_clip ? (
          <video src={api.clipUrl(m.id)} controls preload="metadata" />
        ) : (
          <div className="panel muted">
            No clip yet. {m.video_start_s == null
              ? "This match has no video boundaries."
              : "Set boundaries below and re-clip."}
          </div>
        )}
        {m.review_note && <p className="muted">⚠ {m.review_note}</p>}

        <div className="grid2">
          <div className="stack">
            <div className="field">
              <label>Name</label>
              <input value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} />
            </div>
            <div className="row">
              <div className="field">
                <label>Round</label>
                <select value={form.round ?? "unknown"} onChange={(e) => set({ round: e.target.value })}>
                  {["practice", "qual", "r16", "qf", "sf", "final", "unknown"].map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Match #</label>
                <input type="number" value={form.match_num ?? ""} style={{ width: 90 }}
                  onChange={(e) => set({ match_num: e.target.value ? Number(e.target.value) : undefined })} />
              </div>
            </div>
          </div>
          <div className="stack">
            <div className="row">
              <div className="field">
                <label>Start (s)</label>
                <input type="number" step="0.1" style={{ width: 110 }} value={form.video_start_s ?? ""}
                  onChange={(e) => set({ video_start_s: e.target.value ? Number(e.target.value) : undefined })} />
              </div>
              <div className="field">
                <label>End (s)</label>
                <input type="number" step="0.1" style={{ width: 110 }} value={form.video_end_s ?? ""}
                  onChange={(e) => set({ video_end_s: e.target.value ? Number(e.target.value) : undefined })} />
              </div>
            </div>
            <div className="row">
              <div className="field">
                <label>Red score</label>
                <input type="number" style={{ width: 90 }} value={form.red_score ?? ""}
                  onChange={(e) => set({ red_score: e.target.value ? Number(e.target.value) : undefined })} />
              </div>
              <div className="field">
                <label>Blue score</label>
                <input type="number" style={{ width: 90 }} value={form.blue_score ?? ""}
                  onChange={(e) => set({ blue_score: e.target.value ? Number(e.target.value) : undefined })} />
              </div>
            </div>
          </div>
        </div>

        <div className="grid2" style={{ marginTop: 12 }}>
          <div className="field">
            <label>Red alliance (comma-separated)</label>
            <input value={form.red ?? ""} onChange={(e) => set({ red: e.target.value })} />
          </div>
          <div className="field">
            <label>Blue alliance (comma-separated)</label>
            <input value={form.blue ?? ""} onChange={(e) => set({ blue: e.target.value })} />
          </div>
        </div>

        {save.error && <p className="error">{String((save.error as Error).message)}</p>}
        {reclip.error && <p className="error">{String((reclip.error as Error).message)}</p>}

        <div className="row" style={{ marginTop: 14 }}>
          <button className="primary" disabled={save.isPending} onClick={() => save.mutate(true)}>
            {save.isPending ? "Saving…" : "Save & re-clip"}
          </button>
          <button disabled={save.isPending} onClick={() => save.mutate(false)}>Save only</button>
          <button disabled={reclip.isPending || m.video_start_s == null}
            onClick={() => reclip.mutate()}>
            Re-clip from current boundaries
          </button>
          <div className="spacer" />
          <button className="danger" onClick={() => {
            if (confirm("Delete this match and its clip?"))
              api.deleteMatch(m.id, true).then(() => nav("/matches"));
          }}>Delete match</button>
        </div>
      </div>
    </div>
  );
}
