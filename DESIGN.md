# Foreman: Design

## 1. Purpose

Foreman is a governed agentic SDLC orchestrator. It takes a work item and drives it through a workflow graph. LLM agents do the work inside defined autonomy boundaries. Foreman owns control: ordering, gates, approvals, policy, retries, rollback, cost and audit.

Foreman is target-agnostic and self-contained. Everything that belongs to the orchestrator (code, agent definitions, prompts, permissions, target profiles, scenarios) lives in the Foreman repo. The target repo receives only work product: code, tests, docs, OpenSpec artifacts.

## 2. Workflows at a glance

| Workflow | Input | Output |
|---|---|---|
| feature | user story (file or GitHub issue) | pull request + documentation |
| bugfix | user story / bug report (file or GitHub issue) | pull request + RCA document |
| spike | ambiguous requirement | deep analysis document (+ draft user story for handoff) |

Every PR summary includes the run's cost breakdown and the cumulative cost for its issue.

## 3. Principles

- The graph controls; agents execute.
- LLMs propose; deterministic checks decide. Validate and observe verdicts are code, never model output.
- Least privilege per agent: model, tools, writable paths and context are scoped per agent.
- Verify, don't trust: every agent's changes are checked against policy using the git diff.
- Every decision is traceable: inputs, outputs, gate results, approvals, retries and cost are recorded.
- Specs before code: every change is an OpenSpec change that is validated and approved before implementation.

## 4. Components

| Component | Responsibility |
|---|---|
| Workflow library | Three graphs (feature, bugfix, spike) built from a shared node library |
| Runner | Wraps every node: entry gate, context assembly, worker call, exit gate, retry/fallback, cost capture, audit |
| Workers | `FakeWorker` (tests), `ClaudeCodeWorker` (`claude -p` subprocess), `ReplayWorker` (recorded outputs) |
| Policy engine | Diff-based checks after each stage; cost budget check |
| Spec adapter | Wraps the OpenSpec CLI (validate, archive). One module, so CLI version differences are isolated |
| GitHub adapter | The only code that talks to GitHub (`gh`): reads issues, creates issues, posts status comments, opens PRs, reads review and merge state. Agents never get GitHub access |
| State store | Postgres via LangGraph `PostgresSaver` (durable checkpoints) plus `audit_events` and `cost_events` tables |
| Config | `config/agents.yaml`, `prompts/`, `targets/<name>.yaml`, `scenarios/` |
| CLI | `foreman run <scenario>`, `foreman resume <thread>`, `foreman metrics <run>`, `foreman cost --issue <id>` |

## 5. Workflows

`safe_stop` is reachable from every node. Every run works on a branch `foreman/<run_id>` in the target repo; each stage commits.

### 5.1 feature

```
intake(issue) -> requirements -> [clarify if blocking questions] -> proposal
  -> APPROVE_PROPOSAL -> implement || tests || docs -> validate
  -> archive_spec -> pr_summary -> open_pr -> AWAIT_MERGE -> observe
```

- **proposal** writes an OpenSpec change: `proposal.md`, `design.md` (including the API contract), `tasks.md`, spec deltas. Exit gate: `openspec validate <change> --strict`.
- The **contract** in `design.md` is the synchronization point that lets implement, tests and docs run in parallel. Tests are written from the contract and acceptance criteria, not from the implementation.
- Brownfield features: requirements must list impacted files, modules, APIs and data flows.
- **validate** (deterministic): build, full test suite, all `tasks.md` items checked, OpenSpec validate, policy.
- **archive_spec**: `openspec archive <change> --yes` on the branch, so living specs update inside the same PR.
- **open_pr**: push the branch, open a PR with `Closes #<issue>` and the full summary.
- **AWAIT_MERGE**: the release gate is a human reviewing and merging the PR on GitHub. Foreman pauses and resumes when the PR is merged (`foreman resume`, or a poll of the PR state). Closed without merge: safe-stop with the reviewer's comments recorded.
- **Outputs:** the PR, `docs/` updates, updated `openspec/specs/`.

