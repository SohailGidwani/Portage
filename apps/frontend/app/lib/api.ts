// Thin REST client. NO ORM, NO DB — the backend is the single schema source of truth.
// Browser-side calls hit the host-mapped API port (set via NEXT_PUBLIC_API_URL).

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ---------------------------------------------------------------------------- auth
// Access JWT lives in module memory only (dies with the tab). The refresh token is an
// httpOnly cookie scoped to /auth — JS never sees it. `authedFetch` attaches the bearer
// and, on 401, tries one refresh before giving up (AUTH_MODE=disabled backends never
// 401, so local dev works with zero ceremony).

let accessToken: string | null = null;

export type Me = {
  login: string;
  role: string;
  avatar_url: string | null;
  auth_mode: string;
};

export async function tryRefresh(): Promise<Me | null> {
  const r = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    credentials: "include",
  });
  if (!r.ok) return null;
  const data = await r.json();
  accessToken = data.access_token;
  return data.user;
}

export async function getMe(): Promise<Me | null> {
  const r = await authedFetch(`${API_BASE}/auth/me`);
  return r.ok ? r.json() : null;
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "include" });
  accessToken = null;
}

export function loginUrl(): string {
  return `${API_BASE}/auth/github/login`;
}

async function authedFetch(url: string, init?: RequestInit): Promise<Response> {
  const withAuth = (): RequestInit => ({
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    cache: "no-store",
  });
  let r = await fetch(url, withAuth());
  if (r.status === 401 && (await tryRefresh())) {
    r = await fetch(url, withAuth());
  }
  return r;
}

export type ApiKeySummary = {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
};

export async function listApiKeys(): Promise<ApiKeySummary[]> {
  const r = await authedFetch(`${API_BASE}/auth/keys`);
  if (!r.ok) throw new Error(`listApiKeys ${r.status}`);
  return r.json();
}

export async function createApiKey(name: string): Promise<{ name: string; key: string; note: string }> {
  const r = await authedFetch(`${API_BASE}/auth/keys`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) throw new Error(`createApiKey ${r.status}`);
  return r.json();
}

export async function revokeApiKey(id: string): Promise<void> {
  const r = await authedFetch(`${API_BASE}/auth/keys/${id}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`revokeApiKey ${r.status}`);
}

export type TestSummary = {
  total: number;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  duration_seconds: number;
};

export type GraphSummary = {
  files_parsed: number;
  total_nodes: number;
  total_edges: number;
};

export type Job = {
  id: string;
  repo_url: string;
  migration_recipe: string;
  status: "queued" | "running" | "done" | "failed";
  config: Record<string, unknown>;
  error: string | null;
  report_path: string | null;
  test_summary: TestSummary | null;
  graph_summary: GraphSummary | null;
  created_at: string;
  updated_at: string;
};

// One migration attempt / recovery action on a task (Phase 3 attempts_log entry).
export type AttemptEntry = {
  attempt?: number;
  tier?: "driver" | "escalation" | "deterministic";
  model?: string;
  action?: string;
  reason?: string;
  visit?: number;
  at?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  cost_usd?: number;
  violations?: string[];
};

export type Subtask = {
  id: string;
  type: string;
  title: string;
  status: string;
};

export type Task = {
  id: string;
  type: string;
  title: string;
  target_path: string | null;
  status: "pending" | "running" | "done" | "skipped" | "failed";
  attempts: number;
  order_index: number;
  verify_spec: { affected_tests?: string[]; subtasks?: string[] };
  content_hash: string | null;
  error: string | null;
  diff: string | null;
  attempts_log: AttemptEntry[];
  subtasks: Subtask[];
};

export type RecoverySummary = {
  visits: number;
  actions: {
    visit?: number;
    classification?: string;
    action?: string;
    targets?: string[];
    skipped?: string[];
    at?: string;
  }[];
  tasks_skipped: number;
  escalation_attempted: number;
  escalation_rescued: number;
  integration_visits?: number;
  no_progress_retries?: number;
  last_classification?: string | null;
};

export type OracleIntegrity = {
  protected_files: number;
  checked_files: number;
  clean_files: number;
  integrity_rate: number;
  violations: { path: string; violations: string[] }[];
  files?: { path: string; strategy: string; byte_preserved: boolean; violations: string[] }[];
};

export type LlmUsage = {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
};

export type VerifiedBatch = {
  batch?: number;
  tasks?: string[];
  passed?: boolean;
  summary?: TestSummary;
};

export type Report = {
  job_id?: string;
  repo_url?: string;
  migration_recipe?: string;
  migrated: boolean;
  migration_outcome?: "success" | "failed" | "unsupported" | "not_applicable";
  tasks_total: number;
  tasks_done: number;
  affected_tests: string[];
  unsupported_test_seams?: string[];
  oracle_integrity?: OracleIntegrity;
  verified_batches?: VerifiedBatch[];
  llm_usage?: LlmUsage;
  recovery?: RecoverySummary;
  verify_summary?: TestSummary;
  integrate_summary?: TestSummary;
  test_summary?: TestSummary;
  diff: string;
};

export type EvalRun = {
  id: string;
  suite: string;
  corpus_name: string;
  scenario: string;
  k_index: number;
  job_id: string | null;
  status: "green" | "red" | "error" | "timeout";
  tests_passed: number;
  tests_total: number;
  tasks_total?: number;
  tasks_done?: number;
  tasks_skipped?: number;
  recover_visits: number;
  cost_usd: number;
  wall_seconds: number;
  created_at: string;
};

export async function listEvalRuns(scenario?: string): Promise<EvalRun[]> {
  const q = scenario ? `?scenario=${encodeURIComponent(scenario)}&limit=50` : "";
  const r = await fetch(`${API_BASE}/eval/runs${q}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`listEvalRuns ${r.status}`);
  return r.json();
}

