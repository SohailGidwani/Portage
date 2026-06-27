// Thin REST client. NO ORM, NO DB — the backend is the single schema source of truth.
// Browser-side calls hit the host-mapped API port (set via NEXT_PUBLIC_API_BASE).

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export type Job = {
  id: string;
  repo_url: string;
  migration_recipe: string;
  status: "queued" | "running" | "done" | "failed";
  config: Record<string, unknown>;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export async function getHealth(): Promise<{ status: string; db: string }> {
  const r = await fetch(`${API_BASE}/health`, { cache: "no-store" });
  if (!r.ok) throw new Error(`health ${r.status}`);
  return r.json();
}

export async function listJobs(): Promise<Job[]> {
  const r = await fetch(`${API_BASE}/jobs`, { cache: "no-store" });
  if (!r.ok) throw new Error(`listJobs ${r.status}`);
  return r.json();
}

export async function createJob(input: {
  repo_url: string;
  migration_recipe: string;
}): Promise<Job> {
  const r = await fetch(`${API_BASE}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!r.ok) throw new Error(`createJob ${r.status}`);
  return r.json();
}