### 5.2 bugfix

```
intake(issue) -> triage -> reproduce -> fix -> validate -> rca
  -> archive_spec -> pr_summary -> open_pr -> AWAIT_MERGE -> observe
```

- **triage** writes an OpenSpec change with a MODIFIED requirement and a scenario describing correct behaviour, plus impacted modules and a root-cause hypothesis.
- **reproduce** exit gate: the new test **fails** on the current code. No failing test, no fix.
- **validate** exit gate: that test passes, full suite passes, test count has not dropped.
- **rca** writes `docs/rca/<issue>.md`: summary, impact, timeline, root cause, why it was missed, fix, prevention.
- **Outputs:** the PR and the RCA document.

### 5.3 spike

```
intake(issue) -> clarify -> frame -> explore(option 1 || option 2 || option 3) -> analysis
  -> open_pr (analysis doc only) -> AWAIT_MERGE -> handoff
```

- **clarify**: questions for the human; unanswered ones become stated assumptions.
- **frame** lists up to three candidate approaches. **explore** runs one agent per option in parallel (dynamic fan-out), each read-only on the repo, writing only to `spikes/<run_id>/<option>/`.
- **analysis** writes `docs/spikes/<run_id>/analysis.md`: context, assumptions, options, trade-off matrix (complexity, cost, performance, risk, operability), recommendation, open questions, and a draft user story for the recommended option.
- Only the analysis document is merged, through a PR the human approves. **handoff** creates a new GitHub feature issue from the draft user story, linked to the spike issue.

### 5.4 Cross-workflow handoffs

Handoffs are real GitHub issues, so every piece of work is traceable to an issue and costed against it.

- observe failure -> revert PR + a new bugfix issue with the observer's summary (a human labels it `foreman:ready` before Foreman picks it up)
- spike merged -> a new feature issue with the draft user story

## 6. Agents

Each agent is a separate `claude -p` invocation with its own model, tools, writable paths, context and prompt, defined in `config/agents.yaml` and `prompts/<agent>.md`.

| Agent | Workflow | Model tier | Tools | Writable | Context given |
|---|---|---|---|---|---|
| Requirements analyst | feature | strong | Read | none | user story, target profile summary, repo tree (brownfield) |
| Architect | feature | strong | Read, Write | `openspec/changes/<id>/` | normalized requirement, assumptions, clarifications, impacted files, existing specs |
| Implementer | feature | mid | Read, Edit, Write, build cmd | `paths.src` | approved change (design, tasks), reviewer notes, last failure |
| Test writer | feature | mid | Read, Edit, Write, test cmd | `paths.tests` | contract, acceptance criteria, spec scenarios (not the implementation) |
| Doc writer | feature | small | Read, Write | `paths.docs` | approved design, contract |
| Triager | bugfix | strong | Read, Grep, Write | `openspec/changes/<id>/` | bug report, log excerpt, repo tree, existing specs |
| Reproducer | bugfix | mid | Read, Edit, Write, test cmd | `paths.tests` | triage output |
| Fixer | bugfix | mid | Read, Edit, Write, build/test cmd | `paths.src` | triage output, failing test, failure output |
| RCA writer | bugfix | strong | Read, Write | `docs/rca/` | triage, failing test, fix diff, validate results |
| Framer | spike | strong | Read | none | requirement, clarifications/assumptions, repo tree, existing specs |
| Explorer (one per option) | spike | mid | Read, Grep, Write | `spikes/<run_id>/<option>/` | one option, constraints, repo tree |
| Analyst | spike | strong | Read, Write | `docs/spikes/<run_id>/` | framing, all explorer notes |
| PR summarizer | feature, bugfix | small | Read | none | diff stat, OpenSpec change, validate results, approvals, cost table |
| Observer | feature, bugfix | small | Read | none | log sample, deterministic metrics, SLO thresholds (no source code) |

