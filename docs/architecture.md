# Architecture and recovery

## Independent projects

The orchestrator owns experiment identity, policy, scheduling, recovery, collection and research coordination. A workload owns its algorithms, native configuration, evaluation and standalone execution. Open-Enricher does not import this package or require an orchestration dependency or new CLI.

The existing harness provides local agent execution. It exports selected response files before teardown. Only the coordinator interprets responses and calls the broker. Agent sandboxes do not mount the ledger, SSH configuration or Docker socket.

| Module | Responsibility |
| --- | --- |
| `agent_harness` | Provider commands, OpenSandbox sessions, response export and reports |
| `coordinator.py` | Research roles, rubric/cohort, review gate, budgets and checkpoints |
| `broker.py` | Submit/get/cancel/artifacts interface, pinned bundles and attempts |
| `store.py` | SQLite ledger, event log and Linux process locks |
| `adapters.py` | Workload validation and native result contracts |
| `backends.py` | Local process, Docker and SSH transports |
| `ssh.py` | Host-side persistent OpenSSH connection policy and per-request deadlines |
| `remote.py` | Remote submission fence, Slurm reconciliation and artifact packaging |
| `worker.py` | Workload execution and structured result production |

The remote worker is a zipapp with standard-library dependencies only. Source and worker archives are hashed in the launch bundle. GPU dependencies are provisioned separately. `runtime_id` records an operator-provided environment identity; it does not verify it. Pin image digests and record dependency/environment locks for reproducible deployments.

## Durable identity and state

An experiment ID names a configuration/hypothesis. A run ID identifies one invocation. An attempt ID identifies one execution with its own source, outputs and scheduler receipt.

The unique request ID is bound to a canonical manifest hash. Profiles and command configuration are snapshotted so later configuration edits cannot redirect an existing run. Retries reuse the original source/worker archives and create new attempt directories. Retry is explicit and bounded by `max_attempts`; a failed evaluation does not silently spend another allocation.

```text
.experiments/
  ledger.sqlite3, broker.lock
  runs/<run-id>/<attempt-id>/
    job.json, worker.pyz, source.tar (when applicable)
    artifacts.tar.gz, artifact-index.json
    artifacts/                       # normalized and native evidence
  campaigns/<campaign-id>/<iteration>-<role>/
    context.json, prompt.md, response.json, accepted.json, reports/
```

SQLite commits intent before transport. Linux locks serialize mutations on one broker host and prevent two processes from advancing the same campaign concurrently. The OS releases locks after a crash. External operations are outside SQLite transactions. This is a single-host control plane, not distributed leader election. Store SQLite on local disk and back it up together with artifact directories.

## Submission ambiguity

SSH stages a complete bundle in a temporary directory, then renames it into place. A remote `flock` protects submission. `submit.started.json` is persisted before `sbatch --parsable`; a receipt is persisted afterwards. The unique Slurm job name includes the attempt ID.

A lost SSH response is recovered from that receipt. A crash between scheduler acceptance and receipt persistence is recovered by matching the attempt name in `squeue` and `sacct`. Exactly one match restores identity. No match or conflicting matches remain `unknown`. Absence from the queue does not prove rejection, and the submission marker forbids a second `sbatch`.

This trades availability for duplicate prevention. A crash just before `sbatch`, explicit rejection, or expired accounting data can leave an attempt unknown indefinitely. There is no automatic override. An operator can independently establish that no original job remains active, then use `experimentctl resolve <run-id> --state failed --reason "<evidence>"` before retrying. This is an audited assertion, not a scheduler check: an incorrect assertion can permit a duplicate job. The coordinator never calls this operation. Keep accounting and receipts longer than the maximum job duration. Automatic Slurm requeue is disabled to avoid reusing an attempt directory for another execution.

Cancellation intent is persisted before transport and retried during reconciliation. `scancel` acceptance does not establish terminal state; scheduler observation does. A terminal job with a download failure remains `collecting`. Partial evidence is preserved for failed/cancelled jobs. The broker validates result identity and outcome fields before accepting success.

