const BASE = "";

/** An HTTP error from the API; `status` 401 means "not signed in". */
export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---- types -----------------------------------------------------

export interface Health {
  status: string;
  version: string;
  tools: { ffmpeg: boolean; ffprobe: boolean; yt_dlp: boolean; ocr: boolean };
  ocr_detail: string;
}

export interface AppSettings {
  vexevents_token: string;
  vexevents_token_set: boolean;
  /** "env" = SCOUTHUB_VEXEVENTS_TOKEN / VEX_EVENTS_API_KEY is set and wins. */
  vexevents_token_source: "env" | "settings" | "none";
  clip_pad_start_s: number;
  clip_pad_end_s: number;
  sample_fps: number;
  /** frame rate the live splitter samples at while capturing */
  live_sample_fps: number;
  keep_full_recording: boolean;
  overlay_profile: string;
  /** use the trained team-intro detector (when one has been trained) */
  keyframe_detection: boolean;
  /** intro-only streams: how long a match runs after its intro card */
  intro_match_length_s: number;
  overlay_profiles: string[];
  archive_root: string;
}

export interface DirEntry { name: string; path: string; has_children: boolean; }
export interface DirListing { path: string; parent: string | null; entries: DirEntry[]; }

export interface EventOut {
  id: number; sku: string; re_event_id: number | null; name: string;
  season: string; program: string; start_date: string | null;
}
export interface EventSearchOut {
  sku: string; re_event_id: number; name: string; season: string;
  program: string; start_date: string | null; divisions: { id: number; name: string }[];
}

export type RecordingStatus =
  | "pending" | "recording" | "finalizing" | "completed" | "failed" | "interrupted";

export interface Recording {
  id: number; source_url: string; source_type: string; status: RecordingStatus;
  archive_dir: string; file_path: string | null; title: string;
  live_from_start: boolean; is_live: boolean | null; sections: string | null;
  progress: string; error: string | null;
  duration_s: number | null; started_at: string | null; ended_at: string | null;
  event_id: number | null; created_at: string; match_count: number;
  /** live match splitting, which runs while the video is captured */
  split_status: "" | "running" | "done" | "failed"; split_detail: string;
  analyzed_s: number | null; video_started_at: string | null;
}

export interface TeamRef { number: string; name: string; station: number; }

export interface Match {
  id: number; recording_id: number | null; event_id: number | null;
  name: string; round: string; match_num: number | null; scheduled_at: string | null;
  video_start_s: number | null; video_end_s: number | null;
  detection_confidence: number | null; detection_method: string; status: string;
  review_note: string; red_score: number | null; blue_score: number | null;
  red_teams: TeamRef[]; blue_teams: TeamRef[]; has_clip: boolean;
  winner: "red" | "blue" | "tie" | null;
  /** teams/score are on-screen estimates still waiting for VEX Events data */
  provisional: boolean;
}

export interface Job {
  id: number; kind: string; recording_id: number | null; status: string;
  detail: string; error: string | null; created_at: string;
  started_at: string | null; finished_at: string | null;
}

export type KeyFrameStatus = "candidate" | "confirmed" | "rejected";

export interface KeyFrameAlt {
  t: number; score: number;
  /** match clock read on this frame (so it is the match, not the intro) */
  timer: number | null;
  /** team-number-like text OCR found on the frame */
  tokens: string[];
}

export interface KeyFrame {
  id: number; recording_id: number; match_id: number | null; match_name: string;
  anchor_start_s: number; anchor_end_s: number;
  t: number; run_start_s: number; run_end_s: number; score: number;
  status: KeyFrameStatus; alternatives: KeyFrameAlt[];
  created_at: string; reviewed_at: string | null;
}

export interface KeyFrameModelInfo {
  trained: boolean; trained_at?: string; threshold?: number;
  confirmed?: number; rejected?: number;
  positive_frames?: number; negative_frames?: number; cv_folds?: number;
  cv?: {
    precision: number; recall: number; f1: number; false_positives: number;
    missed: number; intros_found: number; intros_total: number;
  };
  recordings?: number[]; warning?: string;
  /** rejected key frames that look just like confirmed intros (likely review slips) */
  conflicts?: number[];
}

export interface KeyFrameSummary {
  recordings: ({ recording_id: number } & Record<KeyFrameStatus, number>)[];
  totals: Record<KeyFrameStatus, number>;
  min_confirmed: number;
  model: KeyFrameModelInfo;
}

export interface User {
  id: number; username: string; is_admin: boolean;
  created_at: string; last_login_at: string | null;
}
export interface Me { user: User | null; setup_required: boolean; }

// ---- endpoints ----------------------------------------------

