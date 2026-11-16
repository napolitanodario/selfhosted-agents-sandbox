import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

export type Attempt = {
  id: string;
  number: number;
  scheduler: string;
  pipeline: string;
  evaluation: string;
  artifacts: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  job_id: string | null;
  exit_code: number | null;
  metrics: Record<string, number>;
  issues: string[];
};
export type Run = {
  id: string;
  project_id: string;
  experiment_id: string;
  adapter: string;
  profile: string;
  backend: string;
  status: string;
  created_at: string;
  updated_at: string;
  observation_stale: boolean;
  source_revision: string | null;
  parameters: Record<string, unknown>;
  hypothesis: string | null;
  attempts: Attempt[];
};
export type Campaign = {
  id: string;
  project_id: string;
  objective: string;
  rubric: string;
  phase: string;
  error: string | null;
  created_at: string;
  updated_at: string | null;
  max_runs: number;
  baseline_run: string | null;
  runs: string[];
  iterations: Record<string, unknown>[];
  adapter: string;
};
export type AgentSession = {
  id: string;
  campaign_id: string;
  project_id: string;
  role: string;
  iteration: number;
  task_id: string;
  provider: string;
  status: string;
  display_status: string;
  phase: string;
  started_at: string;
  finished_at: string | null;
  heartbeat_at: string;
  last_activity_at: string;
  command_count: number;
  command_errors: number;
  output_bytes: number;
  events_truncated: boolean;
  dropped_events: number;
  sandbox_id?: string;
  run_id?: string;
  error?: string;
  prompt?: string;
  response?: Record<string, unknown>;
};
export type HostSample = {
  time: string;
  hostname: string;
  cpu_percent: number;
  cpu_count: number;
  ram_used_bytes: number;
  ram_total_bytes: number;
  disk_used_bytes: number;
  disk_total_bytes: number;
  dashboard_rss_bytes: number;
};
export type Snapshot = {
  runs: Run[];
  campaigns: Campaign[];
  sessions: AgentSession[];
  projects: string[];
  host_samples: HostSample[];
  notices: string[];
  ledger_available: boolean;
  history_limited: boolean;
};
export type RecordedEvent = {
  sequence: number;
  time: string;
  kind: string;
  payload: Record<string, unknown>;
};
export type Artifact = { path: string; bytes: number; sha256: string };

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function useMonitor(enabled: boolean) {
  const client = useQueryClient();
  const [connection, setConnection] = useState("connecting");
  const [received, setReceived] = useState<Date | null>(null);
  const query = useQuery({
    queryKey: ["snapshot"],
    queryFn: () => request<Snapshot>("/api/snapshot"),
    enabled,
    // Polling also recovers state if an intermediary interrupts the event stream.
    refetchInterval: 15_000,
  });
  useEffect(() => {
    if (!enabled) return;
    let events: EventSource | undefined;
    function connect() {
      events?.close();
      if (!navigator.onLine) {
        setConnection("reconnecting");
        return;
      }
      setConnection("connecting");
      const stream = new EventSource("/api/stream");
      events = stream;
      stream.onopen = () => setConnection("live");
      stream.onerror = () => setConnection("reconnecting");
      stream.addEventListener("snapshot", (event) => {
        try {
          client.setQueryData(["snapshot"], JSON.parse(event.data) as Snapshot);
          setReceived(new Date());
        } catch {
          setConnection("reconnecting");
        }
      });
      stream.addEventListener("auth_expired", () => {
        stream.close();
        client.setQueryData(["auth"], {
          authenticated: false,
          local_access: false,
        });
      });
    }
    function disconnect() {
      // Browsers may leave an established stream open when the device goes
      // offline. Reflect that immediately instead of keeping a stale live badge.
      events?.close();
      setConnection("reconnecting");
    }
    window.addEventListener("online", connect);
    window.addEventListener("offline", disconnect);
    connect();
    return () => {
      events?.close();
      window.removeEventListener("online", connect);
      window.removeEventListener("offline", disconnect);
    };
  }, [enabled, client]);
  return { ...query, connection, received };
}

export const finalRun = (status: string) =>
  ["succeeded", "failed", "cancelled"].includes(status);
export const title = (value: string) =>
  value.replaceAll("_", " ").replace(/^\w/, (letter) => letter.toUpperCase());
export const shortId = (value: string) => value.slice(0, 8);
export const number = (value: number) =>
  new Intl.NumberFormat("en", { maximumFractionDigits: 3 }).format(value);
export function bytes(value: number | null | undefined) {
  if (value == null) return "Unavailable";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  return value >= 1024 ** 3
    ? `${(value / 1024 ** 3).toFixed(1)} GiB`
    : `${(value / 1024 ** 2).toFixed(1)} MiB`;
}
export function duration(start: string, end?: string | null) {
  const seconds = Math.max(
    0,
    Math.floor(
      ((end ? Date.parse(end) : Date.now()) - Date.parse(start)) / 1000,
    ),
  );
  if (!Number.isFinite(seconds)) return "Unavailable";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}
export function date(value: string | null | undefined, timeOnly = false) {
  if (!value) return "Not recorded";
  return new Date(value).toLocaleString(
    "en-GB",
    timeOnly
      ? { hour: "2-digit", minute: "2-digit", second: "2-digit" }
      : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" },
  );
}
export function exportJson(value: unknown, filename: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
