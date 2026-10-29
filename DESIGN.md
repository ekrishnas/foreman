# Foreman: Design

## 1. Purpose

Foreman is a governed agentic SDLC orchestrator. A developer hands it a GitHub issue from inside Claude Code; Foreman works out what kind of work it is, runs the matching workflow graph, and stops for a human wherever a decision belongs to a person. LLM agents do the work inside defined autonomy boundaries. Foreman owns control: classification, ordering, gates, approvals, policy, retries, rollback, cost and audit.

Foreman is product-agnostic. It is a tool that is cloned once and lives apart from any product. Everything that belongs to the orchestrator (code, agent definitions, prompts, permissions) lives in the Foreman repo. Everything that belongs to a product lives in that product's repo: the `/foreman` skill that drives Foreman, a single profile file (`.foreman/target.yaml`) that tells Foreman the product's commands and paths, and the work product (code, tests, docs, OpenSpec artifacts). Foreman ships no default product and its code and prompts name none; a run is told which repo to work on.

## 2. How a developer uses it

```
/foreman work on #1                              (in Claude Code, opened in the product repo)
   |
   |  the skill: finds or clones Foreman, sets up OpenSpec, identifies the tracker and checks login and
   |  repo access, creates .foreman/target.yaml, commits the setup
   |  then runs:  foreman run 1 --repo <product repo> --request "work on #1"      (background)
   v
Foreman: preflight -> classifier agent reads the request + issue + repo -> picks a graph -> runs it
   |
   |  needs a human?  writes a request to the store and waits
   v
Claude polls  foreman pending  -> shows the question to the developer -> foreman respond <id> ...
   |
   v
PR opened -> developer reviews and merges on GitHub -> run completes -> Claude reports
```

The front door is a Claude Code skill (`.claude/skills/foreman/SKILL.md` in the product repo, copied from `skill/foreman/SKILL.md` in the Foreman repo). It is a prompt, not code. On the first use in a product it:

1. finds the Foreman checkout, or asks where it is, or asks permission to clone it outside the product repo and installs its dependencies;
2. sets up OpenSpec: checks the CLI (installing the pinned version with permission) and runs `openspec init --tools claude` (non-interactive) if the repo has no `openspec/` folder;
3. identifies the issue tracker from the repo's remote or a pasted issue URL, checks the tracker's CLI is installed and logged in, and checks the developer has write access to the repo. Only GitHub is supported today; for Azure DevOps and Jira it says so and stops;
4. creates the product profile `.foreman/target.yaml` by inspecting the repo and asking the developer to confirm;
5. commits the setup files (with permission), because a run will not start from a dirty working tree.

On every use it then starts the run, relays questions, and reports. Claude never does engineering work and never answers on the developer's behalf. The same commands work from any terminal.

## 3. Workflows at a glance

The classifier chooses one of seven types. The developer never names a graph.

| Type | When | Output |
|---|---|---|
| `new_implementation` | build something new; the repo has no application code yet | pull request + docs + specs |
| `existing_change` | add or change a feature in a repo that already has code | pull request + docs + specs |
| `bugfix` | something that worked is broken | pull request with a failing test first + RCA document |
| `test_improvement` | add or strengthen tests; no code changes | pull request with tests only |
| `doc_improvement` | write or fix documentation | pull request with docs only |
| `spike` | the requirement is ambiguous or open-ended | analysis document PR + a follow-up issue from its draft story |
| `observe` | the work is deployed; check its logs | a verdict, evidence and the log location, posted on the issue |

Every PR summary includes the run's cost breakdown and the cumulative cost for its issue.

## 4. Principles

- The graph controls; agents execute.
- LLMs propose; deterministic checks decide. The classifier's answer is checked against the repo, an impact analysis is checked for files that exist, a reproduction must fail, the observer's evidence must be quoted from the log.
- Least privilege per agent: model, tools, writable paths and context are scoped per agent.
- Verify, don't trust: every agent's changes are checked against policy using the git diff.
- Every decision is traceable: inputs, outputs, gate results, approvals, retries and cost are recorded.
- Specs before code: changes that alter behaviour are OpenSpec changes, validated and approved before implementation.
- Product-agnostic: no product name in code or defaults. Everything product-specific is a target profile.

## 5. Components

