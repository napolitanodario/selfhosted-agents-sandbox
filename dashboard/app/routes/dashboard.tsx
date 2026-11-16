import { useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  Bot,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Cpu,
  Download,
  FlaskConical,
  GitCompareArrows,
  Layers3,
  LayoutDashboard,
  LockKeyhole,
  LogOut,
  Menu,
  Radio,
  Search,
  Server,
  ShieldCheck,
  TriangleAlert,
  X,
} from "lucide-react";
import { Badge, Empty, HostChart, Panel, Stat } from "../components";
import { CampaignDetail, RunDetail, SessionDetail } from "../details";
import {
  bytes,
  date,
  duration,
  exportJson,
  finalRun,
  number,
  request,
  shortId,
  title,
  useMonitor,
  type AgentSession,
  type Campaign,
  type Run,
  type Snapshot,
} from "../data";

const navigation = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "campaigns", label: "Campaigns", icon: FlaskConical },
  { id: "agents", label: "Agent sessions", icon: Bot },
  { id: "runs", label: "Workloads", icon: Layers3 },
  { id: "compare", label: "Compare runs", icon: GitCompareArrows },
  { id: "host", label: "Agent host", icon: Server },
];
const descriptions: Record<string, string> = {
  overview:
    "A live view of your agents, research campaigns, and cluster workloads.",
  campaigns: "Follow the research loop from baseline to evidence and analysis.",
  agents:
    "Campaign agents running on the Ubuntu host. Each invocation has its own session.",
  runs: "Track execution attempts and the outcomes collected from your workloads.",
  compare: "Inspect experiment parameters and recorded results side by side.",
  host: "Resource measurements from the machine running your agents and this dashboard.",
};