Local process execution uses a launch marker, PID birth token and completion files. Recycled PIDs cannot be mistaken for the worker. Host loss without completion is unknown. Docker recovers by a stable container name. Stopped containers and remote attempt directories remain available; cleanup is explicit after collection.

## Local monitoring over a persistent SSH connection

The orchestrator PC owns scheduling control, periodic status checks and artifact collection. There is no custom resident monitor on the login node and no reverse connection into the PC. The existing remote controller still runs briefly when a broker request arrives; Slurm owns the long-running compute job.

`SSHTransport` invokes the system OpenSSH client with `ControlMaster=auto`, a shared `ControlPath`, and `ControlPersist`. OpenSSH keeps the master process on the PC. Each broker operation uses a new SSH channel on the existing authenticated network connection. Reconstructing the backend or starting another CLI process does not discard that connection. Profiles with matching SSH connection settings and effective destinations share it, including CPU and GPU profiles on the same cluster.

The control socket lives in a short directory owned by the current user with mode `0700`, by default `/tmp/experiment-ssh-<uid>`. Paths that are symlinks, owned by another user or accessible to other users are rejected. OpenSSH's `%C` token distinguishes the effective destination, port, user, local host and jump host. This also avoids Unix socket path-length problems with long WSL workspace paths. A profile may select another short private directory with `ssh.control_directory`.

| Profile setting under `ssh` | Default | Meaning |
| --- | --- | --- |
| `persist_seconds` | 3600 | Retain an idle connection for one hour; not a maximum age for an active connection |
| `connect_timeout_seconds` | 10 | Connection/handshake timeout when a connection must be established |
| `keepalive_seconds` | 60 | OpenSSH server-alive interval when no data has been received |
| `keepalive_count_max` | 3 | Unanswered server-alive probes before abandoning the connection |
| `command_timeout_seconds` | 120 | Deadline for one remote control request |
| `transfer_timeout_seconds` | 1800 | Deadline for staging, artifact packaging or downloading |

The polling interval remains a separate CLI option, defaulting to 30 seconds. These checks reuse the connection throughout a day-long job. Keepalive packets are transport health checks, not new logins or Slurm queries. A missing/dead master allows OpenSSH to establish a connection on a subsequent request. If a connection fails during a request, that request may fail; the next broker reconciliation resumes from durable state. There is no transport-layer replay of a potentially accepted submission. A command timeout alone does not force a healthy shared connection closed or cancel a Slurm job.