| Component | Responsibility |
|---|---|
| Front door | the `/foreman` skill, `skill/foreman/SKILL.md`, installed per product repo: sets up, starts runs, relays human questions, reports |
| CLI | `foreman run`, `resume`, `pending`, `respond` |
| Engine | builds a run, runs the router, compiles and drives the chosen graph, handles safe-stop |
| Router | `workflows/route.py`: the classifier stage and its code checks |
| Workflow library | seven graphs, one file each, built from the shared nodes in `workflows/nodes.py` |
| Runner | wraps every agent stage: entry gate, prompt assembly, worker call, policy check, exit gate, retries, cost capture, audit |
| Worker | `ClaudeCodeWorker`: one `claude -p` subprocess per attempt |
| Policy engine | diff-based checks after each stage |
| Spec adapter | wraps the OpenSpec CLI (validate, archive) |
| GitHub adapter | the only code that talks to GitHub (`gh`): issues, comments, PRs, merge state. Agents never get GitHub access |
| Human channel | `StoreHuman` writes a question to the store and blocks until `foreman respond` answers it |
| Store | one SQLite file (`runs/foreman.db`): audit events, cost events, human requests, and LangGraph checkpoints |
| Config | `config/agents.yaml` and `prompts/` in Foreman; `.foreman/target.yaml` in each product repo |

## 6. Run lifecycle

1. **Start.** `foreman run <issue> --repo <product repo> --request "<developer's words>"`. Preflight checks `git`, `gh`, `claude`, `openspec` are on PATH and `gh` is logged in. The product repo must be a git repository containing `.foreman/target.yaml`; it is given a first commit if it has none, and a private GitHub repository if it has no remote.
2. **Classify.** The classifier agent (Sonnet class, read-only, sandboxed: it sees only the prompt) gets the request, the issue, the target profile, the repo file tree and the existing specs. It answers `Type: <type>` and a rationale. Code rejects an answer that contradicts the repo: `new_implementation` when the repo already has application code; `existing_change`, `bugfix` or `test_improvement` when it has none. A rejected answer goes back to the agent (the usual retry). The decision is audited and posted as an issue comment.
3. **Branch.** For every type except `observe`, Foreman creates `foreman/<run_id>` in the target repo. Each stage commits. `observe` changes nothing, so it creates no branch and tolerates an untracked log folder.
4. **Run the graph.** Checkpointed to SQLite after every node.
5. **Finish.** The run ends `completed` or `safe_stopped`. The chosen type is saved in `runs/<run_id>/run.json`, so `foreman resume` skips classification.

`safe_stop` is reachable from every node.

## 7. Workflows

### 7.1 new_implementation

```
intake -> requirements -> [clarify if blocking questions] -> proposal -> APPROVE
  -> implement || tests || docs -> validate -> archive_spec -> pr_summary -> open_pr -> AWAIT_MERGE
```

- **proposal** writes an OpenSpec change: `proposal.md`, `design.md` (including the API contract), `tasks.md`, spec deltas. Exit gate: `openspec validate <change> --strict`.
- The **contract** in `design.md` is the synchronization point that lets implement, tests and docs run in parallel. Tests are written from the contract and acceptance criteria, not from the implementation.
- **implement** must end with an `## Observability` section (see 7.8). Gate: build passes and the section is complete.
- **validate** (deterministic): build, full test suite, all `tasks.md` items checked, OpenSpec validate, policy over the whole branch. When those pass, the **review** (7.9) runs. A failure of either sends the three parallel stages round again with the failure as context, up to 2 times, then safe-stops with a rollback to the approved commit.
- **archive_spec**: `openspec archive <change> --yes` on the branch, so living specs update inside the same PR.
- **open_pr**: push, open a PR with `Closes #<issue>` and the full summary; post the observability notes on the issue.
- **AWAIT_MERGE**: a human reviews and merges on GitHub. The run waits in-process, polling the PR. Closed without merge: safe-stop with the reviewer's comments recorded.

### 7.2 existing_change

The same graph with two extra steps between intake and requirements:

- **baseline**, only when `openspec/specs/` has no spec yet. OpenSpec's specs start empty and fill as changes are archived, so the first change on an existing product has nothing to build on, and a bugfix has nothing to modify. A baseliner agent explores the code and writes a baseline change describing the *current* behaviour of the part of the system the issue concerns (ADDED requirements only, at most five capabilities). Code validates it, and Foreman archives it at once, into the same branch and PR. It happens once per product.
- **impact**. The impact analyst (read-only) reads the code and lists impacted files, modules and APIs, data flows and risks. Code checks that every file listed exists. The result feeds requirements and the architect.

