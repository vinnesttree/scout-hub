import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Match } from "../api/client";

const ROUNDS = ["", "practice", "qual", "r16", "qf", "sf", "final", "unknown"];

function TeamCells({ m }: { m: Match }) {
  return (
    <>
      <td className="alliance-red">
        <div className="team-list">
          {m.red_teams.map((t) => <span key={t.number} className="team">{t.number}</span>)}
        </div>
      </td>
      <td className="alliance-blue">
        <div className="team-list">
          {m.blue_teams.map((t) => <span key={t.number} className="team">{t.number}</span>)}
        </div>
      </td>
    </>
  );
}

function ScoreCell({ m }: { m: Match }) {
  if (m.red_score == null && m.blue_score == null) return <td className="muted">—</td>;
  return (
    <td>
      <span className={`score red ${m.winner === "red" ? "win" : ""}`}>{m.red_score ?? "?"}</span>
      <span className="muted"> – </span>
      <span className={`score blue ${m.winner === "blue" ? "win" : ""}`}>{m.blue_score ?? "?"}</span>
    </td>
  );
}

export default function MatchLibrary() {
  const nav = useNavigate();
  const events = useQuery({ queryKey: ["events"], queryFn: api.listEvents });

  const [params] = useSearchParams();
  const [team, setTeam] = useState(params.get("team") ?? "");
  const [recordingId, setRecordingId] = useState(params.get("recording") ?? "");
  const [eventId, setEventId] = useState("");
  const [round, setRound] = useState("");
  const [onlyClips, setOnlyClips] = useState(false);
  const [sort, setSort] = useState("scheduled_at");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [groupByTeam, setGroupByTeam] = useState(false);

  const matches = useQuery({
    queryKey: ["matches", { team, eventId, recordingId, round, onlyClips, sort, order }],
    // matches appear (and get VEX Events data) while a stream is still recording
    refetchInterval: 15_000,
    queryFn: () =>
      api.listMatches({
        team: team.trim().toUpperCase() || undefined,
        recording_id: recordingId || undefined,
        event_id: eventId || undefined,
        round: round || undefined,
        has_clip: onlyClips ? true : undefined,
        sort,
        order,
      }),
  });

  const toggleSort = (key: string) => {
    if (sort === key) setOrder((o) => (o === "asc" ? "desc" : "asc"));
    else {
      setSort(key);
      setOrder("asc");
    }
  };
  const sortMark = (key: string) => (sort === key ? (order === "asc" ? " ▲" : " ▼") : "");

  const grouped = useMemo(() => {
    if (!groupByTeam || !matches.data) return null;
    const map = new Map<string, { wins: number; losses: number; ties: number; rows: Match[] }>();
    for (const m of matches.data) {
      for (const t of [...m.red_teams, ...m.blue_teams]) {
        const g = map.get(t.number) ?? { wins: 0, losses: 0, ties: 0, rows: [] };
        g.rows.push(m);
        const onRed = m.red_teams.some((x) => x.number === t.number);
        if (m.winner === "tie") g.ties++;
        else if (m.winner && (m.winner === "red") === onRed) g.wins++;
        else if (m.winner) g.losses++;
        map.set(t.number, g);
      }
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [groupByTeam, matches.data]);

  return (
    <div>
      <h1>Match Library</h1>

      <div className="panel">
        <div className="row">
          <div className="field">
            <label>Team number</label>
            <input value={team} onChange={(e) => setTeam(e.target.value)} placeholder="e.g. 1234A" />
          </div>
          <div className="field">
            <label>Event</label>
            <select value={eventId} onChange={(e) => setEventId(e.target.value)}>
              <option value="">All events</option>
              {events.data?.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Round</label>
            <select value={round} onChange={(e) => setRound(e.target.value)}>
              {ROUNDS.map((r) => <option key={r} value={r}>{r || "All rounds"}</option>)}
            </select>
          </div>
          {recordingId && (
            <span className="badge" style={{ marginBottom: 6 }}>
              Recording #{recordingId}{" "}
              <button type="button" className="sm" onClick={() => setRecordingId("")}>×</button>
            </span>
          )}
          <label style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6 }}>
            <input type="checkbox" checked={onlyClips} onChange={(e) => setOnlyClips(e.target.checked)} />
            Has clip
          </label>
          <label style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6 }}>
            <input type="checkbox" checked={groupByTeam} onChange={(e) => setGroupByTeam(e.target.checked)} />
            Group by team
          </label>
        </div>
      </div>

      {matches.isLoading && <p className="muted">Loading…</p>}
      {matches.error && <p className="error">{String((matches.error as Error).message)}</p>}

      {grouped ? (
        grouped.map(([number, g]) => (
          <div key={number} className="panel">
            <div className="row">
              <h2 style={{ margin: 0 }}>{number}</h2>
              <span className="muted">
                {g.wins}-{g.losses}{g.ties ? `-${g.ties}` : ""} · {g.rows.length} matches
              </span>
            </div>
            <MatchTable rows={g.rows} onOpen={(id) => nav(`/matches/${id}`)}
              toggleSort={toggleSort} sortMark={sortMark} highlightTeam={number} />
          </div>
        ))
      ) : (
        <MatchTable rows={matches.data ?? []} onOpen={(id) => nav(`/matches/${id}`)}
          toggleSort={toggleSort} sortMark={sortMark} />
      )}
    </div>
  );
}

function MatchTable({
  rows, onOpen, toggleSort, sortMark, highlightTeam,
}: {
  rows: Match[];
  onOpen: (id: number) => void;
  toggleSort: (k: string) => void;
  sortMark: (k: string) => string;
  highlightTeam?: string;
}) {
  return (
    <table>
      <thead>
        <tr>
          <th className="no-sort"></th>
          <th onClick={() => toggleSort("match_num")}>Match{sortMark("match_num")}</th>
          <th onClick={() => toggleSort("scheduled_at")}>Scheduled{sortMark("scheduled_at")}</th>
          <th onClick={() => toggleSort("team")}>Red{sortMark("team")}</th>
          <th className="no-sort">Blue</th>
          <th onClick={() => toggleSort("red_score")}>Score{sortMark("red_score")}</th>
          <th className="no-sort">Source</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((m) => (
          <tr key={m.id} className="clickable" onClick={() => onOpen(m.id)}
            style={highlightTeam &&
              [...m.red_teams, ...m.blue_teams].some((t) => t.number === highlightTeam)
              ? { outline: "1px solid var(--accent)" } : undefined}>
            <td>
              {m.has_clip
                ? <img className="thumb" src={api.thumbUrl(m.id)} alt="" loading="lazy" />
                : <div className="thumb" />}
            </td>
            <td>
              <div>{m.name || `#${m.match_num ?? "?"}`}</div>
              <div className="muted" style={{ fontSize: 11 }}>{m.round}</div>
            </td>
            <td className="muted">
              {m.scheduled_at ? new Date(m.scheduled_at).toLocaleString() : "—"}
            </td>
            <TeamCells m={m} />
            <ScoreCell m={m} />
            <td>
              <span className="pill">{m.detection_method}</span>
              {m.provisional && (
                <span className="pill estimate" title={m.review_note}> estimate</span>
              )}
              {!m.has_clip && m.video_start_s == null && (
                <span className="muted" style={{ fontSize: 11 }}> no video</span>
              )}
            </td>
          </tr>
        ))}
        {rows.length === 0 && <tr><td colSpan={7} className="muted">No matches.</td></tr>}
      </tbody>
    </table>
  );
}