export type LeaderboardRow = {
  corpus_name: string;
  tier: string;
  scenario: string;
  runs: number;
  green: number;
  green_rate: number;
  test_pass_mean: number;
  test_pass_variance: number;
  completion_mean?: number;
  cost_mean: number;
  wall_mean: number;
  recover_visits_mean: number;
  escalation_attempted: number;
  escalation_rescued: number;
};

export type Leaderboard = { suites: string[]; rows: LeaderboardRow[] };

export async function getLeaderboard(suites?: string[]): Promise<Leaderboard> {
  const q = suites && suites.length ? `?suites=${encodeURIComponent(suites.join(","))}` : "";
  const r = await fetch(`${API_BASE}/eval/leaderboard${q}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`getLeaderboard ${r.status}`);
  return r.json();
}

export async function getHealth(): Promise<{ status: string; db: string }> {
  const r = await fetch(`${API_BASE}/health`, { cache: "no-store" });
  if (!r.ok) throw new Error(`health ${r.status}`);
  return r.json();
}

export async function getJob(id: string): Promise<Job> {
  const r = await authedFetch(`${API_BASE}/jobs/${id}`);
  if (!r.ok) throw new Error(`getJob ${r.status}`);
  return r.json();
}

export async function getJobTasks(id: string): Promise<Task[]> {
  const r = await authedFetch(`${API_BASE}/jobs/${id}/tasks`);
  if (!r.ok) throw new Error(`getJobTasks ${r.status}`);
  return r.json();
}

export async function getJobReport(id: string): Promise<Report | null> {
  const r = await authedFetch(`${API_BASE}/jobs/${id}/report`);
  if (r.status === 404) return null; // no report yet (job still running)
  if (!r.ok) throw new Error(`getJobReport ${r.status}`);
  return r.json();
}

export async function listJobs(): Promise<Job[]> {
  const r = await authedFetch(`${API_BASE}/jobs`);
  if (!r.ok) throw new Error(`listJobs ${r.status}`);
  return r.json();
}

export async function createJob(input: {
  repo_url: string;
  migration_recipe: string;
  config?: Record<string, unknown>;
}): Promise<Job> {
  const r = await authedFetch(`${API_BASE}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!r.ok) throw new Error(`createJob ${r.status}`);
  return r.json();
}