### 7.3 bugfix

```
intake -> [baseline] -> triage -> APPROVE -> reproduce -> fix -> validate (+ review) -> rca
  -> archive_spec -> pr_summary -> open_pr -> AWAIT_MERGE
```

- **baseline**: as in 7.2, when the product has no specs yet.
- **intake** records a baseline of the current test suite, and any logs pasted under `## Logs` in the issue.
- **triage** writes an OpenSpec change with a MODIFIED requirement and a scenario describing correct behaviour, plus impacted modules and a root-cause hypothesis. A MODIFIED block must keep every scenario the existing requirement has (an OpenSpec rule). If the product has no specs at all, the gate accepts ADDED instead.
- **reproduce** exit gate: the new test **fails** on the current code, the suite grew, and no existing test broke. No failing test, no fix.
- **fix** gate: build passes and the `## Observability` section is complete. **validate**: that test passes, the full suite passes, the count has not dropped.
- **rca** writes `docs/rca/<issue>.md`: summary, impact, timeline, root cause, why it was missed, fix, prevention. Gate: all sections present.

### 7.4 test_improvement

```
intake (test baseline) -> improve_tests -> validate -> pr_summary -> open_pr -> AWAIT_MERGE
```

The test improver may write only under the tests path, so a source change is blocked by policy. Gate: the suite passes, grew, and lost no tests. No OpenSpec change and no proposal approval: behaviour does not change.

### 7.5 doc_improvement

```
intake -> improve_docs -> validate -> pr_summary -> open_pr -> AWAIT_MERGE
```

The doc improver may write only under the docs path and `README.md`. Gate: documentation was written, and every relative markdown link in what changed points at a file that exists.

### 7.6 spike

```
intake -> clarify -> frame -> explore(option 1 || option 2 || option 3) -> analysis
  -> prune_scratch -> open_pr (analysis only) -> AWAIT_MERGE -> handoff
```

- **clarify**: questions from the issue's `## Questions` section go to the human; unanswered ones become stated assumptions.
- **frame** lists up to three candidate approaches. **explore** runs one agent per option in parallel (dynamic fan-out), each writing only to `spikes/<run_id>/<option>/`.
- **analysis** writes `docs/spikes/<run_id>/analysis.md`: context, assumptions, options, trade-off matrix (complexity, cost, performance, risk, operability), recommendation, open questions, and a draft user story.
- Only the analysis document is merged. **handoff** files a new GitHub issue from the draft story, linked to the spike; that issue can then go through `/foreman` like any other.

### 7.7 observe

```
gather -> locate_logs -> read_logs -> observe -> report
```

Started by a sentence such as "issue #1 is deployed to qa, check the logs". There is no branch and no PR.

- **gather** reads the issue and its comments: the implementer's observability notes, and any log location saved earlier.
- **locate_logs** finds the logs, in order: a path in the developer's message; a location saved on the issue (`Foreman log location: ...`); the `Log location` in the implementer's notes. If none resolves to something that exists, it **asks the developer** (twice at most) and saves the answer as an issue comment, so the next run does not ask.
- **read_logs** reads the last 300 lines (up to 200 KB) of the file, or of the newest file in a folder.
- **observe**: the observer agent sees only the request, the issue, the notes and the excerpt, in a sandbox with no source. It answers `Verdict: working|failing|inconclusive`, a summary and evidence. Code checks that every log line it quotes is really in the excerpt; invented evidence is rejected and retried.
- **report** posts the verdict, the evidence and **the log location that was used** on the issue.

Local files only. A log service integration (Datadog and similar) is a later improvement.

### 7.8 Observability notes

Every code-writing agent (implementer, fixer) must end its output with:

```
## Observability
- Log location: <file path relative to the repo root>
- Success: <an example line that shows the feature working>
- Errors: <strings or patterns that show it failing>
```

This is enforced by a gate. The notes go into the PR description and are posted as an issue comment, which is where `observe` finds them later.

### 7.9 Review