export const api = {
  health: () => req<Health>("/api/health"),

  me: () => req<Me>("/api/auth/me"),
  login: (username: string, password: string) =>
    req<Me>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  setup: (username: string, password: string) =>
    req<Me>("/api/auth/setup", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => req<void>("/api/auth/logout", { method: "POST" }),

  listUsers: () => req<User[]>("/api/users"),
  createUser: (body: { username: string; password: string; is_admin: boolean }) =>
    req<User>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (id: number, body: { password?: string; current_password?: string; is_admin?: boolean }) =>
    req<User>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteUser: (id: number) => req<void>(`/api/users/${id}`, { method: "DELETE" }),

  getSettings: () => req<AppSettings>("/api/settings"),
  putSettings: (body: Partial<AppSettings>) =>
    req<AppSettings>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
  testVexEvents: () =>
    req<{ ok: boolean; source: string }>("/api/settings/test-vexevents", { method: "POST" }),

  listDirs: (path?: string) =>
    req<DirListing>("/api/fs/dirs" + (path ? `?path=${encodeURIComponent(path)}` : "")),
  makeDir: (path: string, name: string) =>
    req<DirListing>("/api/fs/dirs", { method: "POST", body: JSON.stringify({ path, name }) }),

  listEvents: () => req<EventOut[]>("/api/events"),
  /** `q` = an events.vex.com event link or an event code (RE-V5RC-26-4058). */
  searchEvent: (q: string) =>
    req<EventSearchOut>(`/api/events/search?q=${encodeURIComponent(q)}`),
  importEvent: (sku: string) =>
    req<EventOut>("/api/events/import", { method: "POST", body: JSON.stringify({ sku }) }),
  deleteEvent: (id: number) => req<void>(`/api/events/${id}`, { method: "DELETE" }),

  listRecordings: () => req<Recording[]>("/api/recordings"),
  createRecording: (body: {
    source_url: string; archive_dir?: string | null; event_id?: number | null;
    /** events.vex.com event link or event code; imported automatically. */
    event?: string | null;
    live_from_start?: boolean; sections?: string | null;
  }) => req<Recording>("/api/recordings", { method: "POST", body: JSON.stringify(body) }),
  stopRecording: (id: number) =>
    req<Recording>(`/api/recordings/${id}/stop`, { method: "POST" }),
  processRecording: (id: number) =>
    req<{ job_id: number }>(`/api/recordings/${id}/process`, { method: "POST" }),
  refreshRecordingApi: (id: number) =>
    req<{ job_id: number }>(`/api/recordings/${id}/refresh-api`, { method: "POST" }),
  deleteRecording: (id: number, deleteFiles: boolean) =>
    req<void>(`/api/recordings/${id}?delete_files=${deleteFiles}`, { method: "DELETE" }),

  listMatches: (params: Record<string, string | number | boolean | undefined>) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") q.set(k, String(v));
    }
    return req<Match[]>(`/api/matches?${q.toString()}`);
  },
  getMatch: (id: number) => req<Match>(`/api/matches/${id}`),
  patchMatch: (id: number, body: Record<string, unknown>) =>
    req<Match>(`/api/matches/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  reclipMatch: (id: number) => req<Match>(`/api/matches/${id}/reclip`, { method: "POST" }),
  deleteMatch: (id: number, deleteFiles: boolean) =>
    req<void>(`/api/matches/${id}?delete_files=${deleteFiles}`, { method: "DELETE" }),
  clipUrl: (id: number) => `/api/matches/${id}/clip`,
  thumbUrl: (id: number) => `/api/matches/${id}/thumb`,

  listKeyFrames: (params: { recording_id?: number; status?: string }) => {
    const q = new URLSearchParams();
    if (params.recording_id) q.set("recording_id", String(params.recording_id));
    if (params.status) q.set("status", params.status);
    return req<KeyFrame[]>(`/api/keyframes?${q.toString()}`);
  },
  keyFrameSummary: () => req<KeyFrameSummary>("/api/keyframes/summary"),
  findKeyFrames: (recordingId: number) =>
    req<{ job_id: number }>(`/api/keyframes/find?recording_id=${recordingId}`, { method: "POST" }),
  patchKeyFrame: (id: number, body: { status?: KeyFrameStatus; t?: number }) =>
    req<KeyFrame>(`/api/keyframes/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  bulkKeyFrames: (ids: number[], status: KeyFrameStatus) =>
    req<{ updated: number }>("/api/keyframes/bulk", { method: "POST", body: JSON.stringify({ ids, status }) }),
  deleteKeyFrames: (recordingId: number) =>
    req<void>(`/api/keyframes?recording_id=${recordingId}`, { method: "DELETE" }),
  trainKeyFrames: () => req<KeyFrameModelInfo>("/api/keyframes/train", { method: "POST" }),
  deleteKeyFrameModel: () => req<void>("/api/keyframes/model", { method: "DELETE" }),
  keyFrameImageUrl: (id: number, t: number) => `/api/keyframes/${id}/image?t=${t}`,

  listJobs: (recordingId?: number) =>
    req<Job[]>("/api/jobs" + (recordingId ? `?recording_id=${recordingId}` : "")),
};
