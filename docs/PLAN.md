# Foreman Implementation Plan

> **For agentic workers:** execute task by task with `superpowers:executing-plans`; use `superpowers:test-driven-development` inside each task. Steps use checkbox syntax.

**Goal:** Build Foreman as specified in `DESIGN.md`: three workflows (feature, bugfix, spike), governed runner, three worker types, policy, cost and audit, GitHub and OpenSpec adapters, CLI.

**Architecture:** One flat `foreman/` package. Each agent stage is data (`Stage`) executed by one generic `run_stage()`; deterministic steps are plain node functions. Workflows only wire nodes into LangGraph graphs. Services (worker, store, github, human, git repo) live on a `Run` object closed over by the nodes, never in graph state. State holds only a serialisable context bag.

**Tech Stack:** Python 3.12+, `uv`, `pytest`, `langgraph`, `langgraph-checkpoint-postgres`, `psycopg`, `pyyaml`, stdlib `argparse`, `subprocess`.

**Spec:** `DESIGN.md` (section numbers below refer to it).

## Global Constraints

- Retries: max 2 per stage (3 attempts), previous failure fed back (§7). Fallback: one extra attempt on the `strong` tier, then safe-stop (§7).
- Branch `foreman/<run_id>` in the target repo; every successful stage commits (§5).
- Prompt built only from the agent's listed context keys under labelled headings (§6).
- Worker env: no API keys; no network tools; permissions via CLI flags only (§6).
- Validate, policy, observe verdicts are code (§3). Foreman never merges its own PR (§10).
- Cost row per agent call with fields from §8; audit row per event with fields from §11.
- Model tiers: `small=claude-haiku-5-5`, `mid=claude-sonnet-5-5`, `strong=claude-opus-5-5`.
- Unit tests: `MemorySaver`, `MemoryStore`, `FakeWorker`, `FakeGitHub`, tmp git repo. No Docker, no network. Integration tests carry `@pytest.mark.integration`.

## Decisions the design leaves open

- **Parallel stages** (implement/tests/docs, spike explorers) are parallel graph branches, but `run_stage` holds one lock around worker call, diff check and commit, so diffs attribute to one stage. Real concurrency is a known simplification.
- **Approvals** (proposal, high-impact diff, spike clarify) are synchronous `Human` callbacks (terminal or scripted). Only `await_merge` uses LangGraph `interrupt`, because only it must survive a process exit.
- **Tasks checkbox:** the Implementer cannot write `openspec/`, so agents report `DONE: <task id>` lines and Foreman ticks `tasks.md` itself.
- **Bugfix** also gets an `approve_proposal` after triage (principle "specs before code").
- **Spike clarify** takes its questions from the issue's `## Questions` section.
- **Change id** is chosen by Foreman: `<slug(title)>-<issue>`.
- **Validate failure** loops back to `implement` / `fix` with `last_failure`, max 2 loops, then rollback to the approved commit and safe-stop.
- **OpenSpec in fake mode** runs the real adapter against `python -m foreman.fake_openspec` (a structural stub), so adapter code is exercised without the CLI.

## Review Focus

- Agent text missing a required section or listing more than 3 options: gate fails with a clear message and retries; no crash.
- Secret in a new untracked file, or a binary file in the diff: scanner flags the text file and skips the binary.
- Resume after safe-stop: the failed node reruns, no duplicate commits or cost rows, same branch and run id.
- Target repo dirty, or `foreman/<run_id>` already exists: refuse to start, do not clobber.
- `claude` times out, prints non-JSON, or returns `is_error`: failed attempt with message, cost 0, no crash.

## File Structure

