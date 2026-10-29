# Foreman workflow graphs

[Back to the README](README.md) · [Design](DESIGN.md)

Each kind of work is a LangGraph graph built by `foreman/workflows/<type>.py`. The diagrams below were taken from the compiled graphs, so the boxes and arrows are the ones in the code; the words on the dashed arrows come from the routing functions. Node names are the names the code and `foreman logs` use.

- **Blue rectangle:** an agent stage. An LLM works inside its boundaries (its own model, tools and writable paths) and code checks the result. The second line is the agent's name in `config/agents.yaml`.
- **Grey rounded box:** plain code, no LLM.
- **Amber hexagon:** Foreman stops and waits for you.
- **Solid arrow:** always taken. **Dashed arrow:** a choice made by code.

- [From a request to a graph](#from-a-request-to-a-graph)
- [Inside every agent stage](#inside-every-agent-stage)
- [What validate does](#what-validate-does)
- [new_implementation](#new_implementation)
- [existing_change](#existing_change)
- [bugfix](#bugfix)
- [test_improvement](#test_improvement)
- [doc_improvement](#doc_improvement)
- [spike](#spike)
- [observe](#observe)
- [Checking a diagram against the code](#checking-a-diagram-against-the-code)

## From a request to a graph

```mermaid
flowchart LR
    ask(["/foreman work on issue N<br/>the skill, in the product repo"]):::code --> pre(["foreman run N<br/>preflight, load .foreman/target.yaml"]):::code
    pre --> classify["classify<br/>classifier"]:::agent
    classify --> check(["code check against the repo"]):::code
    check -. "refused: the reason goes back to the classifier" .-> classify
    check -- "accepted" --> branch(["git branch foreman/RUN_ID<br/>not for observe"]):::code
    branch --> run_graph(["run the graph for that type<br/>the seven graphs below"]):::code
    run_graph --> out(["a pull request for you to merge<br/>observe: a comment on the issue"]):::code

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
```

## Inside every agent stage

This is `run_stage` in `foreman/core/runner.py`. The same loop runs for every blue rectangle in the diagrams below. A failed attempt resets the working tree, and the reason is fed into the next attempt's prompt.

```mermaid
flowchart TD
    entry(["entry gate<br/>is the required context there?"]):::code --> prompt(["build the prompt<br/>from the agent's listed context only"]):::code
    prompt --> worker["claude -p<br/>the agent's own model, tools and writable paths"]:::agent
    worker --> policy(["policy check on the git diff<br/>blocked paths and secrets"]):::code
    policy -- "clean" --> gate(["exit gate<br/>this stage's own checks"]):::code
    gate -- "passed" --> high{{"high-impact approval<br/>only for a dependency, migration,<br/>deleted test or CI/config change"}}:::human
    high -- "approved, or nothing to approve" --> commit(["keep the result<br/>tick tasks and commit where the stage does"]):::code
    policy -- "violation" --> fail(["failed: reset the working tree"]):::code
    gate -- "failed" --> fail
    high -- "rejected" --> fail
    fail -- "attempt 1 or 2 failed:<br/>try again with the reason" --> prompt
    fail -- "attempt 3 failed" --> stop(["safe stop"]):::stop

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef human fill:#fef3c7,stroke:#d97706,color:#111
    classDef stop fill:#fee2e2,stroke:#dc2626,color:#111
```

Before every call Foreman also checks the run's cost against its budget and stops if it is spent.

## What `validate` does

This is the check in new_implementation, existing_change and bugfix. test_improvement and doc_improvement run only the build, the tests and the policy check, with no review.

```mermaid
flowchart TD
    checks(["run every check<br/>build<br/>full test suite<br/>every task in tasks.md ticked<br/>openspec validate --strict<br/>policy over the whole branch"]):::code
    review["review<br/>reviewer<br/>OpenSpec's verify method"]:::agent
    fail(["count the failure"]):::code
    ok(["validated"]):::code
    again(["the code-writing stages run again<br/>with the failure as context"]):::code
    stop(["safe stop and roll back<br/>to the approved commit"]):::stop

    checks -- "all passed" --> review
    checks -- "something failed" --> fail
    review -- "no CRITICAL issue" --> ok
    review -- "a CRITICAL issue" --> fail
    fail -- "attempt 1 or 2" --> again
    fail -- "attempt 3" --> stop

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef stop fill:#fee2e2,stroke:#dc2626,color:#111
```

Foreman ticks a task in `tasks.md` only when the agent that did it ends its reply with `DONE: <task id>`. Foreman does not run a task's own verification step. A task nobody reports stays unchecked, and the validation fails with `tasks not reported done: 1.1 (Implementation), ...`, followed by each task's text. The comment on the issue shows what every failed check said (the end of a build or test output, the list of tasks) and the results of all checks, and the final stop repeats them: `validation failed 3 times: <what failed> (last checks: build: pass; tests: 7 passed; ...)`.

## new_implementation

a new feature in a repo that has no application code yet

```mermaid
flowchart TD
    intake(["intake"]):::code --> requirements["requirements<br/>requirements_analyst"]:::agent
    clarify{{"clarify<br/>you answer the blocking questions"}}:::human
    proposal["proposal<br/>architect<br/>writes the OpenSpec change"]:::agent
    approve{{"approve_proposal<br/>you approve the change"}}:::human

    subgraph parallel["implement, tests and docs run in parallel"]
        direction LR
        implement["implement<br/>implementer"]:::agent
        tests["tests<br/>test_writer"]:::agent
        docs["docs<br/>doc_writer"]:::agent
    end

    validate(["validate<br/>see above"]):::code
    archive_spec(["archive_spec<br/>openspec archive"]):::code
    pr_summary["pr_summary<br/>pr_summarizer"]:::agent
    open_pr(["open_pr<br/>push, open the pull request"]):::code
    await_merge{{"await_merge<br/>you merge the PR on GitHub"}}:::human

    requirements -. "it has blocking questions" .-> clarify
    requirements -. "it has none" .-> proposal
    clarify --> proposal
    proposal --> approve
    approve -. "rejected: back to proposal with your note<br/>a third rejection stops the run" .-> proposal
    approve -. "approved" .-> parallel
    parallel --> validate
    validate -. "failed: all three run again" .-> parallel
    validate -. "passed" .-> archive_spec
    archive_spec --> pr_summary --> open_pr --> await_merge

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef human fill:#fef3c7,stroke:#d97706,color:#111
```

## existing_change

a feature in a repo that already has code. It is the new_implementation graph with two steps in front.

```mermaid
flowchart TD
    intake(["intake"]):::code
    baseline["baseline<br/>baseliner<br/>records what the code does today"]:::agent
    archive_baseline(["archive_baseline<br/>openspec archive"]):::code
    impact["impact<br/>impact_analyst<br/>files, modules, APIs, data flows"]:::agent
    rest(["requirements, clarify, proposal, approve_proposal,<br/>implement + tests + docs, validate, archive_spec,<br/>pr_summary, open_pr, await_merge<br/>exactly as in new_implementation"]):::same

    intake -. "the product has no specs yet" .-> baseline
    intake -. "it has specs" .-> impact
    baseline --> archive_baseline --> impact
    impact --> rest

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef same fill:#fff,stroke:#6b7280,stroke-dasharray:5 3,color:#111
```

## bugfix

something that worked is broken

```mermaid
flowchart TD
    intake(["intake<br/>records the test baseline, reads pasted logs"]):::code
    baseline["baseline<br/>baseliner"]:::agent
    archive_baseline(["archive_baseline"]):::code
    triage["triage<br/>triager<br/>an OpenSpec change that modifies the broken requirement"]:::agent
    approve_proposal{{"approve_proposal<br/>you approve the triage"}}:::human
    reproduce["reproduce<br/>reproducer<br/>the new test must fail"]:::agent
    fix["fix<br/>fixer<br/>cannot edit tests"]:::agent
    validate(["validate<br/>see above"]):::code
    rca["rca<br/>rca_writer<br/>docs/rca/N.md"]:::agent
    archive_spec(["archive_spec"]):::code
    pr_summary["pr_summary<br/>pr_summarizer"]:::agent
    open_pr(["open_pr"]):::code
    await_merge{{"await_merge<br/>you merge the PR on GitHub"}}:::human

    intake -. "the product has no specs yet" .-> baseline
    intake -. "it has specs" .-> triage
    baseline --> archive_baseline --> triage
    triage --> approve_proposal
    approve_proposal -. "rejected" .-> triage
    approve_proposal -. "approved" .-> reproduce
    reproduce --> fix --> validate
    validate -. "failed" .-> fix
    validate -. "passed" .-> rca
    rca --> archive_spec --> pr_summary --> open_pr --> await_merge

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef human fill:#fef3c7,stroke:#d97706,color:#111
```

## test_improvement

add or strengthen tests, no code changes

```mermaid
flowchart TD
    intake(["intake<br/>records the test baseline"]):::code --> improve_tests["improve_tests<br/>test_improver<br/>writes only under the tests path"]:::agent
    improve_tests --> validate(["validate<br/>build, more tests than before, policy"]):::code
    validate -. "failed" .-> improve_tests
    validate -. "passed" .-> pr_summary["pr_summary<br/>pr_summarizer"]:::agent
    pr_summary --> open_pr(["open_pr"]):::code --> await_merge{{"await_merge<br/>you merge the PR on GitHub"}}:::human

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef human fill:#fef3c7,stroke:#d97706,color:#111
```

## doc_improvement

write or fix documentation

```mermaid
flowchart TD
    intake(["intake"]):::code --> improve_docs["improve_docs<br/>doc_improver<br/>writes only under the docs path and README.md"]:::agent
    improve_docs --> validate(["validate<br/>build, tests, policy<br/>and every relative link resolves"]):::code
    validate -. "failed" .-> improve_docs
    validate -. "passed" .-> pr_summary["pr_summary<br/>pr_summarizer"]:::agent
    pr_summary --> open_pr(["open_pr"]):::code --> await_merge{{"await_merge<br/>you merge the PR on GitHub"}}:::human

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef human fill:#fef3c7,stroke:#d97706,color:#111
```

## spike

the requirement is ambiguous, so explore before building

```mermaid
flowchart TD
    intake(["intake<br/>the issue's Questions section becomes the blocking questions"]):::code
    clarify{{"clarify<br/>you answer the questions"}}:::human
    frame["frame<br/>framer<br/>1 to 3 options"]:::agent

    subgraph options["in parallel, one per option"]
        explore["explore<br/>explorer<br/>writes notes under spikes/"]:::agent
    end

    analysis["analysis<br/>analyst<br/>trade-off matrix, recommendation, draft user story"]:::agent
    prune_scratch(["prune_scratch<br/>keep a copy of the notes, drop them from the branch"]):::code
    open_pr(["open_pr<br/>only the analysis document is in the PR"]):::code
    await_merge{{"await_merge<br/>you merge the PR on GitHub"}}:::human
    handoff(["handoff<br/>file the draft user story as a new issue<br/>labelled foreman:from-spike"]):::code

    intake --> clarify --> frame
    frame -. "one explorer per option" .-> explore
    explore --> analysis --> prune_scratch --> open_pr --> await_merge --> handoff

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
    classDef human fill:#fef3c7,stroke:#d97706,color:#111
```

## observe

"it is deployed, check the logs". No branch and no pull request.

```mermaid
flowchart TD
    gather(["gather<br/>read the issue and the observability notes the implementer left"]):::code
    locate_logs(["locate_logs<br/>a path in your message, a saved location or the notes<br/>asks you if none works, twice at most"]):::code
    read_logs(["read_logs<br/>the last 300 lines of the newest file"]):::code
    observe["observe<br/>observer<br/>sees only the log excerpt, never the source"]:::agent
    report(["report<br/>comment on the issue: verdict and quoted evidence<br/>code checks every quoted line is in the log"]):::code

    gather --> locate_logs --> read_logs --> observe --> report

    classDef agent fill:#dbeafe,stroke:#2563eb,color:#111
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111
```

## Checking a diagram against the code

After changing a graph, print the compiled graph and compare it with the diagram (this one is `bugfix`):

```bash
uv run python -c "from unittest.mock import MagicMock; from foreman.workflows import GRAPHS; print(GRAPHS['bugfix'](MagicMock()).compile().get_graph().draw_mermaid())"
```