OpenSpec has no `review` command. Its review step is `verify`, an optional skill that checks an implementation against its change artifacts. Foreman runs that method through its own **reviewer** agent, in `new_implementation`, `existing_change` and `bugfix`, inside the validate step once build, tests, tasks, OpenSpec validation and policy have all passed (no point reviewing code that does not build).

- The reviewer (read-only) gets the change summary, design, current tasks, delta specs, the validation results and the branch diff. It checks **completeness** (tasks done, every requirement implemented), **correctness** (implementation matches intent, every scenario handled and tested) and **coherence** (design followed, project patterns), and reports issues as CRITICAL, WARNING or SUGGESTION, each with a recommendation and `file:line` references.
- Code checks the report has every section and that **every `file:line` it cites exists**; otherwise the attempt is rejected and retried.
- Any **CRITICAL** issue fails validation, so implement, tests and docs run again with the issue as context (the same loop and the same limit as any validation failure). Warnings and suggestions never block; they appear in the PR under "Review (OpenSpec verify)".

## 8. Agents

Each agent is a separate `claude -p` invocation with its own model, tools, writable paths, context and prompt, defined in `config/agents.yaml` and `prompts/<agent>.md`.

| Agent | Graphs | Tier | Tools | Writable | Context given |
|---|---|---|---|---|---|
| Classifier | router | mid | Read (sandbox) | none | request, issue, target profile, repo tree, existing specs |
| Baseliner | existing_change, bugfix (first run only) | mid | Read, Grep, Write, OpenSpec helpers | `openspec/changes/baseline-<id>/` | issue, target profile, repo tree |
| Impact analyst | existing_change | mid | Read, Grep | none | issue, target profile, repo tree, existing specs |
| Requirements analyst | new_implementation, existing_change | strong | Read | none | issue, target profile, repo tree, impact analysis |
| Architect | new_implementation, existing_change | strong | Read, Write, OpenSpec helpers | `openspec/changes/<id>/` | requirement, assumptions, clarifications, impacted files, existing specs, reviewer notes |
| Implementer | new_implementation, existing_change | mid | Read, Edit, Write, build cmd | `paths.src` | design, tasks, reviewer notes, last failure |
| Test writer | new_implementation, existing_change | mid | Read, Edit, Write, test cmd | `paths.tests` | contract, acceptance criteria, spec scenarios (not the implementation) |
| Doc writer | new_implementation, existing_change | small | Read, Write | `paths.docs` | design, contract |
| Triager | bugfix | strong | Read, Grep, Write, OpenSpec helpers | `openspec/changes/<id>/` | bug report, log excerpt, repo tree, existing specs |
| Reproducer | bugfix | mid | Read, Edit, Write, test cmd | `paths.tests` | triage output |
| Fixer | bugfix | mid | Read, Edit, Write, build/test cmd | `paths.src` | triage, failing test, failure output |
| RCA writer | bugfix | strong | Read, Write | `docs/rca/` | triage, failing test, fix diff, validate results |
| Reviewer | new_implementation, existing_change, bugfix | mid | Read, Grep, OpenSpec read helpers | none | change summary, design, tasks, delta specs, validation results, branch diff |
| Test improver | test_improvement | mid | Read, Grep, Edit, Write, test cmd | `paths.tests` | request/issue, target profile, repo tree |
| Doc improver | doc_improvement | mid | Read, Grep, Edit, Write | `paths.docs`, `README.md` | request/issue, target profile, repo tree, existing specs |
| Framer | spike | strong | Read | none | requirement, clarifications/assumptions, repo tree, existing specs |
| Explorer (one per option) | spike | mid | Read, Grep, Write | `spikes/<run_id>/<option>/` | one option, constraints, repo tree |
| Analyst | spike | strong | Read, Write | `docs/spikes/<run_id>/` | framing, all explorer notes |
| Observer | observe | mid | Read (sandbox) | none | request, issue, observability notes, log file name, log excerpt |
| PR summarizer | all PR graphs | small | Read | none | diff stat, OpenSpec change, validate results, approvals, cost table |

**OpenSpec helpers** are the CLI commands `context`, `list`, `status`, `instructions`, `show` and `validate` (plus `new change` for the agents that write a change). They let an agent get the installed OpenSpec version's own templates and the project's rules from `openspec/config.yaml` instead of relying on a format described in a prompt. `archive` and `apply` are in no agent's permissions: only Foreman's code archives, and agents never run OpenSpec's `apply`.