```
foreman/util.py      sh, glob_match, sha, section, slug, merge_dicts
foreman/config.py    Agent, Target, Scenario + loaders
foreman/gitops.py    GitRepo
foreman/policy.py    Finding, check_changes
foreman/store.py     AuditEvent, CostEvent, MemoryStore, PostgresStore
foreman/metrics.py   run_metrics, cost_table
foreman/workers.py   WorkerRequest/Result, ClaudeCodeWorker, ReplayWorker, Recorder
foreman/fake.py      FakeWorker + behaviours + fixture target + fake smoke
foreman/fake_openspec.py  structural stub of the openspec CLI
foreman/specs.py     SpecAdapter
foreman/github.py    GitHub (gh), FakeGitHub
foreman/human.py     ScriptedHuman, TerminalHuman
foreman/context.py   context providers, build_prompt
foreman/runner.py    SafeStop, Run, Stage, run_stage
foreman/nodes.py     shared deterministic nodes
foreman/pr.py        PR body + cost table
foreman/observe.py   smoke run, verdict, rollback handoff
foreman/engine.py    make_run, drive (interrupt loop), check_expect
foreman/workflows/{feature,bugfix,spike}.py   build(run) -> StateGraph
foreman/cli.py
config/agents.yaml  prompts/*.md  targets/*.yaml  scenarios/*.yaml  scenarios/inputs/*.md
docker-compose.yml  README.md
```

---

### Task 1: util, config, config files

**Files:** create `foreman/util.py`, `foreman/config.py`, `config/agents.yaml`, `targets/url-shortener.yaml` (as §13), `targets/fake.yaml`; test `tests/test_util_config.py`.

**Produces:**
- `sh(cmd: str, cwd: Path, env: dict | None = None, timeout: int | None = None) -> ShResult(code: int, out: str)`; shell=True, stdout+stderr merged, timeout gives code 124.
- `glob_match(path: str, pattern: str) -> bool` (`**` any depth, `*` within a segment).
- `sha(text: str) -> str` (12 hex), `section(text: str, heading: str) -> str`, `slug(s: str) -> str`, `merge_dicts(a, b) -> dict`.
- `Agent(name, tier, tools, writable, context)`, `Target(name, repo, paths, commands, health_url, slo, dependency_manifests, protected)`, `Scenario(name, workflow, input, target, start_ref, approvals, script, budget_usd, faults, expect)`.
- `load_agents(path) -> tuple[dict[str, Agent], dict[str, str]]` (agents, tier→model), `load_target(path)`, `load_scenario(path)`.

- [ ] Tests: `glob_match("src/a/b.py","src/**")`, `glob_match("x/A.csproj","**/*.csproj")`, `glob_match("a.csproj","**/*.csproj")` true, `glob_match("srcx/a","src/**")` false; `section("## A\nx\n## B\ny","A") == "x"`; `slug("Fix: Click Counts!") == "fix-click-counts"`; `load_agents` returns 14 agents, `implementer.tier == "mid"`, context keys ending `?` are optional; `load_target` of §13 profile round-trips; `sh("exit 3", cwd).code == 3`.
- [ ] Implement; write `config/agents.yaml` with the 14 agents of §6 (tools, writable with `{change}`, `{run_id}`, `{option}`, `{paths.src}`, `{paths.tests}`, `{paths.docs}` placeholders, context keys).
- [ ] Run `uv run pytest tests/test_util_config.py -q`, commit.

### Task 2: gitops and policy

**Files:** `foreman/gitops.py`, `foreman/policy.py`; tests `tests/test_gitops.py`, `tests/test_policy.py`.

**Produces:**
- `Change(status: str, path: str)` with status `A|M|D`.
- `GitRepo(path)`: `start_run(branch, start_ref) -> str` (refuses dirty tree or existing branch), `head() -> str`, `changes() -> list[Change]` (incl. untracked), `added_text() -> str` (added lines plus new text files, binaries skipped), `commit(message) -> str` (allows empty), `reset_hard(sha=None)` (reset + clean -fd), `diff_stat(base) -> str`, `diff_text(base, paths=None) -> str`, `tree() -> str`, `snapshot() -> dict[str, str | None]`, `restore(snapshot)`.
- `Finding(kind, detail, blocking)`; `check_changes(changes, added_text, writable, target) -> list[Finding]`. Blocking: `path` (outside writable), `secret`. Approval-needed: `dependency` (manifests), `migration` (protected), `deleted_test`, `config` (`.github/**`, `Dockerfile*`, `docker-compose*.yml`).

