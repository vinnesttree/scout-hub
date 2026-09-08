import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Match } from "../api/client";

export default function MatchDetail() {
  const { id } = useParams();
  const matchId = Number(id);
  const nav = useNavigate();
  const qc = useQueryClient();

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