export default function Dashboard() {
  const client = useQueryClient();
  const auth = useQuery({
    queryKey: ["auth"],
    queryFn: () =>
      request<{ authenticated: boolean; local_access: boolean }>("/api/auth"),
    refetchInterval: 60_000,
  });
  const monitor = useMonitor(!!auth.data?.authenticated);
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const [mobileOpen, setMobileOpen] = useState(false);
  const section = location.pathname.split("/")[1] || "overview";
  const project = params.get("project") || "all";
  const search = params.get("q") || "";
  const status = params.get("status") || "all";
  function update(key: string, value: string) {
    const next = new URLSearchParams(params);
    value && value !== "all" ? next.set(key, value) : next.delete(key);
    setParams(next, { replace: true });
  }
  function go(page: string) {
    const next = new URLSearchParams(params);
    ["run", "session", "campaign", "status"].forEach((key) => next.delete(key));
    navigate(
      `${page === "overview" ? "/" : `/${page}`}${next.size ? `?${next}` : ""}`,
    );
    setMobileOpen(false);
  }
  function open(kind: "run" | "session" | "campaign", id: string) {
    const next = new URLSearchParams(params);
    ["run", "session", "campaign"].forEach((key) => next.delete(key));
    next.set(kind, id);
    setParams(next);
  }
  function close() {
    const next = new URLSearchParams(params);
    ["run", "session", "campaign"].forEach((key) => next.delete(key));
    setParams(next);
  }
  if (auth.isPending)
    return (
      <div className="boot">
        <Activity size={24} />
        Connecting to Observatory...
      </div>
    );
  if (auth.error)
    return (
      <div className="boot">
        <TriangleAlert />
        <p>{auth.error.message}</p>
        <button className="primary-button" onClick={() => void auth.refetch()}>
          Try again
        </button>
      </div>
    );
  if (!auth.data?.authenticated)
    return (
      <LoginScreen
        onSuccess={() => {
          client.clear();
          void auth.refetch();
        }}
      />
    );
  const source = monitor.data;
  const inProject = (item: { project_id: string }) =>
    project === "all" || item.project_id === project;
  const matches = (values: unknown[]) =>
    values.join(" ").toLowerCase().includes(search.toLowerCase());
  const runs =
    source?.runs.filter(
      (item) =>
        inProject(item) &&
        matches([item.id, item.experiment_id, item.project_id, item.profile]),
    ) || [];
  const campaigns =
    source?.campaigns.filter(
      (item) =>
        inProject(item) && matches([item.id, item.objective, item.project_id]),
    ) || [];
  const sessions =
    source?.sessions.filter(
      (item) =>
        inProject(item) &&
        matches([
          item.id,
          item.role,
          item.provider,
          item.task_id,
          item.project_id,
        ]),
    ) || [];
  const activeSessions = sessions.filter(
    (item) => item.display_status === "running",
  );
  const activeRuns = runs.filter((item) => !finalRun(item.status));
  const completed = runs.filter(
    (item) => item.status === "succeeded" || item.status === "failed",
  );
  const successful = completed.filter((item) => item.status === "succeeded");
  const attention =
    campaigns.filter((item) => item.phase === "needs_attention").length +
    runs.filter(
      (item) =>
        ["failed", "unknown"].includes(item.status) || item.observation_stale,
    ).length +
    sessions.filter((item) =>
      ["failed", "interrupted", "unobserved"].includes(item.display_status),
    ).length;
  const host = source?.host_samples.at(-1);
  const selectedCampaign = source?.campaigns.find(
    (item) => item.id === params.get("campaign"),
  );
  return (
    <div className="application">
      {mobileOpen && (
        <button
          className="mobile-scrim"
          aria-label="Close navigation"
          onClick={() => setMobileOpen(false)}
        />
      )}
      <aside className={`sidebar ${mobileOpen ? "open" : ""}`}>
        <button className="brand" onClick={() => go("overview")}>
          <span className="brand-symbol">
            <Activity size={21} />
          </span>
          <span>
            Observatory<small>EXPERIMENT WORKSPACE</small>
          </span>
        </button>
        <div className="workspace-select">
          <label htmlFor="project">PROJECT SCOPE</label>
          <select
            id="project"
            value={project}
            onChange={(event) => update("project", event.target.value)}
          >
            <option value="all">All projects</option>
            {source?.projects.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <span>
            {source?.projects.length ?? 0} registered in recorded activity
          </span>
        </div>
        <span className="nav-label">MONITOR</span>
        <nav aria-label="Main navigation">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={section === item.id ? "active" : ""}
              aria-current={section === item.id ? "page" : undefined}
              onClick={() => go(item.id)}
            >
              <item.icon size={18} />
              <span>{item.label}</span>
              {item.id === "agents" && activeSessions.length > 0 && (
                <span className="nav-count">{activeSessions.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="read-only">
            <ShieldCheck size={16} />
            <div>
              Observation only<small>Experiments stay in control</small>
            </div>
          </div>
          <div className="sidebar-host">
            <span
              className={`status-dot ${monitor.connection === "live" ? "connected" : ""}`}
            />
            <span>{host?.hostname || "Agent host"}</span>
            <small>v1</small>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs">
            <button
              className="icon-button mobile-menu"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation"
            >
              <Menu size={20} />
            </button>
            <span>Workspace</span>
            <ChevronRight size={13} />
            <strong>{project === "all" ? "All projects" : project}</strong>
          </div>
          <div className="topbar-right">
            <span className={`connection ${monitor.connection}`}>
              <Radio size={14} />
              {monitor.connection === "live"
                ? "Live connection"
                : title(monitor.connection)}
            </span>
            {!auth.data.local_access && (
              <button
                className="icon-button"
                aria-label="Sign out"
                onClick={async () => {
                  await request("/api/auth/logout", { method: "POST" });
                  client.clear();
                  void auth.refetch();
                }}
              >
                <LogOut size={16} />
              </button>
            )}
          </div>
        </header>
        <main id="main-content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">EXPERIMENT OPERATIONS</div>
              <h1>
                {navigation.find((item) => item.id === section)?.label ||
                  "Page not found"}
              </h1>
              <p>
                {descriptions[section] || "Choose a page from the navigation."}
              </p>
            </div>
            <button
              className="secondary-button export-button"
              disabled={!source}
              onClick={() =>
                exportJson(
                  {
                    exported_at: new Date().toISOString(),
                    project,
                    runs,
                    campaigns,
                    sessions,
                  },
                  "observatory-snapshot.json",
                )
              }
            >
              <Download size={15} />
              Export snapshot
            </button>
          </div>
          <div className="filterbar">
            <label className="search">
              <Search size={16} />
              <input
                aria-label="Search recorded activity"
                value={search}
                onChange={(event) => update("q", event.target.value)}
                placeholder="Search experiments, agents, or IDs..."
              />
              {search && (
                <button
                  className="icon-button"
                  onClick={() => update("q", "")}
                  aria-label="Clear search"
                >
                  <X size={14} />
                </button>
              )}
            </label>
            {["runs", "agents"].includes(section) && (
              <select
                aria-label="Filter by status"
                value={status}
                onChange={(event) => update("status", event.target.value)}
              >
                <option value="all">All states</option>
                {[
                  ...new Set(
                    section === "runs"
                      ? runs.map((item) => item.status)
                      : sessions.map((item) => item.display_status),
                  ),
                ]
                  .sort()
                  .map((item) => (
                    <option key={item} value={item}>
                      {title(item)}
                    </option>
                  ))}
              </select>
            )}
            <span className="filter-meta">
              {project === "all" ? "Across all projects" : project}
              <span className="separator">/</span>Read-only
            </span>
          </div>
          {monitor.error && (
            <div className="notice error">
              {monitor.error.message}.{" "}
              {source
                ? "Previously received data remains visible."
                : "Waiting for recorded data."}
            </div>
          )}
          {monitor.connection === "reconnecting" && (
            <div className="notice">
              Reconnecting to the agent host. Last stream update:{" "}
              {monitor.received?.toLocaleTimeString("en-GB") || "not received"}.
            </div>
          )}
          {source?.notices.map((message) => (
            <div className="notice" key={message}>
              {message}
            </div>
          ))}
          {!source && !monitor.error && (
            <div className="loading-panels">Loading experiment activity...</div>
          )}
          {source && section === "overview" && (
            <>
              <div className="stats-grid">
                <Stat
                  label="Active agents"
                  value={activeSessions.length}
                  note={`${sessions.length} recorded sessions`}
                  icon={<Bot size={17} />}
                  onClick={() => go("agents")}
                />
                <Stat
                  label="Active workloads"
                  value={activeRuns.length}
                  note={`${activeRuns.filter((item) => item.backend === "slurm-ssh").length} on the remote cluster`}
                  icon={<Layers3 size={17} />}
                  onClick={() => go("runs")}
                />
                <Stat
                  label="Success rate"
                  value={
                    completed.length
                      ? `${Math.round((successful.length / completed.length) * 100)}%`
                      : "N/A"
                  }
                  note={`${successful.length} of ${completed.length} completed runs; cancelled excluded`}
                  icon={<CheckCircle2 size={17} />}
                  onClick={() => go("runs")}
                />
                <Stat
                  label="Attention items"
                  value={attention}
                  note="Errors, unknown states, or stale observations"
                  icon={<TriangleAlert size={17} />}
                  onClick={() => go("runs")}
                />
              </div>
              <div className="overview-grid">
                <Panel
                  title="Research campaigns"
                  eyebrow="IN PROGRESS"
                  action={
                    <button
                      className="text-button"
                      onClick={() => go("campaigns")}
                    >
                      View all <ArrowUpRight size={14} />
                    </button>
                  }
                >
                  <CampaignList
                    campaigns={campaigns
                      .filter((item) => item.phase !== "done")
                      .slice(0, 3)}
                    open={(id) => open("campaign", id)}
                    compact
                  />
                </Panel>
                <Panel
                  title="Agent host"
                  eyebrow={host?.hostname || "LOCAL RESOURCES"}
                  action={
                    <button
                      className="icon-button"
                      aria-label="View host resources"
                      onClick={() => go("host")}
                    >
                      <ArrowUpRight size={18} />
                    </button>
                  }
                >
                  <HostChart samples={source.host_samples} />
                </Panel>
              </div>
              <Panel
                title="Agent activity"
                eyebrow="CAMPAIGN SESSIONS"
                action={
                  <button className="text-button" onClick={() => go("agents")}>
                    All sessions <ArrowUpRight size={14} />
                  </button>
                }
              >
                <SessionTable
                  sessions={sessions.slice(0, 6)}
                  open={(id) => open("session", id)}
                />
              </Panel>
              <div className="overview-grid lower">
                <Panel
                  title="Recent workloads"
                  action={
                    <button className="text-button" onClick={() => go("runs")}>
                      View all <ArrowUpRight size={14} />
                    </button>
                  }
                >
                  <RunTable
                    runs={runs.slice(0, 5)}
                    open={(id) => open("run", id)}
                    compact
                  />
                </Panel>
                <ActivityPanel
                  runs={runs}
                  sessions={sessions}
                  campaigns={campaigns}
                  open={open}
                />
              </div>
            </>
          )}
          {source && section === "campaigns" && (
            <Panel
              title="Campaigns"
              action={
                <span className="count-label">
                  {campaigns.length} in this view
                </span>
              }
            >
              <CampaignList
                campaigns={campaigns}
                open={(id) => open("campaign", id)}
              />
            </Panel>
          )}
          {source && section === "agents" && (
            <Panel
              title="Recorded agent sessions"
              action={
                <span className="count-label">
                  {activeSessions.length} active
                </span>
              }
            >
              <SessionTable
                sessions={sessions.filter(
                  (item) => status === "all" || item.display_status === status,
                )}
                open={(id) => open("session", id)}
              />
            </Panel>
          )}
          {source && section === "runs" && (
            <Panel
              title="Workload history"
              action={
                <span className="count-label">{runs.length} recorded runs</span>
              }
            >
              <RunTable
                runs={runs.filter(
                  (item) => status === "all" || item.status === status,
                )}
                open={(id) => open("run", id)}
              />
            </Panel>
          )}
          {source && section === "compare" && <Comparison runs={runs} />}
          {source && section === "host" && (
            <>
              <div className="stats-grid">
                <Stat
                  label="Host CPU"
                  value={host ? `${host.cpu_percent.toFixed(1)}%` : "N/A"}
                  note={`${host?.cpu_count ?? 0} logical CPUs`}
                  icon={<Cpu size={17} />}
                />
                <Stat
                  label="Memory used"
                  value={bytes(host?.ram_used_bytes)}
                  note={`of ${bytes(host?.ram_total_bytes)}`}
                  icon={<Server size={17} />}
                />
                <Stat
                  label="Disk used"
                  value={bytes(host?.disk_used_bytes)}
                  note={`of ${bytes(host?.disk_total_bytes)} on the state filesystem`}
                  icon={<Layers3 size={17} />}
                />
                <Stat
                  label="Dashboard memory"
                  value={bytes(host?.dashboard_rss_bytes)}
                  note="Resident memory of this monitoring service"
                  icon={<Activity size={17} />}
                />
              </div>
              <Panel
                title="CPU and memory history"
                eyebrow={host?.hostname || "AGENT HOST"}
                action={
                  <span className="count-label">
                    Latest {source.host_samples.length} samples
                  </span>
                }
              >
                <HostChart samples={source.host_samples} />
              </Panel>
              <div className="architecture-note">
                <Server size={24} />
                <div>
                  <h3>Two execution environments</h3>
                  <p>
                    Campaign agents run on this Ubuntu host. Experiment
                    workloads run on the GPU cluster. The dashboard reads
                    cluster observations and collected artifacts from the
                    orchestrator; no agent or monitoring service is installed on
                    the cluster.
                  </p>
                  <small>
                    Last local sample: {date(host?.time)}. A stopped collector
                    leaves gaps in the chart.
                  </small>
                </div>
              </div>
            </>
          )}
          <footer className="page-footer">
            <span>
              <ShieldCheck size={13} />
              Monitoring only. No execution controls.
            </span>
            <span>Times shown in your device's local time</span>
          </footer>
        </main>
      </div>
      {params.get("session") && (
        <SessionDetail
          key={params.get("session")}
          id={params.get("session")!}
          close={close}
        />
      )}
      {params.get("run") && (
        <RunDetail
          key={params.get("run")}
          id={params.get("run")!}
          close={close}
        />
      )}
      {selectedCampaign && (
        <CampaignDetail
          campaign={selectedCampaign}
          runs={source?.runs || []}
          close={close}
          openRun={(id) => open("run", id)}
        />
      )}
    </div>
  );
}

function LoginScreen({ onSuccess }: { onSuccess: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await request("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      setToken("");
      onSuccess();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-screen">
      <div className="login-card">
        <span className="login-mark">
          <Activity size={27} />
        </span>
        <span className="eyebrow">EXPERIMENT OBSERVATORY</span>
        <h1>
          Your research,
          <br />
          in view.
        </h1>
        <p>Follow your agents and experiments from one private workspace.</p>
        <form onSubmit={submit}>
          <label htmlFor="access-token">Workspace access token</label>
          <div className="login-input">
            <LockKeyhole size={16} />
            <input
              id="access-token"
              type="password"
              autoComplete="current-password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              required
            />
          </div>
          {error && (
            <div className="notice error" role="alert">
              {error}
            </div>
          )}
          <button className="primary-button" disabled={busy}>
            {busy ? "Signing in..." : "Open workspace"}
            <ArrowRight size={16} />
          </button>
        </form>
        <div className="login-footer">
          <ShieldCheck size={14} />
          Private access. Read-only monitoring.
        </div>
      </div>
    </div>
  );
}

function CampaignList({
  campaigns,
  open,
  compact = false,
}: {
  campaigns: Campaign[];
  open: (id: string) => void;
  compact?: boolean;
}) {
  if (!campaigns.length)
    return (
      <Empty title={compact ? "No active campaigns" : "No matching campaigns"}>
        Campaigns started by the orchestrator will appear here, with their
        objective and current phase.
      </Empty>
    );
  const steps = [
    "baseline",
    "propose",
    "review",
    "submit",
    "wait",
    "evaluate",
    "analyse",
  ];
  return (
    <div className="campaign-list">
      {campaigns.map((campaign) => {
        const step =
          campaign.phase === "wait_baseline"
            ? 0
            : campaign.phase === "done"
              ? steps.length
              : steps.indexOf(campaign.phase);
        return (
          <button
            className="campaign-card"
            key={campaign.id}
            onClick={() => open(campaign.id)}
          >
            <div className="campaign-card-top">
              <span className="project-tag">{campaign.project_id}</span>
              <Badge status={campaign.phase} />
            </div>
            <h3>{campaign.objective}</h3>
            <div
              className="campaign-steps"
              aria-label={`Campaign phase: ${title(campaign.phase)}`}
            >
              {steps.map((name, index) => (
                <span
                  key={name}
                  className={
                    index < step ? "complete" : index === step ? "current" : ""
                  }
                  title={title(name)}
                />
              ))}
            </div>
            <div className="campaign-card-bottom">
              <span className="mono">{shortId(campaign.id)}</span>
              <span>
                {campaign.runs.length} / {campaign.max_runs} runs
              </span>
              <span className="campaign-step-label">
                {title(campaign.phase)}
                <ChevronRight size={13} />
              </span>
            </div>
          </button>
        );
      })}
    </div>
  );
}

function SessionTable({
  sessions,
  open,
}: {
  sessions: AgentSession[];
  open: (id: string) => void;
}) {
  if (!sessions.length)
    return (
      <Empty title="No matching agent sessions">
        Only agents invoked by an experiment campaign are recorded. New sessions
        appear automatically.
      </Empty>
    );
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Agent / role</th>
            <th>Project</th>
            <th>State</th>
            <th>Current step</th>
            <th>Duration</th>
            <th>Commands</th>
            <th>Last activity</th>
          </tr>
        </thead>
        <tbody>
          {sessions.map((session) => (
            <tr key={session.id}>
              <td>
                <button
                  className="cell-link agent-cell"
                  onClick={() => open(session.id)}
                >
                  <span
                    className={`agent-avatar ${session.display_status === "running" ? "active" : ""}`}
                  >
                    <Bot size={17} />
                  </span>
                  <span>
                    <strong>{title(session.role)}</strong>
                    <small>
                      {session.provider} · {shortId(session.id)}
                    </small>
                  </span>
                </button>
              </td>
              <td>{session.project_id}</td>
              <td>
                <Badge status={session.display_status} />
              </td>
              <td>{title(session.phase)}</td>
              <td className="mono">
                {duration(session.started_at, session.finished_at)}
              </td>
              <td className="mono">
                {session.command_count}
                {session.command_errors > 0 && (
                  <span className="error-count">
                    {" "}
                    / {session.command_errors} failed
                  </span>
                )}
              </td>
              <td className="muted">{date(session.last_activity_at, true)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunTable({
  runs,
  open,
  compact = false,
}: {
  runs: Run[];
  open: (id: string) => void;
  compact?: boolean;
}) {
  if (!runs.length)
    return (
      <Empty title="No matching workloads">
        Submitted experiments and their execution attempts will appear here.
      </Empty>
    );
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Experiment</th>
            <th>State</th>
            {!compact && (
              <>
                <th>Project</th>
                <th>Execution</th>
                <th>Attempts</th>
              </>
            )}
            <th>Last observed</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>
                <button className="cell-link" onClick={() => open(run.id)}>
                  <strong>{run.experiment_id}</strong>
                  <small className="mono">{shortId(run.id)}</small>
                </button>
              </td>
              <td>
                <Badge status={run.status} />
                {run.observation_stale && (
                  <span className="stale-label">Observation stale</span>
                )}
              </td>
              {!compact && (
                <>
                  <td>{run.project_id}</td>
                  <td>
                    <span className="execution-kind">
                      {run.backend === "slurm-ssh" ? (
                        <Server size={14} />
                      ) : (
                        <Cpu size={14} />
                      )}
                      {run.backend === "slurm-ssh" ? "GPU cluster" : "Local"}
                    </span>
                    <small className="muted">{run.profile}</small>
                  </td>
                  <td className="mono">{run.attempts.length}</td>
                </>
              )}
              <td className="muted">{date(run.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ActivityPanel({
  runs,
  sessions,
  campaigns,
  open,
}: {
  runs: Run[];
  sessions: AgentSession[];
  campaigns: Campaign[];
  open: (kind: "run" | "session" | "campaign", id: string) => void;
}) {
  const items = [
    ...sessions.map((item) => ({
      id: item.id,
      kind: "session" as const,
      name: `${title(item.role)} session`,
      detail: `${item.project_id} · ${title(item.display_status)}`,
      time: item.finished_at || item.last_activity_at,
    })),
    ...runs.map((item) => ({
      id: item.id,
      kind: "run" as const,
      name: item.experiment_id,
      detail: `${item.project_id} · ${title(item.status)}`,
      time: item.updated_at,
    })),
    ...campaigns.map((item) => ({
      id: item.id,
      kind: "campaign" as const,
      name: `Campaign ${shortId(item.id)}`,
      detail: `${item.project_id} · ${title(item.phase)}`,
      time: item.updated_at || item.created_at,
    })),
  ]
    .sort((a, b) => Date.parse(b.time) - Date.parse(a.time))
    .slice(0, 5);
  return (
    <Panel title="Latest activity" eyebrow="RECORDED UPDATES">
      {items.length ? (
        <div className="activity-list">
          {items.map((item) => (
            <button
              key={`${item.kind}-${item.id}`}
              onClick={() => open(item.kind, item.id)}
            >
              <span className="activity-node" />
              <span>
                <strong>{item.name}</strong>
                <small>{item.detail}</small>
              </span>
              <time>{date(item.time, true)}</time>
            </button>
          ))}
        </div>
      ) : (
        <Empty title="A quiet workspace">
          Recorded agent and workload updates will appear here.
        </Empty>
      )}
    </Panel>
  );
}

function Comparison({ runs }: { runs: Run[] }) {
  const firstProject = runs[0]?.project_id;
  const [selected, setSelected] = useState<string[]>(() =>
    runs
      .filter((run) => run.project_id === firstProject && finalRun(run.status))
      .slice(0, 2)
      .map((run) => run.id),
  );
  const chosen = runs.filter((run) => selected.includes(run.id));
  const keys = [
    ...new Set(
      chosen.flatMap((run) => Object.keys(run.attempts.at(-1)?.metrics || {})),
    ),
  ].sort();
  function metric(run: Run, key: string) {
    const attempt = run.attempts.at(-1);
    if (
      attempt?.pipeline !== "succeeded" ||
      !["succeeded", "not_requested"].includes(attempt.evaluation)
    )
      return undefined;
    return attempt.metrics[key];
  }
  return (
    <>
      <Panel
        title="Select up to three runs"
        action={<span className="count-label">{chosen.length} selected</span>}
      >
        <div className="compare-picker">
          {runs.map((run) => (
            <label key={run.id}>
              <input
                type="checkbox"
                checked={selected.includes(run.id)}
                disabled={
                  !selected.includes(run.id) &&
                  (chosen.length >= 3 ||
                    (!!chosen.length &&
                      chosen[0].project_id !== run.project_id))
                }
                onChange={(event) =>
                  setSelected((previous) =>
                    event.target.checked
                      ? [...previous, run.id]
                      : previous.filter((id) => id !== run.id),
                  )
                }
              />
              <span>
                <strong>{run.experiment_id}</strong>
                <small>
                  {run.project_id} · {shortId(run.id)}
                </small>
              </span>
              <Badge status={run.status} />
            </label>
          ))}
          {!runs.length && (
            <Empty title="No runs to compare">
              Complete experiments to compare their parameters and results.
            </Empty>
          )}
        </div>
      </Panel>
      {chosen.length >= 2 ? (
        <Panel title="Recorded results">
          <div className="comparison-note">
            <CircleHelp size={17} />
            <span>
              Check that input cohorts and evaluation rubrics match. Metric
              direction is project-specific; differences do not automatically
              mean improvements. Failed outcomes are excluded.
            </span>
          </div>
          <div className="table-scroll">
            <table className="comparison-table">
              <thead>
                <tr>
                  <th>Metric</th>
                  {chosen.map((run) => (
                    <th key={run.id}>
                      {run.experiment_id}
                      <small className="mono">{shortId(run.id)}</small>
                    </th>
                  ))}
                  {chosen.length === 2 && <th>Change (right minus left)</th>}
                </tr>
              </thead>
              <tbody>
                {keys.map((key) => (
                  <tr key={key}>
                    <td>{key}</td>
                    {chosen.map((run) => (
                      <td key={run.id} className="mono">
                        {metric(run, key) === undefined
                          ? "Unavailable"
                          : number(metric(run, key)!)}
                      </td>
                    ))}
                    {chosen.length === 2 && (
                      <td className="mono">
                        {metric(chosen[0], key) !== undefined &&
                        metric(chosen[1], key) !== undefined
                          ? number(
                              metric(chosen[1], key)! - metric(chosen[0], key)!,
                            )
                          : "Unavailable"}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!keys.length && (
            <p className="panel-padding muted">
              No numeric metrics have been collected for the selected runs.
            </p>
          )}
          <div className="compare-configs">
            {chosen.map((run) => (
              <div key={run.id}>
                <h3>{run.experiment_id}</h3>
                <span className="muted small">
                  Commit:{" "}
                  {run.source_revision
                    ? shortId(run.source_revision)
                    : "built-in"}
                </span>
                <pre className="code-block">
                  {JSON.stringify(run.parameters, null, 2)}
                </pre>
              </div>
            ))}
          </div>
        </Panel>
      ) : (
        <Empty title="Choose at least two runs">
          Select runs from the same project to inspect their recorded results.
        </Empty>
      )}
    </>
  );
}