- [ ] Tests: start_run refuses dirty repo and existing branch; untracked file appears as `A`; `reset_hard` removes untracked files; `check_changes` flags `tests/x.py` written by an agent whose writable is `src/**` as blocking `path`; flags `AKIA` + 16 chars, a PEM header and `password = "hunter2hunter2"` as `secret`; `src/App/App.csproj` change gives non-blocking `dependency`; deleting `tests/t.py` gives `deleted_test`; binary file with random bytes does not crash the scan (Review Focus).
- [ ] Implement, run, commit.

### Task 3: store and metrics

**Files:** `foreman/store.py`, `foreman/metrics.py`; tests `tests/test_store.py`, `tests/test_metrics.py`.

**Produces:**
- `AuditEvent(run_id, workflow, stage, event, attempt=0, model="", duration_ms=0, details={}, ts="")`, `CostEvent(run_id, issue_id, workflow, stage, agent, attempt, model, input_tokens, output_tokens, cost_usd, duration_ms)`.
- `MemoryStore(runs_dir: Path | None = None)` and `PostgresStore(conninfo, runs_dir=None)`, both with `audit(ev)`, `cost(ev)`, `audit_events(run_id) -> list`, `cost_events(run_id=None, issue_id=None) -> list`. `audit()` also appends a JSON line to `runs/<run_id>/audit.jsonl` when `runs_dir` is set.
- `SCHEMA_SQL` creating `audit_events`, `cost_events` and views `v_cost_per_run`, `v_cost_per_issue`, `v_stage_success`, `v_retry_rate`.
- `run_metrics(audit, cost) -> dict` with `stage_success`, `retry_rate`, `rollbacks`, `mttr_s`, `latency_s`, `human_wait_s`, `cost_usd`, `retry_cost_usd`; `cost_table(cost, issue_cost) -> str` (markdown: agent, model, attempts, tokens, cost, duration, run total, issue total, retry cost shown separately).

- [ ] Tests: jsonl export written; `cost_events(issue_id=7)` spans two run ids; `run_metrics` on a hand-built event list: one stage failed then passed gives `retry_rate > 0` and `mttr_s` equals the gap; `cost_table` contains both totals.
- [ ] Implement, run, commit. (Postgres store is exercised in Task 12.)

### Task 4: workers, fake worker, spec adapter

**Files:** `foreman/workers.py`, `foreman/fake.py`, `foreman/fake_openspec.py`, `foreman/specs.py`; tests `tests/test_workers.py`, `tests/test_specs.py`.

**Produces:**
- `WorkerRequest(agent, model, prompt, cwd, key, stage, call)`, `WorkerResult(text, input_tokens=0, output_tokens=0, cost_usd=0.0, duration_ms=0, error=None)`.
- `ClaudeCodeWorker(timeout=1800, claude_bin="claude").run(req)`: runs `claude -p --output-format json --model M --allowedTools "..." --disallowedTools "WebFetch,WebSearch"` with prompt on stdin, `cwd=req.cwd`, env limited to `PATH,HOME,LANG,TERM`. Parses `result`, `total_cost_usd`, `duration_ms`, `usage.input_tokens/output_tokens`; non-JSON, `is_error` or timeout gives `error` set and cost 0.
- `Recorder(dir).save(key, result, snapshot)` and `ReplayWorker(dir).run(req)` (writes recorded files into `req.cwd`, returns recorded result).
- `FakeWorker(faults: list[dict])`: `run(req)` dispatches `BEHAVIORS[req.agent.name]`; deterministic tokens/cost by tier (small 0.01, mid 0.05, strong 0.20). Faults: `{stage, calls: [n,...] | all: true, kind: no_output | bad_path | secret | break_tests | cost, usd?}`; `call` counts worker calls per stage.
- `SpecAdapter(cmd: list[str], cwd: Path)`: `validate(change) -> tuple[bool, str]` (`<cmd> validate <change> --strict`), `archive(change)` (`<cmd> archive <change> --yes`, raises `RuntimeError` on failure).
- `foreman.fake_openspec` CLI stub: `validate` checks `proposal.md`, `design.md`, `tasks.md` exist and a spec delta has `## ADDED|MODIFIED Requirements` with `#### Scenario:`; `archive` moves the change to `openspec/changes/archive/` and copies deltas to `openspec/specs/<cap>/spec.md`.

