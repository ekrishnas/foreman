# Foreman

A governed agentic SDLC orchestrator. Give it a work item (a GitHub issue) and it drives it through a workflow graph. LLM agents do the work inside defined boundaries; Foreman owns control: ordering, gates, approvals, policy, retries, rollback, cost and audit. The full design is in [DESIGN.md](DESIGN.md).

| Workflow | Input | Output |
|---|---|---|
| `feature` | user story | pull request + docs under `openspec/docs/`, specs archived in the same PR |
| `bugfix` | bug report | pull request with a failing test first + RCA document (`openspec/docs/rca/`) |
| `spike` | ambiguous requirement | analysis PR (`openspec/docs/spikes/<run>/analysis.md`); merging it files the proposed issues |

## Quick start (offline, no accounts)

Python is pinned to 3.13 (`.python-version`; `requires-python` is `>=3.12,<3.15`). Some dependencies (`zstandard`, `pyyaml`) have no wheels for Python 3.15 yet, so `uv` would try to compile them and ask for a C compiler. `uv` downloads 3.13 for you; no compiler is needed.

```bash
uv sync
uv run foreman run scenarios/greenfield.yaml     # fake mode: deterministic fake agents, local git, no network
uv run pytest -q                                 # the whole suite, also offline
```

Fake mode builds a throwaway Python "url shortener" repo under `runs/<run-id>/target`, files the scenario's input as an issue in a local `FakeGitHub` (`runs/<run-id>/github/`), runs the real graph, runner, policy and gates against it, and merges the PR itself as the "human". The output ends with the run's status, PR number, cost and whether the scenario's `expect` block held.

**Windows:** fake mode (`foreman run ...`) is written to run natively: the fake profile only calls `python`, and all file and process IO is explicit UTF-8. It has been checked by simulation (wheel resolution for Windows x64 and ARM64, an ASCII default-encoding run, no `os.killpg`), not on a real Windows machine. The test suite assumes a POSIX shell (`sleep`, `touch`, `#!` stub scripts); run `pytest` under WSL.

## Commands

| Command | What it does |
|---|---|
| `foreman run <scenario.yaml> [--mode fake\|live\|replay]` | run a scenario |
| `foreman run --issue <n>` | live: run an existing issue; the workflow comes from its `foreman:<workflow>` label |
| `foreman resume <run-id> [--poll]` | continue a paused or safe-stopped run (needs `FOREMAN_DB_URL`) |
| `foreman metrics <run-id>` | stage success, retry rate, rollbacks, MTTR, latency, human wait, cost |
| `foreman cost --issue <n>` | cumulative cost of an issue across all its runs |
| `foreman issue create <file.md> --type feature\|bugfix\|spike` | file a markdown file as a labelled issue |
| `foreman handoff <analysis.md> --issue <spike>` | file the issues of a spike analysis that is already approved, after you confirm the list |

Exit codes: `0` completed (or paused waiting for a merge), `1` error or unmet `expect`, `2` safe-stopped, `3` observe failed.

## Modes

- **fake** – `FakeWorker` (deterministic, scripted faults), local git, `FakeGitHub`, a structural stand-in for the OpenSpec CLI. Used by Foreman's own tests. It never asks about branches: it works in a throwaway repo.
- **live** – `claude -p` per agent, real `gh`, real `openspec`. Every worker call is recorded to `runs/<id>/recorded/`.
- **replay** – re-runs recorded worker outputs (`--recording runs/<id>/recorded`), so an evaluator needs no Claude install. `--record` also records in fake mode.

## Live setup

1. `claude` installed and logged in (workers run on your Claude plan; reported cost is the API-equivalent, notional figure).
2. `gh auth login` as the human who will review and merge. Only Foreman's GitHub adapter calls `gh`; agents get no token and no network tools.
3. `openspec` installed and pinned: `npm install -g @fission-ai/openspec@1.15.0`.
4. Postgres for durable checkpoints, audit and cost: `docker compose up -d`, then
   `export FOREMAN_DB_URL=postgresql://foreman:foreman@localhost:5432/foreman`.
