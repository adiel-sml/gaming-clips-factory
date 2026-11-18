export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Job = { id: string; type: string; status: "queued" | "running" | "done" | "failed"; error?: string | null };

export type Video = {
  id: string;
  title: string | null;
  source_type: string;
  source_url: string | null;
  duration: number;
  width: number;
  height: number;
  fps: number;
  status: string;
  error: string | null;
  game: string | null;
  language: string | null;
  options: Record<string, unknown>;
  stats: { analysis_timings?: Record<string, number>; words?: number; candidates?: number };
  highlight_count?: number;
  clip_count?: number;
  last_job?: Job | null;
  created_at: string;
};

export type Clip = {
  id: string;
  video_id: string;
  highlight_id: string;
  variant: string;
  start: number;
  end: number;
  duration: number;
  layout: string | null;
  crop: { x?: number; layout?: string; webcam_region?: number[] };
  subtitle_style: string | null;
  hook: { text?: string; duration?: number; alternatives?: string[]; source?: string };
  titles: string[];
  captions: string[];
  hashtags: string[];
  title: string | null;
  status: string;
  qc: { passed?: boolean; reasons?: string[]; warnings?: string[]; plan?: Record<string, unknown> };
  video_url: string | null;
  thumbnail_url: string | null;
  metadata_url: string | null;
  render_seconds: number;
  highlight?: Highlight | null;
};

export type Highlight = {
  id: string;
  video_id: string;
  rank: number;
  start: number;
  end: number;
  start_ts: string;
  end_ts: string;
  peak: number;
  duration: number;
  score: number;
  category: string;
  reason: string;
  confidence: number;
  transcript: string;
  components: Record<string, number>;
  status: string;
  clips?: Clip[];
  thumbnail_url?: string | null;
};

export type Stats = {
  videos: number;
  videos_failed: number;
  clips_total: number;
  clips_by_status: Record<string, number>;
  clips_pending: number;
  clips_needs_review: number;
  clips_validated: number;
  clips_rejected: number;
  avg_analysis_seconds: number;
  avg_render_seconds: number;
  jobs: Record<string, number>;
  errors: { video_id?: string; job_id?: string; title?: string; type?: string; error: string }[];
  best_highlights: Highlight[];
};

export type SystemInfo = {
  subtitle_presets: string[];
  layouts: string[];
  games: Record<string, string>;
  defaults: Record<string, string | number>;
  hardware: { cpu_count: number; gpu_available: boolean; gpu_name: string | null };
};

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  stats: () => request<Stats>("/stats"),
  system: () => request<SystemInfo>("/system"),
  videos: () => request<Video[]>("/videos"),
  video: (id: string) => request<Video>(`/videos/${id}`),
  createVideo: (body: unknown) =>
    request<{ video: Video; job: Job | null; already_imported: boolean }>("/videos", { method: "POST", body: JSON.stringify(body) }),
  uploadVideo: (form: FormData) =>
    request<{ video: Video; job: Job | null; already_imported: boolean }>("/videos/upload", { method: "POST", body: form }),
  analyze: (id: string, force = false) => request<{ job: Job }>(`/videos/${id}/analyze`, { method: "POST", body: JSON.stringify({ force }) }),
  highlights: (id: string) => request<Highlight[]>(`/videos/${id}/highlights`),
  generate: (id: string) => request<{ job: Job }>(`/highlights/${id}/generate`, { method: "POST", body: "{}" }),
  rejectHighlight: (id: string) => request<Highlight>(`/highlights/${id}/reject`, { method: "POST" }),
  clips: (status?: string) => request<Clip[]>(`/clips${status ? `?status=${status}` : ""}`),
  clip: (id: string) => request<Clip>(`/clips/${id}`),
  patchClip: (id: string, body: unknown) => request<Clip>(`/clips/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  renderClip: (id: string) => request<{ job: Job }>(`/clips/${id}/render`, { method: "POST" }),
  exportClip: (id: string, force = false) =>
    request<{ path: string; clip: Clip }>(`/clips/${id}/export`, { method: "POST", body: JSON.stringify({ force }) }),
  job: (id: string) => request<Job>(`/jobs/${id}`),
};

export const fileUrl = (path: string | null | undefined, bust?: string | number) =>
  path ? `${API_URL}${path}${bust ? `?v=${bust}` : ""}` : undefined;

export const fmt = (s: number) => {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  const h = Math.floor(m / 60);
  return h ? `${h}:${String(m % 60).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

export const CATEGORY_FR: Record<string, string> = {
  unexpected_reaction: "Réaction inattendue",
  funny: "Drôle",
  rage: "Rage",
  victory_clutch: "Victoire / clutch",
  fail: "Fail",
  action: "Action",
  quote: "Phrase marquante",
  rare_event: "Événement rare",
  tension_release: "Tension → explosion",
};