- [ ] Tests: `ClaudeCodeWorker` against a stub `claude` script on PATH (JSON ok, non-JSON, `is_error`, sleep past timeout); `ReplayWorker` reproduces files; `FakeWorker` fault `no_output` on call 0 only; `SpecAdapter` validate fails on a change missing `design.md`, passes on a complete one, archive moves it.
- [ ] Implement (behaviours are added per workflow in Tasks 7, 9, 10), run, commit.

### Task 5: GitHub adapters and human

**Files:** `foreman/github.py`, `foreman/human.py`; tests `tests/test_github.py`, `tests/test_human.py`.

**Produces:**
- `Issue(number, title, body, labels)`, `PRState(state: "open"|"merged"|"closed", merge_sha: str | None, comments: list[str])`.
- Interface on `GitHub` (via `gh`) and `FakeGitHub(root: Path, repo: GitRepo)`: `create_issue(title, body, labels) -> int`, `get_issue(n) -> Issue`, `comment(n, body)`, `push(branch)`, `open_pr(branch, title, body, labels, closes) -> int`, `pr_state(pr) -> PRState`, `merged_commit(pr) -> str`, `ensure_repo(name)`; `GitHub.ensure_repo` also enables branch protection on `main`.
- `FakeGitHub` extras: `merge(pr)` (real `git merge --no-ff` into `main`), `close(pr, comment)`. Files under `root/issues/<n>.json`, `root/prs/<n>.json`.
- `Decision(approved, note="", who="")`; `ScriptedHuman(script: dict[str, list[str]])` with `approve(kind, summary) -> Decision` (entries `"approve"`, `"reject: <note>"`, default approve when exhausted) and `answer(question) -> str | None`; `TerminalHuman()` same interface using `input()`.

- [ ] Tests: FakeGitHub issue numbering, PR file contains `Closes #1`, `merge` makes `merged_commit` valid; `GitHub` builds the expected `gh` argv (inject a recording `sh`); ScriptedHuman reject-then-approve.
- [ ] Implement, run, commit.

### Task 6: context and runner

**Files:** `foreman/context.py`, `foreman/runner.py`; test `tests/test_runner.py`.

**Produces:**
- `build_prompt(run, agent, ctx, last_failure) -> str`: contents of `prompts/<agent>.md`, then `## Boundaries` (writable paths, branch, change id), then one `## <key>` section per context key (ctx first, then provider; missing non-optional key raises). Providers: `repo_tree`, `target_profile`, `existing_specs`, `diff_stat`, `cost_table`.
- `SafeStop(reason, rollback=False)`.
- `Run` dataclass: `run_id, issue_id, scenario, target, agents, models, repo, worker, store, github, human, spec, runs_dir, recorder, lock`; methods `audit(stage, event, **details)`, `run_cost() -> float`.
- `Stage(name, agent, produces, gate, placeholders=None, after=None)`; `gate(run, ctx, text) -> str | None` (failure message), `after(run, ctx, text) -> dict` (ctx updates).
- `run_stage(run, stage, state) -> dict` returning `{"ctx": {...}, "done": {stage: inputs_hash}}`. Order: skip if `done[stage]` equals current inputs hash; entry gate (context keys present); budget check; attempt loop (3 attempts + 1 fallback on strong tier); per attempt: worker call under `run.lock`, cost row, record, `changes()` → `check_changes`; blocking finding or gate failure → `reset_hard`, audit `retry`, continue with failure fed back; approval-needed findings → `human.approve("high_impact", ...)`, rejection counts as failure; success → `after` hook, commit, audit `stage_passed`. Exhausted → `SafeStop`.

- [ ] Tests (tmp git repo, FakeWorker with a trivial behaviour): passes first time and writes one cost row; gate fails once then passes (audit has `retry`, 2 cost rows, attempt numbers 0 and 1); three failures then success on `strong` model (audit `fallback`); `bad_path` fault always: `SafeStop` and working tree clean; budget `0.001` gives `SafeStop("budget")` before any call; same inputs hash skips the stage; `dependency` finding with ScriptedHuman reject counts as failure.
- [ ] Implement, run, commit.

### Task 7: feature workflow end to end (fake)