5. `git config user.name` and `user.email` set in the target repo (or globally): Foreman commits as you.
6. `uv run foreman run scenarios/greenfield.yaml --mode live`. On the first greenfield run Foreman creates `../url-shortener` and the GitHub repo (private, PRs required on `main`). The run pauses at the PR; merge it on GitHub, then `foreman resume <run-id>` (or `--poll`).

### Spikes: from analysis to issues

Everything the agents write for people lives under `openspec/docs/` in the target repo (`spikes/<run>/analysis.md`, `rca/<issue>.md`, and the doc writer's pages), next to `openspec/specs/` and `openspec/changes/`. Agents can write to only one of those areas each, so the doc writer cannot touch an approved design.

A spike ends in a pull request that adds one analysis file. The analyst keeps it short and finishes with **Open questions** (numbered decisions, each with a recommended answer, then a list of things only testing can settle) and **Proposed issues**: 1 to 10 small `### <title>` issues in build order.

1. Open the PR. Its description says which issues merging will file. Answer the decisions by editing `analysis.md` in the PR.
2. **Merging the PR is the approval.** `foreman resume` (or `--poll`) then files one GitHub issue per proposed issue and lists them in build order on the spike issue. Foreman files them through `gh`, never the agents.
3. Each new issue is labelled `foreman:feature` and `foreman:needs-approval`, so no run can start on it. Settle its decisions, add `foreman:ready`, then `foreman run --issue <n>`.

If the analysis reached the base branch without a PR (or you approved it some other way), run `foreman handoff <analysis.md> --issue <spike>`: it shows the list, asks you to confirm, and files the same issues. An older analysis with a single `## Draft user story` section files one issue.

### Branches and commits

Foreman never commits or pushes to your base branch. Before a live run files an issue or spends anything, it checks that **origin has a base branch** (`main`, `master`, `develop` or `trunk`) and then asks you two things:

1. Which branch to start from and open the pull request against (it lists the base branches on origin and proposes origin's default).
2. How to name the run's branch. If origin's branches already follow a convention (say `feature/...`), it proposes that; otherwise `foreman/<run id>`.

Press enter to accept the proposal. The answers are saved in `runs/<id>/run.json`, so `foreman resume` does not ask again. The run branch starts from `origin/<base>`, never from your local copy, and Foreman refuses to run when the local base has commits that are not on origin (they would be silently left out). If origin has no base branch yet it stops with the command to fix it (`git push -u origin main`).

Commits are **authored by you** (your git identity) and carry `Co-Authored-By: Foreman <foreman@localhost>`. Foreman authors a commit itself only when git has no identity at all, as in fake mode on a bare machine; live mode refuses to start without one.

Every commit message follows Conventional Commits (`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `ci`): imperative mood, subject under 50 characters with no trailing period, body wrapped at 72. A small `committer` agent (`prompts/committer.md`) writes it from the staged diff and the code checks the rules (`foreman/commits.py`); if the agent keeps breaking them, a fixed per-stage message is used instead. If the diff contains a secret, nothing is committed: the run stops with a `WARNING` and the uncommitted changes are discarded.

The greenfield scenario tags the merged commit `v1` (`tag: v1`). For the brownfield scenario, add the deliberate "performance refactor" commit on top that replaces the atomic click increment with read-modify-write, and tag it `v1-regressed`.

## How it works

- **One generic runner.** Every agent stage is data (`Stage`) executed by `run_stage`: entry gate, prompt built only from the agent's listed context keys, worker call under the run lock, policy check on the git diff, exit gate, up to 2 retries with the failure fed back, one fallback to the strongest model, cost row, audit rows, commit (message written by the `committer` agent, checked in code). Verdicts are code, never model output.
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
- Pull request titles are `[foreman/<workflow>] <issue title>`, not Conventional Commits. A repo that squash-merges would put that title on its base branch.
- The commit-message imperative-mood check is a heuristic on the first word (it flags `added`, `adds`, `adding`); it can miss odd cases.
- The bugfix workflow also asks for approval of the triage change before reproducing (specs before code), which the workflow diagram in DESIGN.md does not draw.
- A spike's clarify questions come from the `## Questions` section of its issue.