Not agents (deterministic code): intake, clarify (human), validate, release, policy, cost, PR assembly, log location and reading.

Rules for all agents:
- The runner builds each prompt **only** from the agent's listed context keys, under labelled headings; on retry it adds the previous gate failure.
- Permissions are passed as CLI flags per invocation (`--model`, `--allowedTools`, `--disallowedTools`). Nothing is written into the target repo to configure agents.
- No web or network tools. Workers get a minimal environment: no API keys, no secrets.
- A *sandbox* stage (classifier, observer) runs in an empty directory under `runs/<run_id>/sandbox/`, not in the repo.

## 9. Human interaction

Foreman never prompts on its own terminal, because it is usually started by Claude Code with no one at the keyboard.

- `StoreHuman` inserts a row in `human_requests` (`run_id, kind, prompt`) and polls it every 2 seconds until it is answered or 24 hours pass (then safe-stop).
- `foreman pending [--run ID] [--wait SECONDS]` lists open requests; `--wait` blocks until one exists, the run has finished, or the time is up.
- `foreman respond <id> --approve | --reject --note TEXT | --answer TEXT` answers one.
- Kinds: `proposal` (approve the OpenSpec change), `high_impact` (a dependency, migration, deleted-test or CI/config change), `question` (a clarifying question or a log location). Release is not a request: it is the PR merge on GitHub.

## 10. Governance

- **Gates:** entry and exit on every node.
- **Human approval:** the proposal, any high-impact diff, and the PR merge. Branch protection on `main` makes it impossible for Foreman to merge its own work.
- **Policy (on the diff):** writable paths only; secret pattern scan; dependency, migration, deleted-test and CI/config changes need approval; protected paths.
- **Cost budget:** per-run cap (default $10); exceeding it triggers safe-stop before the next agent call.
- **Retries:** max 2 per stage, previous failure fed back as context.
- **Review:** a CRITICAL issue from the reviewer fails validation (7.9).
- **Rollback:** before merge, a validation that fails 3 times resets the run branch to the last approved commit.
- **Safe-stop:** persist state, record the reason, comment on the issue, exit non-zero, resumable with `foreman resume`.

## 11. Cost tracking

- `claude -p --output-format json` returns cost, token usage and duration. Every agent call records a `cost_events` row: `run_id, issue_id, workflow, stage, agent, attempt, model, input_tokens, output_tokens, cost_usd, duration_ms`.
- On a Claude subscription the figure is the API-equivalent (notional) cost; it is labelled as such.
- The PR summary includes a cost table (agent, model, attempts, tokens, cost, duration), the run total, retry cost shown separately, and the total for the issue across all runs. The classifier's call is part of the run.

## 12. PR summary

Assembled by code, with a short narrative from the PR summarizer agent:

- Linked issue, type, and OpenSpec change id
- What changed and why (narrative)
- Files changed (diff stat)
- Gates passed, retries, policy findings
- Approvals: who, when, notes
- Test results
- Observability notes (code-writing graphs)
- Cost table (run and issue totals)
- Risks and rollback instructions
- Bugfix only: link to the RCA document

## 13. GitHub integration

- The product is a real GitHub repo. Foreman creates it on the first run if the folder has no remote (the skill asks the developer first), with branch protection on `main` (PR required). GitHub Issues and GitHub pull requests are the only tracker and code host supported today.
- **Issues are the unit of work.** The developer files an issue the normal way; Foreman reads it with `gh`. No labels are required.
- **Status on the issue:** Foreman comments at each gate (classification, proposal ready for approval, validation results, PR opened, merged, observe verdict), so the issue thread is a human-readable audit trail alongside the database.
- **PRs:** one per run, `Closes #<issue>`, labelled `foreman:<type>`.
- **Credentials:** `gh` is authenticated as the human; only the GitHub adapter calls it. Workers get no token and no network.

## 14. Storage and audit

One SQLite file, `runs/foreman.db`, shared by `foreman run` and the commands that talk to it (`pending`, `respond`) running in other processes.

- `audit_events`: `run_id, workflow, stage, event, attempt, model, duration_ms, details (json), ts`
- `cost_events`: see section 11
- `human_requests`: `id, run_id, kind, prompt, status, approved, note, answer, who, created, answered`
- LangGraph checkpoints, in the same file, so a crashed or safe-stopped run can be resumed.