Not agents (deterministic code): intake, clarify (human), validate, release, policy, cost, PR assembly.

Rules for all agents:
- The runner builds each prompt **only** from the agent's listed context keys, under labelled headings; on retry it adds the previous gate failure.
- Permissions are passed as CLI flags per invocation (`--model`, `--allowedTools`, `--disallowedTools`). Nothing is written into the target repo to configure agents.
- No web or network tools. Workers get a minimal environment: no API keys, no secrets.
- Each prompt states role, inputs, required output format, and boundaries.

## 7. Governance

- **Gates:** entry and exit on every node.
- **Human approval:** OpenSpec proposal (terminal), release (PR merge on GitHub), spike findings (PR merge on GitHub); plus automatic pause on any high-impact diff (dependency manifests, migrations/schema, deleted tests, CI or config).
- **Policy (on the diff):** writable paths only; secret pattern scan; dependency changes need approval; test count cannot drop; protected paths.
- **Cost budget:** per-run cap in the scenario; exceeding it triggers safe-stop.
- **Retries:** max 2 per stage, previous failure fed back as context.
- **Fallback:** escalate to the stronger model tier once.
- **Rollback:** before merge, reset the run branch to the last approved commit; after merge, Foreman opens a revert PR.
- **Safe-stop:** persist state, record the reason, exit non-zero, resumable.
- **Re-plan:** each stage output stores its hash and its input hashes; a rejection, clarify answer or changed upstream output invalidates downstream stages whose inputs no longer match.

## 8. Cost tracking

- `claude -p --output-format json` returns cost, token usage and duration. Every agent call records a `cost_events` row: `run_id, issue_id, workflow, stage, agent, attempt, model, input_tokens, output_tokens, cost_usd, duration_ms`.
- On a Claude subscription the figure is the API-equivalent (notional) cost; it is labelled as such.
- Rollups: per stage, per run (session), per issue (all runs for that issue, including retries and re-runs). Retry cost is shown separately so wasted spend is visible.
- The PR summary includes a cost table: agent, model, attempts, tokens, cost, duration; run total; issue total.

## 9. PR summary

Assembled by code, with a short narrative from the PR summarizer agent:

- Linked issue and OpenSpec change id
- What changed and why (narrative)
- Files changed (diff stat)
- Gates passed, retries, fallbacks, policy findings
- Approvals: who, when, notes
- Test results
- Cost table (run and issue totals)
- Risks and rollback instructions
- Bugfix only: link to the RCA document

## 10. GitHub integration

- The target is a real GitHub repo (`url-shortener`). Foreman creates it on the first greenfield run if it does not exist, with branch protection on `main` (PR required).
- **Issues are the unit of work.** Scenario inputs in `scenarios/inputs/` are filed as issues with `foreman issue create <file> --type feature|bugfix|spike`, labelled `foreman:<workflow>`. `foreman run --issue <n>` starts the matching workflow.
- **Status on the issue:** Foreman comments at each gate (proposal ready for approval, validation results, PR opened, observe verdict), so the issue thread is a human-readable audit trail alongside the database.
- **PRs:** one per run, `Closes #<issue>`, body from the PR summary section, labelled with the workflow.
- **Release gate:** a human merge on GitHub. Branch protection makes it impossible for Foreman to merge its own work.
- **Credentials:** `gh` is authenticated as the human; only the GitHub adapter calls it. Workers get no token and no network.
- **Offline mode:** fake and replay runs use a local `FakeGitHub` that writes issues and PRs as files under `runs/<run_id>/github/`, so tests never touch GitHub.

## 11. Observability of Foreman

`audit_events`: `run_id, workflow, stage, event, attempt, model, duration_ms, details (jsonb), ts`, also exported to `runs/<run_id>/audit.jsonl`.

