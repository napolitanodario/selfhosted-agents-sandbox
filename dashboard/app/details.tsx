import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, Download, FileText, Terminal } from "lucide-react";
import { Badge, Drawer, Empty, Facts } from "./components";
import {
  bytes,
  date,
  duration,
  exportJson,
  number,
  request,
  shortId,
  title,
  type AgentSession,
  type Artifact,
  type Campaign,
  type RecordedEvent,
  type Run,
} from "./data";

export function SessionDetail({
  id,
  close,
}: {
  id: string;
  close: () => void;
}) {
  const query = useQuery({
    queryKey: ["session", id],
    queryFn: () => request<AgentSession>(`/api/sessions/${id}`),
    refetchInterval: 5000,
  });
  const [events, setEvents] = useState<RecordedEvent[]>([]);
  const [logError, setLogError] = useState("");
  const [earlier, setEarlier] = useState(false);
  const [follow, setFollow] = useState(true);
  const log = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let cursor: number | null = null;
    let displayed: RecordedEvent[] = [];
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function read() {
      try {
        const page = await request<{
          events: RecordedEvent[];
          cursor: number;
          earlier_events: boolean;
        }>(
          `/api/sessions/${id}/events${cursor === null ? "" : `?after=${cursor}`}`,
          { signal: controller.signal },
        );
        if (stopped) return;
        if (cursor === null) setEarlier(page.earlier_events);
        cursor = page.cursor;
        // The durable cursor and sequence IDs make reconnects idempotent.
        const known = new Set(displayed.map((event) => event.sequence));
        displayed = [
          ...displayed,
          ...page.events.filter((event) => !known.has(event.sequence)),
        ];
        if (displayed.length > 1000) setEarlier(true);
        displayed = displayed.slice(-1000);
        setEvents(displayed);
        setLogError("");
        timer = setTimeout(read, page.events.length === 200 ? 100 : 3000);
      } catch (error) {
        if (!stopped) {
          setLogError((error as Error).message);
          timer = setTimeout(read, 5000);
        }
      }
    }
    void read();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [id]);
  useEffect(() => {
    if (follow && log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [events, follow]);
  const session = query.data;
  return (
    <Drawer
      title={session ? `${title(session.role)} session` : "Agent session"}
      subtitle={shortId(id)}
      close={close}
    >
      {query.error && <div className="notice error">{query.error.message}</div>}
      {!session ? (
        <p className="muted">Loading recorded session...</p>
      ) : (
        <>
          <div className="detail-status">
            <Badge status={session.display_status} />
            <span>
              {session.provider} / {session.task_id}
            </span>
          </div>
          {session.display_status === "unobserved" && (
            <div className="notice">
              The coordinator heartbeat is missing. The agent's current state is
              unknown.
            </div>
          )}
          <Facts
            values={[
              ["Campaign", shortId(session.campaign_id)],
              ["Project", session.project_id],
              ["Iteration", session.iteration],
              ["Duration", duration(session.started_at, session.finished_at)],
              ["Current step", title(session.phase)],
              [
                "Sandbox",
                session.sandbox_id
                  ? shortId(session.sandbox_id)
                  : "Not recorded",
              ],
              ["Commands", session.command_count],
              ["Command errors", session.command_errors],
              ["Last activity", date(session.last_activity_at)],
              ["Coordinator heartbeat", date(session.heartbeat_at)],
            ]}
          />
          {session.error && <div className="notice error">{session.error}</div>}
          {(session.events_truncated || session.dropped_events > 0) && (
            <div className="notice">
              Telemetry is incomplete. {session.dropped_events} events were
              dropped; output may have reached the recording limit.
            </div>
          )}
          <div className="section-heading">
            <h3>
              <Terminal size={16} />
              Session activity
            </h3>
            <button
              className={`small-button ${follow ? "selected" : ""}`}
              onClick={() => setFollow(!follow)}
              aria-pressed={follow}
            >
              <ArrowDown size={13} />
              Follow output
            </button>
          </div>
          {earlier && (
            <p className="muted small">
              Showing the latest events in this view. Earlier output may remain
              in the original harness report.
            </p>
          )}
          {logError && <p className="notice error">{logError}</p>}
          <div
            className="log-view"
            ref={log}
            role="log"
            aria-label="Recorded session activity"
            aria-live="off"
          >
            {events.length === 0 && (
              <p className="log-empty">Waiting for recorded events...</p>
            )}
            {events.map((event) => (
              <div
                className={`log-row ${event.kind === "log" ? "output" : "event"}`}
                key={event.sequence}
              >
                <time>{date(event.time, true)}</time>
                <pre>{eventText(event)}</pre>
              </div>
            ))}
          </div>
          <details className="disclosure">
            <summary>Recorded prompt</summary>
            <pre className="code-block">
              {session.prompt || "Prompt unavailable for this session."}
            </pre>
          </details>
          <details className="disclosure" open={!!session.response}>
            <summary>Validated response</summary>
            <pre className="code-block">
              {session.response
                ? JSON.stringify(session.response, null, 2)
                : "No validated response has been recorded yet."}
            </pre>
          </details>
          <p className="muted small">
            Token usage and cost are unavailable because the current harness
            does not report them.
          </p>
          <button
            className="secondary-button"
            onClick={() =>
              exportJson(
                { session, displayed_events: events },
                `session-${shortId(id)}.json`,
              )
            }
          >
            <Download size={15} />
            Export displayed session
          </button>
        </>
      )}
    </Drawer>
  );
}

function eventText(event: RecordedEvent) {
  const payload = event.payload;
  if (event.kind === "log") return String(payload.text ?? "");
  if (event.kind === "command.started")
    return `$ [${payload.label}] ${payload.command}`;
  if (event.kind === "command.finished")
    return `${payload.label}: exit ${payload.exit_code ?? "unknown"}, ${number(Number(payload.seconds || 0))}s${payload.error ? `\n${payload.error}` : ""}`;
  return `${title(event.kind.replaceAll(".", " "))}  ${JSON.stringify(payload)}`;
}

export function RunDetail({ id, close }: { id: string; close: () => void }) {
  const query = useQuery({
    queryKey: ["run", id],
    queryFn: () => request<Run>(`/api/runs/${id}`),
    refetchInterval: 5000,
  });
  const [attemptId, setAttemptId] = useState("");
  const [selectedPath, setSelectedPath] = useState("");
  const run = query.data;
  const attempt =
    run?.attempts.find((item) => item.id === attemptId) ?? run?.attempts.at(-1);
  const artifactUrl = `/api/runs/${id}/attempts/${attempt?.id}`;
  const artifacts = useQuery({
    queryKey: ["artifacts", id, attempt?.id],
    queryFn: () => request<Artifact[]>(`${artifactUrl}/artifacts`),
    enabled: !!attempt,
    refetchInterval: 10_000,
  });
  const preview = useQuery({
    queryKey: ["preview", id, attempt?.id, selectedPath],
    queryFn: () =>
      request<{ content: string; truncated: boolean }>(
        `${artifactUrl}/preview?path=${encodeURIComponent(selectedPath)}`,
      ),
    enabled: !!attempt && !!selectedPath,
  });
  const events = useQuery({
    queryKey: ["run-events", id],
    queryFn: () => request<RecordedEvent[]>(`/api/runs/${id}/events`),
    refetchInterval: 10_000,
  });
  return (
    <Drawer
      title={run?.experiment_id || "Workload details"}
      subtitle={`Run ${shortId(id)}`}
      close={close}
    >
      {query.error && <div className="notice error">{query.error.message}</div>}
      {!run ? (
        <p className="muted">Loading recorded workload...</p>
      ) : (
        <>
          <div className="detail-status">
            <Badge status={run.status} />
            <span>
              {run.backend === "slurm-ssh"
                ? "Remote cluster workload"
                : "Local workload"}
            </span>
          </div>
          {run.observation_stale && (
            <div className="notice">
              This workload has not been observed recently. Keep its campaign or
              watcher running on the agent host to refresh cluster status.
            </div>
          )}
          <Facts
            values={[
              ["Project", run.project_id],
              ["Execution profile", run.profile],
              ["Submitted", date(run.created_at)],
              ["Last observation", date(run.updated_at)],
              [
                "Source commit",
                run.source_revision
                  ? shortId(run.source_revision)
                  : "Built-in workload",
              ],
              ["Attempts", run.attempts.length],
            ]}
          />
          {run.hypothesis && <blockquote>{run.hypothesis}</blockquote>}
          <div className="section-heading">
            <h3>Execution attempt</h3>
            <select
              aria-label="Execution attempt"
              value={attempt?.id || ""}
              onChange={(event) => {
                setAttemptId(event.target.value);
                setSelectedPath("");
              }}
            >
              {run.attempts.map((item) => (
                <option value={item.id} key={item.id}>
                  Attempt {item.number}
                </option>
              ))}
            </select>
          </div>
          {attempt && (
            <>
              <div className="state-grid">
                {[
                  ["Scheduler", attempt.scheduler],
                  ["Pipeline", attempt.pipeline],
                  ["Evaluation", attempt.evaluation],
                  ["Artifacts", attempt.artifacts],
                ].map(([name, state]) => (
                  <div key={name}>
                    <span>{name}</span>
                    <Badge status={state || "pending"} />
                  </div>
                ))}
              </div>
              <Facts
                values={[
                  ["Job ID", attempt.job_id || "Not recorded"],
                  [
                    "Execution duration",
                    attempt.started_at && attempt.finished_at
                      ? duration(attempt.started_at, attempt.finished_at)
                      : "Not recorded",
                  ],
                  ["Exit code", attempt.exit_code ?? "Not recorded"],
                ]}
              />
              {attempt.error && (
                <div className="notice error">{attempt.error}</div>
              )}
              {attempt.issues.map((issue, index) => (
                <div className="notice" key={index}>
                  {issue}
                </div>
              ))}
              <h3>Recorded metrics</h3>
              {Object.keys(attempt.metrics).length ? (
                <div className="metric-list">
                  {Object.entries(attempt.metrics).map(([name, value]) => (
                    <div key={name}>
                      <span>{name}</span>
                      <strong>{number(value)}</strong>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted">
                  No numeric metrics have been collected for this attempt.
                </p>
              )}
            </>
          )}
          <details className="disclosure">
            <summary>Experiment parameters</summary>
            <pre className="code-block">
              {JSON.stringify(run.parameters, null, 2)}
            </pre>
          </details>
          <h3>Collected artifacts</h3>
          {artifacts.error && (
            <p className="notice error">{artifacts.error.message}</p>
          )}
          {artifacts.data?.length ? (
            <div className="artifact-list">
              {artifacts.data.map((item) => (
                <button
                  key={item.path}
                  className={selectedPath === item.path ? "selected" : ""}
                  onClick={() => setSelectedPath(item.path)}
                >
                  <FileText size={16} />
                  <span>{item.path}</span>
                  <small>{bytes(item.bytes)}</small>
                </button>
              ))}
            </div>
          ) : (
            <p className="muted">
              Artifacts appear after the broker collects this attempt.
            </p>
          )}
          {preview.error && <p className="notice">{preview.error.message}</p>}
          {selectedPath && preview.data && (
            <div className="artifact-preview">
              <div className="section-heading">
                <h3>{selectedPath}</h3>
                <button
                  className="text-button"
                  onClick={() => setSelectedPath("")}
                >
                  Close preview
                </button>
              </div>
              {preview.data.truncated && (
                <p className="notice">Preview limited to 256 KiB.</p>
              )}
              <pre className="code-block">{preview.data.content}</pre>
            </div>
          )}
          <details className="disclosure">
            <summary>Broker event history</summary>
            {events.error && <p>{events.error.message}</p>}
            <div className="log-view">
              {events.data?.map((event) => (
                <div className="log-row event" key={event.sequence}>
                  <time>{date(event.time, true)}</time>
                  <pre>{eventText(event)}</pre>
                </div>
              ))}
            </div>
          </details>
        </>
      )}
    </Drawer>
  );
}

export function CampaignDetail({
  campaign,
  runs,
  close,
  openRun,
}: {
  campaign: Campaign;
  runs: Run[];
  close: () => void;
  openRun: (id: string) => void;
}) {
  return (
    <Drawer
      title="Campaign details"
      subtitle={shortId(campaign.id)}
      close={close}
    >
      <div className="detail-status">
        <Badge status={campaign.phase} />
        <span>{campaign.project_id}</span>
      </div>
      <h3>Research objective</h3>
      <p className="objective">{campaign.objective}</p>
      <h3>Evaluation rubric</h3>
      <p>{campaign.rubric}</p>
      <Facts
        values={[
          ["Run budget", `${campaign.runs.length} / ${campaign.max_runs}`],
          ["Created", date(campaign.created_at)],
          ["Last update", date(campaign.updated_at)],
          ["Iterations", campaign.iterations.length],
        ]}
      />
      {campaign.error && <div className="notice error">{campaign.error}</div>}
      <h3>Experiment history</h3>
      <div className="campaign-runs">
        {campaign.runs.map((id, index) => {
          const run = runs.find((item) => item.id === id);
          return (
            <button key={id} onClick={() => openRun(id)}>
              <span className="iteration-number">
                {index === 0 ? "B" : String(index)}
              </span>
              <span>
                <strong>{run?.experiment_id || shortId(id)}</strong>
                <small>
                  {id === campaign.baseline_run ? "Baseline" : "Candidate"}
                </small>
              </span>
              {run && <Badge status={run.status} />}
            </button>
          );
        })}
      </div>
      {!campaign.runs.length && (
        <Empty title="No workloads submitted">
          The coordinator has not submitted the baseline yet.
        </Empty>
      )}
      {campaign.iterations.map((iteration, index) => (
        <details key={index} className="disclosure">
          <summary>
            Iteration {index + 1}: proposal, review and analysis
          </summary>
          <pre className="code-block">{JSON.stringify(iteration, null, 2)}</pre>
        </details>
      ))}
    </Drawer>
  );
}
