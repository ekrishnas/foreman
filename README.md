# Foreman

A governed agentic SDLC orchestrator. Give it a work item (a GitHub issue) and it drives it through a workflow graph. LLM agents do the work inside defined boundaries; Foreman owns control: ordering, gates, approvals, policy, retries, rollback, cost and audit. The full design is in [DESIGN.md](DESIGN.md).

| Workflow | Input | Output |
|---|---|---|
| `feature` | user story | pull request + docs, specs archived in the same PR |
| `bugfix` | bug report | pull request with a failing test first + RCA document |
| `spike` | ambiguous requirement | analysis document PR + a new feature issue from its draft story |

## Quick start (offline, no accounts)

```bash
uv sync
uv run foreman run scenarios/greenfield.yaml     # fake mode: deterministic fake agents, local git, no network
uv run pytest -q                                 # the whole suite, also offline
```

Fake mode builds a throwaway Python "url shortener" repo under `runs/<run-id>/target`, files the scenario's input as an issue in a local `FakeGitHub` (`runs/<run-id>/github/`), runs the real graph, runner, policy and gates against it, and merges the PR itself as the "human". The output ends with the run's status, PR number, cost and whether the scenario's `expect` block held.

## Commands

| Command | What it does |
|---|---|
| `foreman run <scenario.yaml> [--mode fake\|live\|replay]` | run a scenario |
| `foreman run --issue <n>` | live: run an existing issue; the workflow comes from its `foreman:<workflow>` label |
| `foreman resume <run-id> [--poll]` | continue a paused or safe-stopped run (needs `FOREMAN_DB_URL`) |
| `foreman metrics <run-id>` | stage success, retry rate, rollbacks, MTTR, latency, human wait, cost |
| `foreman cost --issue <n>` | cumulative cost of an issue across all its runs |
| `foreman issue create <file.md> --type feature\|bugfix\|spike` | file a markdown file as a labelled issue |

Exit codes: `0` completed (or paused waiting for a merge), `1` error or unmet `expect`, `2` safe-stopped, `3` observe failed.

## Modes

- **fake** – `FakeWorker` (deterministic, scripted faults), local git, `FakeGitHub`, a structural stand-in for the OpenSpec CLI. Used by Foreman's own tests.
- **live** – `claude -p` per agent, real `gh`, real `openspec`. Every worker call is recorded to `runs/<id>/recorded/`.
- **replay** – re-runs recorded worker outputs (`--recording runs/<id>/recorded`), so an evaluator needs no Claude install. `--record` also records in fake mode.

## Live setup

1. `claude` installed and logged in (workers run on your Claude plan; reported cost is the API-equivalent, notional figure).
2. `gh auth login` as the human who will review and merge. Only Foreman's GitHub adapter calls `gh`; agents get no token and no network tools.
3. `openspec` installed and pinned: `npm install -g @fission-ai/openspec@1.15.0`.
4. Postgres for durable checkpoints, audit and cost: `docker compose up -d`, then
   `export FOREMAN_DB_URL=postgresql://foreman:foreman@localhost:5432/foreman`.
5. `uv run foreman run scenarios/greenfield.yaml --mode live`. On the first greenfield run Foreman creates `../url-shortener` and the GitHub repo (private, PRs required on `main`). The run pauses at the PR; merge it on GitHub, then `foreman resume <run-id>` (or `--poll`).

The greenfield scenario tags the merged commit `v1` (`tag: v1`). For the brownfield scenario, add the deliberate "performance refactor" commit on top that replaces the atomic click increment with read-modify-write, and tag it `v1-regressed`.

## How it works

- **One generic runner.** Every agent stage is data (`Stage`) executed by `run_stage`: entry gate, prompt built only from the agent's listed context keys, worker call under the run lock, policy check on the git diff, exit gate, up to 2 retries with the failure fed back, one fallback to the strongest model, cost row, audit rows, commit. Verdicts are code, never model output.
- **Least privilege per agent.** Model, tools, writable paths and context come from `config/agents.yaml`; permissions are CLI flags per call and nothing is written into the target repo. The Observer runs in an empty directory and never sees source.
- **Workflows are LangGraph graphs** wired from the shared nodes in `foreman/nodes.py`. A failed validation re-enters the fan-out; stages whose input hashes are unchanged are skipped (this is the re-plan mechanism).
- **Governance:** writable-path and secret checks block an attempt; dependency, migration, deleted-test and CI/config changes pause for human approval; a per-run USD budget safe-stops; `SafeStop` persists state and is resumable; after validation fails twice the branch is reset to the last approved commit.
- **Audit:** every event goes to `runs/<run-id>/audit.jsonl` (and `audit_events` in Postgres, with SQL views for the metrics in DESIGN.md section 11); every agent call goes to `cost_events`.

## Layout

```
foreman/        runner, nodes, workflows/, policy, adapters, workers, store, engine, cli
config/         agents.yaml        prompts/   one file per agent
targets/        target profiles    scenarios/ scenarios, faults/ and inputs/
docs/PLAN.md    implementation plan and notes
```

## Tests

`uv run pytest -q` runs everything offline (fake worker, fake GitHub, in-memory checkpointer). Integration tests need a Postgres:

```bash
FOREMAN_TEST_DB_URL=postgresql://foreman:foreman@localhost:5432/foreman uv run pytest -m integration
```

## Known limitations and simplifications

- **Agent calls are serialised.** Parallel graph branches (implement/tests/docs, spike explorers) run as parallel nodes, but `run_stage` holds one lock so each stage's diff is attributable. Real concurrency would need a git worktree per branch.
- **External CLIs are verified against stubs.** The `claude`, `gh` and `openspec` argument shapes are unit-tested against stand-in executables, and the `claude` and `gh` flags and JSON field names were checked against the installed binaries' `--help`. The shape of `claude -p --output-format json` and the `openspec validate/archive` behaviour have not been exercised against live installs: watch the first live run.
- **Live mode has not been run end to end** (it needs your Claude plan, GitHub account and a .NET toolchain). Everything else is covered by the offline suite and, for Postgres, by the integration tests.
- Regex secret scan, not a full scanner. Single machine. "Deployment" is local docker compose. Proposal approval happens in the terminal; release approval is a GitHub merge.
- The bugfix workflow also asks for approval of the triage change before reproducing (specs before code), which the workflow diagram in DESIGN.md does not draw.
- A spike's clarify questions come from the `## Questions` section of its issue.