**Files:** `foreman/nodes.py`, `foreman/pr.py`, `foreman/engine.py`, `foreman/workflows/feature.py`, `prompts/*.md` for feature agents, feature behaviours in `foreman/fake.py`, `targets/fake.yaml`, `scenarios/greenfield.yaml`, `scenarios/inputs/feature-url-shortener.md`; test `tests/test_feature_e2e.py`.

**Produces:**
- Nodes (each `fn(run) -> Callable[[State], dict]`): `intake`, `clarify`, `approve_proposal` (records `approved_commit`; reject sets `reviewer_notes`, max 3 rejections), `validate_feature` (build, full tests, all `tasks.md` boxes ticked, `spec.validate`; failure sets `last_failure`), `archive_spec`, `open_pr`, `await_merge` (§5.1 with `interrupt`).
- `pr.build_body(run, ctx, narrative) -> str` with every §9 item.
- `workflows.feature.build(run) -> StateGraph` following §5.1 (conditional edge to `clarify` when the requirements text has a `## Blocking questions` section with bullets; fan-out/join for implement, tests, docs; validate loop back to `implement` max 2 then `SafeStop(rollback=True)`).
- `engine.make_run(scenario, mode, runs_dir, store=None) -> Run` (builds fake target repo from fixture when mode is `fake`), `engine.drive(run, graph, config) -> Result(status, pr, ...)` (invokes, and when the graph pauses at `await_merge` under scripted approvals calls `FakeGitHub.merge`, then resumes), `engine.check_expect(scenario, result, run) -> list[str]` (failures).
- On `SafeStop` during a node: audit `safe_stop`; if `rollback`, `repo.reset_hard(approved_commit)` and audit `rollback`; result status `safe_stopped`.

- [ ] Test: run `greenfield` fake: status `completed`, PR body contains cost table and `Closes #1`, `docs/` and `src/` files exist on `main` after merge, `openspec/specs/` updated, audit contains `stage_passed` for every stage; a proposal rejection (script `reject: add rate limit` then `approve`) re-runs `proposal` and downstream stages; Review Focus: architect output missing `tasks.md` is retried.
- [ ] Implement, run, commit.

### Task 8: fault-injection scenarios

**Files:** `scenarios/faults/*.yaml`, `tests/test_faults.py`.

- [ ] Five tests on the feature workflow in fake mode, each asserting audit events and final status: retry-then-success (`implement` call 0 `break_tests`: validate loop, `retry`), fallback (`tests` calls 0-2 `no_output`: `fallback` event, strong model in cost row), policy block (`implement` all calls `bad_path`: `safe_stopped`, reason mentions path, tree clean), budget safe-stop (`budget_usd: 0.05`, fault `cost` 1.0: `safe_stopped` reason `budget`), rollback (`implement` calls 0-2 `break_tests`: `rollback` event, branch head equals `approved_commit`).
- [ ] Add resume test: after the budget stop, raise the budget, resume the same thread: failed node reruns, no duplicate commits, same run id (Review Focus).
- [ ] Commit.

### Task 9: bugfix workflow

**Files:** `foreman/workflows/bugfix.py`, bugfix behaviours and `v1-regressed` fixture in `foreman/fake.py`, prompts, `scenarios/brownfield.yaml`, `scenarios/inputs/bug-click-counts.md`; test `tests/test_bugfix_e2e.py`.

**Produces:** graph per §5.2 plus `approve_proposal` after triage; `reproduce` gate: suite fails and test count increased; `validate_bugfix`: reproduction test passes, suite passes, count ≥ baseline + 1; `rca` gate: `docs/rca/<issue>.md` contains the 7 required headings.

- [ ] Tests: brownfield fake run completes; a reproducer whose new test passes is rejected by its gate ("no failing test, no fix"); fixer that deletes the failing test fails validate; PR body links the RCA file.
- [ ] Implement, run, commit.

### Task 10: spike workflow

**Files:** `foreman/workflows/spike.py`, spike behaviours in `foreman/fake.py`, prompts, `scenarios/ambiguous.yaml`, `scenarios/inputs/spike-analytics.md`; test `tests/test_spike_e2e.py`.