Files per run in `runs/<run_id>/`: `run.json` (config, request, chosen type), `sandbox/` (the working directory of sandboxed stages) and, for a spike, `spikes/` (the exploration notes, kept after they are dropped from the branch).

## 15. Product profile (`.foreman/target.yaml`, in the product repo)

This is everything Foreman is told about a product. The skill creates it on first use (it inspects the repo and asks the developer to confirm) and the developer commits it, so it is versioned with the product and shared by the team. The repo it describes is the one that contains it.

```yaml
name: my-app                     # the GitHub repo name if Foreman has to create one; defaults to the folder name
spec_format: openspec
paths: { src: "src/**", tests: "tests/**", docs: "docs/**" }
commands:
  build: dotnet build
  test: dotnet test
dependency_manifests: ["**/*.csproj"]
protected: ["**/Migrations/**"]
```

`paths` scope what each agent may write; `commands.build` and `commands.test` become the scoped `Bash(...)` permissions of the code-writing agents and are what the gates run; `dependency_manifests` and `protected` decide what needs human approval. `examples/target.yaml` in the Foreman repo is a template. Agents may not write `.foreman/` or `.claude/`: they are outside every writable path.

## 16. Infrastructure

- No servers. SQLite is a file. There is no Docker requirement for Foreman itself.
- External CLIs: `claude` (logged in), `gh` (authenticated), `openspec` (pinned to 1.15.0), `git`, `uv`. The product's own toolchain (for example `dotnet`) is needed by the gates. The skill checks all of these and offers to install what is missing.
- Foreman is cloned once per machine and shared by any number of products; its `runs/` folder holds the SQLite file and every run's files.
- Python 3.13 (`.python-version`).

## 17. Testing

`pytest` runs offline with doubles in `tests/doubles/`: a fake worker that plays each agent from a script, a fake GitHub, an OpenSpec stand-in, a small throwaway Python project as the target, and a scripted human. Faults (bad output, bad paths, secrets, budget, broken tests) are injected per stage. What this proves is Foreman's own logic: routing and its checks, graph wiring, gates, retries, policy, rollback, safe-stop and resume, the human channel, observe's log finding and evidence check. It does not prove that real agents write good output, that the prompts work, or that `claude -p`, `gh` and `openspec` behave as assumed; only a real run does.

## 18. Repo layout

```
foreman/                 engine.py, cli.py
  core/                  config, runner, context, policy, human, util
  adapters/              github, gitops, specs, workers
  state/                 store (SQLite)
  reports/               pr body and cost table
  workflows/             nodes (shared), route, and one file per graph
config/agents.yaml       prompts/ (one file per agent)
skill/foreman/SKILL.md   the /foreman skill, copied into each product repo's .claude/skills/foreman/
examples/                a template profile and two sample issues
tests/                   unit and end-to-end tests; doubles/ has the fakes
runs/                    SQLite database and per-run files (git-ignored)
DESIGN.md  README.md
```

Product repo (what the skill adds): `.claude/skills/foreman/SKILL.md`, `.foreman/target.yaml`, `openspec/`.

## 19. Limitations and later improvements

- Single machine; no distributed execution or queue.
- Proposal approval and questions go through Claude Code (or `foreman respond`); release is a GitHub merge. No custom UI.
- Regex secret scan, not a full scanner.
- Cost on a subscription is notional (API-equivalent).
- OpenSpec CLI commands vary by version; pinned and wrapped in one adapter.
- Generated code quality is bounded by the worker model; human review is part of the flow by design.
- Parallel stages share one git working tree and take turns under one lock; true concurrency needs a worktree per branch.

Cut from the first version, to be added later: driving OpenSpec's own skills (`/opsx:propose`, `/opsx:apply`, `/opsx:verify`) from the agents, once they are shown to work under `claude -p` (today the agents use only the CLI helpers); Azure DevOps and Jira (split the GitHub adapter into a tracker and a code host, then add an adapter for each; the skill already recognises both and stops with a clear message); skipping unchanged stages after a failed validation (input hashing); falling back to a stronger model after retries, and recording worker output for replay; `foreman metrics` and `foreman cost` and SQL metric views; a log-service integration (Datadog) for `observe`; an automatic deploy-and-smoke-test stage with revert on SLO breach; Postgres for a shared, multi-user installation.