A failed download remains temporary until a complete archive is fetched. The broker stays in its collection state and a later reconciliation can download again without submitting a new job. Host authentication and normal SSH host-key verification remain in effect. Connection options follow the official [OpenSSH configuration documentation](https://man.openbsd.org/ssh_config#ControlMaster).

If the PC shuts down, jobs and results remain on the cluster. Monitoring and collection resume when the local watcher starts again. The master otherwise expires after its configured idle period; no cluster-side daemon needs stopping. Site-wide SSH session limits or VPN disconnects can still require reconnecting, but healthy connections are not intentionally recreated on each polling tick.

## Separate outcome dimensions

| Dimension | States |
| --- | --- |
| Scheduler | prepared, submitting, queued, running, completed, failed, cancelled, unknown |
| Pipeline | pending, succeeded, failed |
| Evaluation | pending, succeeded, failed, not_requested |
| Artifacts | pending, ready, partial |

A successful run requires a completed job, successful pipeline, successful/unrequested evaluation and collected structured result. Native scoring failure does not erase inference success. Exit codes and domain results remain separately inspectable.

Collection excludes datasets, source code and caches. It creates a size-limited archive and a SHA-256 inventory. Extraction rejects traversal, links and special files. A size-limit failure leaves files on the execution host; manually collect them or increase the policy for a new run. Existing runs retain their policy snapshot.

## Open-Enricher integration

The initial adapter targets the schema/archive contract inspected at commit `9413458dc4b06713504a6a31568753af15a4f8a2`. Verify compatibility when selecting a revision that changes its CLI or output schema.

`git archive` captures only the configured commit. Dirty files, untracked files, submodule contents and hydrated Git LFS objects are not copied; source symlinks are rejected by extraction. Each attempt has a private checkout, dataset root, native `runs/local` and progress log. Only its selected variant remains in the private checkout. The original repository is unchanged.

The payload contains the six current variant fields and an explicit video list. The standalone CLI runs one variant, processing its videos sequentially. That sequential workload behavior complements the orchestrator, which schedules and compares independent invocations.

`OPEN_ENRICHER_VIDEO_INPUTS` references existing files on the execution host. The worker hashes videos before inference and records observed digests. Optional manifest digests must cover the exact cohort and are verified before inference. Other workload adapters are responsible for validating their own input references.

Normalization inspects `video_results.json`, `loop_summary.json`, the exact cohort, and each video's result, profile and timing files. Evaluation success requires successful evaluator rows and score files for every video. CLI exit zero alone cannot establish success because the native loop catches some failures internally.

Audio-cache reuse is disabled; model caches are private too, so fresh attempts may download model weights. Provision disk and network accordingly. A future read-only model store or locking cache adapter can reduce repeated downloads without sharing mutable output/progress files. Optional external synopsis files are copied into the private checkout. Native Cursor evaluation currently shares the GPU allocation; separate CPU scoring requires a supported workload entrypoint or another adapter.

## Agent sessions and context

The state machine runs baseline → propose → review → submit → wait → evaluate → analyse. Waiting invokes no agent. The local coordinator can remain running, or stop and resume later. Remote jobs continue while the PC sleeps/disconnects; observation resumes with the broker.

Every role starts a fresh OpenSandbox session and exports a validated JSON response before destruction. The coordinator stores a task snapshot, prompt, context, report and accepted response. Responses are reused only for matching context digests. Failed role invocations move to `needs_attention`; `campaign resume` retries the interrupted phase after its cause is fixed. An interrupted submit uses the same request ID.

Recovery reconstructs context explicitly; it does not restore provider-hidden reasoning or native session databases. The evaluator receives the fixed rubric and evidence without the proposal/review transcript. The analyst receives the hypothesis and earlier decisions. Evidence is bounded to 512 KiB total and 128 KiB per file; the inventory exposes omitted artifacts. Larger workloads need deterministic summaries or retrieval tools before this can constitute full evaluation.

Agents propose configuration changes only. They cannot alter source revision, credentials, scheduler commands or resource profiles. Review is bound to the manifest digest. The campaign caps runs including the baseline, and the broker caps active runs and attempts. Agent TTL/command timeouts are template settings; provider token/money limits are not enforced.

## Deployment boundaries

OpenSandbox isolates agents; Docker isolates local CPU jobs. Slurm schedules resources but is not a sandbox for hostile code. Use trusted workloads and a dedicated cluster account or site-approved container mechanism before enabling arbitrary generated code remotely.

Workload commands are operator-owned argv arrays. Agent parameters are JSON, not shell fragments. SSH commands are quoted, use batch authentication and preserve host-key verification. Secrets should be runtime environment names, not literal configuration values, because job profiles are persisted. Reports/artifacts may contain sensitive workload data.

Remote requirements: `sbatch`, `squeue`, `sacct`, `scancel`; shared storage visible on login/compute nodes; current Python 3.11+ on both; workload dependencies on compute nodes. Semantics follow the official [sbatch](https://slurm.schedmd.com/sbatch.html) and [sacct](https://slurm.schedmd.com/sacct.html) documentation. Fake transport tests cover the protocol, but every site needs a live CPU smoke test for its environment and accounting policies.

MCP/HTTP transport, native provider session restoration, automatic failure-class retries, arbitrary code patches, shared caches, multi-host coordination and automatic environment provisioning are future extensions. The current boundary is the CLI and Python broker interface.