Metrics (SQL views): success rate per stage, retry rate, rollback count, MTTR (failure to next passing gate), end-to-end latency, human wait time, cost per run and per issue.

## 12. Observe stage (post-deployment)

- "Deployment" means starting the released build with the target profile's `run` command (docker compose).
- A deterministic script drives smoke traffic for a fixed window, then collects error count, p95 latency and logs.
- Verdict is code: compare against `slo` in the target profile.
- The Observer agent (read-only, no source code) summarizes anomalies and probable causes for the human.
- Fail: rollback, then a bugfix work item awaiting approval.

## 13. Target profile (`targets/url-shortener.yaml`, in the Foreman repo)

```yaml
name: url-shortener
repo: ../url-shortener
spec_format: openspec
paths: { src: "src/**", tests: "tests/**", docs: "docs/**" }
commands:
  build: dotnet build
  test: dotnet test
  run: docker compose up -d
  stop: docker compose down
  smoke: ./scripts/smoke.sh
health_url: http://localhost:8080/health
slo: { error_rate_max: 0.01, p95_ms_max: 200 }
dependency_manifests: ["**/*.csproj"]
protected: ["**/Migrations/**"]
```

## 14. Run modes

- `fake`: deterministic FakeWorker, used by Foreman's own tests
- `live`: `claude -p` per agent, on the user's Claude plan
- `replay`: re-runs recorded worker outputs, so an evaluator needs no Claude install

## 15. Infrastructure

- `docker-compose.yml` (Foreman repo): Postgres for checkpoints, audit and cost.
- The target app runs its own Postgres and uses Testcontainers in its integration tests.
- Foreman unit tests use `MemorySaver` and `FakeWorker` (fast, no Docker). One integration test exercises `PostgresSaver`.
- External CLIs: `claude` (live mode), `openspec` (pinned version), `gh` (authenticated, live mode), `git`, `docker`.

## 16. Scenarios

A scenario file declares: `name, workflow, input (issue file to file, or an existing issue number), target, start_ref, approvals (interactive | scripted), budget_usd, faults, expect`.

| Scenario | Workflow | Input | Shows |
|---|---|---|---|
| Greenfield | feature | issue filed from `scenarios/inputs/feature-url-shortener.md`, empty repo | full lifecycle, OpenSpec proposal to archive, PR with cost; tagged `v1` |
| Brownfield | bugfix | issue filed from `scenarios/inputs/bug-click-counts.md`, against `v1-regressed` | impact analysis, failing test first, fix, RCA, PR |
| Ambiguous | spike | issue filed from `scenarios/inputs/spike-analytics.md` | clarification, parallel option exploration, analysis document, handoff story |

`v1-regressed`: after the greenfield run, a deliberate "performance refactor" commit replaces the atomic click increment with read-modify-write, giving the bugfix scenario a realistic regression.

Fault-injection runs (fake mode) prove: retry then success, fallback, policy block, budget safe-stop, rollback.

`expect` assertions make each scenario self-checking.

## 17. Repo layout

Foreman repo (everything orchestrator-related):

```
foreman/           graphs, nodes, runner, policy, spec adapter, intake, pr, cost, audit, workers, cli
config/agents.yaml
prompts/           one file per agent
targets/           target profiles
scenarios/         scenario files + inputs/
runs/              exported audit logs, recorded outputs
tests/
docker-compose.yml
DESIGN.md
README.md
```

Target repo `url-shortener/` (work product only): `src/`, `tests/`, `docs/`, `openspec/`, Dockerfile, docker-compose.yml.

## 18. Limitations

- Single machine; no distributed execution or queue.
- Proposal approval is in the terminal; release approval is a GitHub merge. No custom UI.
- Regex secret scan, not a full scanner.
- "Deployment" is local docker compose.
- Cost on a subscription is notional (API-equivalent).
- OpenSpec CLI commands vary by version; pinned and wrapped in one adapter.
- Generated code quality is bounded by the worker model; human review is part of the flow by design.