**Produces:** graph per §5.3. `parse_options(text) -> list[Option(id, title, body)]` (max 3, else gate failure); explore via `Send` per option, notes at `spikes/<run_id>/<option>/notes.md`; analysis gate: 7 required sections plus `Draft user story`; before `open_pr` the exploration scratch is copied to `runs/<run_id>/spikes/` and removed from the branch; `handoff` creates a feature issue (label `foreman:feature`) from the draft story and comments the link on the spike issue.

- [ ] Tests: unanswered clarify question appears under `assumptions`; 3 explorers each write only in their option dir; a fourth option is rejected by the frame gate; after merge a new issue exists whose body quotes the draft story; PR diff contains only `docs/spikes/<run_id>/analysis.md`.
- [ ] Implement, run, commit.

### Task 11: observe and cross-workflow handoffs

**Files:** `foreman/observe.py`; tests `tests/test_observe.py`.

**Produces:** `run_smoke(run) -> Metrics(requests, errors, p95_ms, log)` (runs `commands.run`, `commands.smoke` whose last stdout line is JSON, then `commands.stop`; fake mode swaps in `FakeEnv.smoke`); `verdict(metrics, slo) -> tuple[bool, str]` pure; `observe` node: pass records audit `observe_passed` and comments on the issue; fail runs the Observer agent for a summary, opens a revert PR (`git revert -m 1 <merge_sha>` on `foreman/revert-<run_id>`) and creates a bugfix issue labelled `foreman:bugfix`, `foreman:needs-approval`, then ends `status=observe_failed`.

- [ ] Tests: `verdict` boundaries (error rate exactly 0.01 passes, p95 201 fails); fake fault `slo_breach` yields revert PR file plus bugfix issue; `engine` refuses `run --issue` on an issue labelled `foreman:needs-approval` without `foreman:ready`.
- [ ] Implement, run, commit.

### Task 12: CLI, recording and replay, Postgres, docs

**Files:** `foreman/cli.py`, `docker-compose.yml`, `README.md`, remaining `prompts/*.md`; tests `tests/test_cli.py`, `tests/test_postgres.py` (integration).

**Produces:** `foreman run <scenario> [--mode fake|live|replay] [--issue N] [--recording DIR]`, `foreman resume <thread>`, `foreman metrics <run>`, `foreman cost --issue <id>`, `foreman issue create <file> --type feature|bugfix|spike`. Run metadata saved to `runs/<run_id>/run.json` (scenario copy, mode, target path, issue) so `resume` can rebuild the `Run`. `FOREMAN_DB_URL` selects `PostgresStore` + `PostgresSaver`; unset means memory (resume across processes then unavailable, said in the error). Live mode records every worker call to `runs/<run_id>/recorded/`.

- [ ] Tests: `run scenarios/greenfield.yaml --mode fake` exits 0 and prints PR number and run cost; `cost --issue 1` sums two runs; integration: `PostgresSaver` pause at `await_merge` and resume from a new connection, `PostgresStore` views return rows (skipped when Docker or Postgres is unavailable).
- [ ] README: quick start, modes, prerequisites for live (`claude`, `gh auth login`, pinned `openspec`), how to create `v1-regressed`, known simplifications (serialised agent calls, regex secret scan, `openspec` CLI flags unverified against a live install).
- [ ] Run full suite `uv run pytest -q`, commit and push.

---

## Implementation notes (what changed while building)

- `PostgresStore` and the SQL views landed in Task 12, not Task 3, so they could be tested against a real Postgres.
- Stages can run in a `sandbox` (an empty directory): the Observer uses it so it never sees source.
- Attempt numbers are a per-stage-instance call counter taken from the cost table, so retries, fallbacks and validation loop-backs all show up as `attempt > 0` in the retry-spend figure.
- A failed validation re-enters the parallel fan-out; the unchanged stages are skipped by input hash instead of re-run.
- `FileStore` reads the `audit.jsonl` / `cost.jsonl` exports so `foreman metrics` and `foreman cost` work without a database.
- Added beyond the plan: `foreman resume --poll`, status comments on the issue at every gate, `prepare_live_target` (creates the local and GitHub repos on the first greenfield run), `RunRefused` for issues awaiting `foreman:ready`.
