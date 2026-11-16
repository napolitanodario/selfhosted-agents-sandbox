# Automatic deployment on the sandbox PC

The workflow in `.github/workflows/deploy.yml` runs on pushes to `main`, or
manually from the Actions tab with `main` selected. Tests run on a GitHub-hosted
Linux runner. After they pass, a **self-hosted runner on the sandbox PC** installs
the tested commit into a dedicated Python virtual environment and activates it.

The runner initiates outbound HTTPS connections to GitHub. GitHub does not need
SSH access to the PC, including when the PC is behind a VPN. See GitHub's
[runner connectivity requirements](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#communication).
SSH from this PC to the GPU cluster continues to use the broker's existing
configuration; deployment does not change that connection.

## One-time setup on the PC

Install these prerequisites yourself:

- Linux, Git and a current Python 3.11+ patch release with `venv`/`ensurepip` and
  tarfile extraction filters. Python 3.12 is the version used in the workflow's tests.
- Docker, OpenSandbox server and the required sandbox/container images.
- SSH client, VPN and cluster access if using Slurm.
- A current GitHub Actions runner, registered for this repository with the
  additional label **`experiment-orchestrator`**. Keep its automatic updates enabled.
- Node.js 24 and npm for manual deployments. The GitHub workflow provisions Node
  with `actions/setup-node` for the frontend build.

In the repository, open **Settings → Actions → Runners → New self-hosted runner**,
select Linux and follow the generated download/registration instructions. Install
the runner as a service using GitHub's
[Linux service instructions](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application).
Run it as the ordinary Linux account that will own this deployment. The workflow
does not use `sudo`. That account also needs the permissions to run Docker and
access the workload repositories when you later start campaigns.

Use a deployment directory **outside the runner's `_work` directory**. The runner
checkout is disposable; production files must survive checkout cleanup. Example,
run as the deployment account:

```bash
mkdir -p /home/dario/services/experiment-orchestrator
chmod 700 /home/dario/services/experiment-orchestrator
```

In **Settings → Secrets and variables → Actions → Variables**, configure:

| Variable | Required | Example / default |
| --- | --- | --- |
| `ORCHESTRATOR_DEPLOY_ROOT` | Yes | `/home/dario/services/experiment-orchestrator` |
| `ORCHESTRATOR_PYTHON` | No | `/usr/bin/python3.12`; defaults to `python3` on the runner's service PATH |

The directory must be writable by the runner account. No deployment SSH key,
SSH host secret or personal access token is needed. The workflow uses its
read-only `GITHUB_TOKEN` to check whether the commit is still the latest on `main`.
Restrict who can push to `main`: those commits execute on the deployment PC.
This workflow has no pull-request trigger.

The PC needs access to the Python package index, the npm registry, and Node.js
downloads for the package installation and dashboard build.
System dependencies remain manually provisioned; Python dependencies from
`pyproject.toml` are installed automatically **inside each release's venv**.
Global Python packages are not inherited. Standard pip configuration, such as an
internal package index or `PIP_NO_INDEX` plus `PIP_FIND_LINKS`, can be configured
for the runner account if needed.

## What each deployment does

1. Run the unit/integration test suite and validate bundled task definitions on
   GitHub. These checks need no model credentials, OpenSandbox server or GPU.
2. Check out the same commit on the sandbox PC. Serialize deployments with both
   GitHub concurrency and a local filesystem lock.
3. Confirm that the commit is still the latest `main`. Superseded queued runs are
   skipped; the workflow does not intentionally roll back to an older push.
4. Export tracked files with `git archive` into a new release directory. Untracked
   `.env`, credentials, local databases and runner checkout files are not copied.
5. Build the dashboard with the npm lockfile. Create `.venv` **at its permanent
   path**, install the package with its dashboard dependencies and compiled assets,
   run `pip check`, validate the bundled examples and load the operator config.
   Installation is not editable and does not depend on the runner checkout.
6. Link persistent configuration/state into the release and recheck `main`.
7. Atomically switch `current` to the new release, retaining `previous` and all
   older releases. A failed install or validation leaves `current` unchanged.

```text
/home/dario/services/experiment-orchestrator/
├── current -> releases/<commit>-<unique-id>/
├── previous -> releases/<previous-release>/
├── releases/
│   └── <commit>-<unique-id>/
│       ├── .venv/
│       ├── DEPLOYMENT.json
│       ├── examples/, tasks/, src/, ...
│       ├── .env -> ../../shared/.env
│       ├── orchestrator.yaml -> ../../shared/orchestrator.yaml
│       ├── .experiments -> ../../shared/.experiments
│       ├── runs -> ../../shared/runs
│       └── secrets -> ../../shared/secrets
└── shared/
    ├── .env
    ├── orchestrator.yaml
    ├── .experiments/     # SQLite, checkpoints, artifacts
    ├── runs/             # standalone harness reports
    └── secrets/          # local provider authentication files
```

The first deployment initializes `shared/.env` from `.env.example` and
`shared/orchestrator.yaml` from the default CPU configuration. Later deployments
preserve them, even when repository defaults change. You may create these files
before the first deployment instead. Put real credentials only in the PC's
`shared/.env` or configured local authentication files.

For Slurm, edit `shared/orchestrator.yaml` using `examples/slurm-config.yaml` as
a template. Paths in this configuration are resolved relative to **`shared/`**,
because the loader resolves the config symlink. Prefer absolute paths for workload
repositories; keep `state_dir: .experiments` for the default shared state location.
Existing state from another installation is not migrated automatically.

The dashboard's host samples and campaign-agent telemetry use a separate
`monitoring.sqlite3` inside the same persistent state directory. To run the web
service, configure `shared/dashboard.env` and the example systemd user unit as
described in the [dashboard guide](dashboard.md). Deployment activates code but
does not automatically restart the dashboard. Its restart is independent of
campaign processes and OpenSandbox.

## Starting campaigns after deployment

Deployment installs and selects the runtime. The current project has no single
always-running orchestrator daemon: you explicitly start a particular campaign.
The workflow therefore does not start campaigns, restart existing ones, or restart
OpenSandbox. A push cannot accidentally launch a GPU experiment.

Connect to the PC and enter the physical release directory before starting work:

```bash
cd -P /home/dario/services/experiment-orchestrator/current
.venv/bin/experimentctl campaign create examples/campaigns/cpu-scripted.yaml
.venv/bin/experimentctl campaign run CAMPAIGN_ID
```

The scripted CPU campaign exercises real sandbox containers without an AI model.
OpenSandbox and Docker must already be running. For AI roles, configure provider
authentication and use `examples/campaigns/cpu.yaml` instead. See the
[harness guide](agent-harness.md) for server and authentication setup.

Use your process supervisor, such as a systemd service configured with the physical
release path, to keep `campaign run` alive after disconnecting SSH. Do not launch
long-lived campaigns as GitHub Actions steps: the runner cleans up job processes.
Running campaigns keep their original code and venv across deployments; new
invocations through `current` use the new version. Old releases are intentionally
not deleted automatically, since running processes may still need their files.

For a manual installation using the same deployment logic:

```bash
python3 scripts/deploy.py \
  --source "$PWD" \
  --root /home/dario/services/experiment-orchestrator \
  --python /usr/bin/python3.12
```

This deploys the committed `HEAD`, not uncommitted working-tree changes. The
GitHub workflow also passes the expected tested commit and enables the `main`
freshness checks.

## Recovery and rollback

A failed deployment reports an Actions failure, removes its incomplete release
and leaves the active release untouched. An abrupt machine shutdown or forced
process termination can leave an inactive release directory, but does not change
`current` before activation. Inspect such directories before removing them.

For a code rollback, revert the offending commit on `main` and push: this runs the
same tests and deployment process. Existing campaign processes continue with the
version they started with until deliberately stopped/restarted. Shared state is
not rolled back with code; future database format changes need an explicit
migration and compatibility plan. This deployment adds no database migration.

Keep the deployment directory on a local filesystem and back up `shared/` using a
SQLite-aware backup method for its ledger. Package dependencies currently have
version ranges, not a lockfile; the commit pins application code, but does not
guarantee identical dependency versions across fresh deployments.
