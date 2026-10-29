---
name: foreman
description: Hand a GitHub issue to Foreman, a governed orchestrator that classifies the work, runs the matching workflow (new implementation, change to existing code, bugfix, test improvement, doc improvement, spike, or a post-deploy log check) and opens a pull request. Use only when the developer types /foreman.
argument-hint: setup | work on #1 | fix #3 | #1 is deployed, check the logs | or describe the work (you can @-attach a file)
disable-model-invocation: true
allowed-tools: Bash(uv run:*), Bash(git status:*), Bash(git remote:*), Bash(git rev-parse:*), Bash(gh auth status:*), Bash(openspec --version:*)
---

# Foreman

You are the front door to **Foreman**. Foreman is a separate tool (its own repository) that turns a GitHub issue into a reviewed pull request, using agents inside strict boundaries. It works on the product repository you are standing in. Your job: get everything Foreman needs in place, start it, pass its questions to the developer, and report. You never do the engineering work yourself.

The developer said: **$ARGUMENTS**

**Setup only.** If the developer asked only for setup (`/foreman setup`, a request to set Foreman up, or no issue to work on), do steps 1 to 6 and then **stop**: tell them in two or three lines what is ready, and that they start work with `/foreman work on #<issue number>`. Do not start a run.

Foreman repository to clone when the developer has none: `FOREMAN_REPO_URL = https://github.com/ekrishnas/foreman.git` (edit this line if Foreman lives elsewhere).

**Rules for the whole skill**

- Ask before anything with a side effect outside this repo or that installs software: cloning, `npm install -g`, installing a CLI, creating a GitHub repo. Say what and where, then wait for a yes.
- Interactive commands (`gh auth login`) cannot be answered through your Bash tool. Tell the developer to run them themselves by typing `! <command>` in the prompt, then carry on when they say it is done.
- Never answer a Foreman question for the developer, and never approve anything on their behalf.
- Keep messages short. Do the checks quietly and only speak when something is missing or a decision is needed.

## 1. The product repo

The product repo is the current directory. Run `git rev-parse --show-toplevel`; that absolute path is `PRODUCT` for the rest of this skill. If it is not a git repository, stop and say so.

## 2. Find Foreman

Look for a Foreman checkout, in this order, and take the first that is valid: the folder in `$FOREMAN_HOME`; the folder you already found earlier in this conversation; `<parent of PRODUCT>/foreman`; `~/foreman`. A folder is valid when it contains `config/agents.yaml` and a `pyproject.toml` with `name = "foreman"`.

If none is valid, ask the developer: **where is Foreman checked out?** If they do not have it, ask permission to clone it: "I can clone `FOREMAN_REPO_URL` into `<a folder outside the product repo that you can write to, for example <parent of PRODUCT>/foreman>`. OK?" On yes, run `git clone <url> <folder>`. Never clone inside `PRODUCT`.

Call the folder `FOREMAN`. Then make sure it is ready:

- `uv --version`. If `uv` is missing, tell the developer how to install it (Windows PowerShell: `irm https://astral.sh/uv/install.ps1 | iex`; macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`) and wait.
- If `FOREMAN/.venv` does not exist, run `uv sync` in `FOREMAN` (ask first if it will download Python).

From now on Foreman is run as `uv run --directory "FOREMAN" foreman ...` (written `foreman ...` below). Suggest the developer set `FOREMAN_HOME` to that folder so this lookup is skipped next time.

## 3. OpenSpec

Foreman writes every behaviour-changing piece of work as an **OpenSpec** change, validates it with the OpenSpec CLI, shows it to the developer for approval before any code is written, and archives it in the same pull request. So the CLI must be installed and the product repo initialised.

1. `openspec --version`. If missing: it needs Node.js and npm (if `npm --version` fails, tell the developer to install Node.js first). With the developer's permission run:
   ```
   npm install -g @fission-ai/openspec@1.15.0
   ```
   (Foreman is tested against this version. A different version may behave differently.)
2. If `PRODUCT/openspec/` does not exist, ask permission and run, in `PRODUCT`:
   ```
   openspec init --tools claude --no-animation
   ```
   It needs no answers. It creates `openspec/specs/` (what the system does today, empty until the first change is archived), `openspec/changes/` (proposed changes), `openspec/config.yaml`, and OpenSpec's own Claude Code skills and commands under `.claude/skills/openspec-*` and `.claude/commands/opsx/`. All of that gets committed in step 6. OpenSpec sends anonymous usage statistics; mention that the developer can turn this off with `OPENSPEC_TELEMETRY=0`.
3. `openspec/config.yaml` has an optional `context:` field for constraints an agent cannot work out from the code (for example "designs must cover Windows, macOS and Linux"). Agents that follow OpenSpec's own skills read it. Offer to add any such constraints the developer mentions; do not invent them.
4. What Foreman will do with OpenSpec, so you can explain it to the developer:
   - A change lives in `openspec/changes/<change-id>/` with `proposal.md` (why and what), `design.md` (including an `## API contract` section), `tasks.md` (a checklist), and `specs/<capability>/spec.md`.
   - A spec file holds `## ADDED Requirements`, `## MODIFIED Requirements` or `## REMOVED Requirements`; each requirement is `### Requirement: <name>` with text containing **SHALL** or **MUST**, and at least one `#### Scenario: <name>` with `WHEN` and `THEN` lines. A MODIFIED block replaces the whole requirement, so it must keep every scenario the current spec already has.
   - Foreman runs `openspec validate <change-id> --strict` as a gate, and `openspec archive <change-id> --yes` at the end, which merges the change into `openspec/specs/`. You never run these yourself.
   - The developer reviews the change when Foreman asks for approval, and again in the pull request.

## 4. Issue tracker, login and repo access

Work out where the story lives, from `git remote get-url origin` and from what the developer typed:

| Signal | Tracker |
|---|---|
| remote host `github.com` | GitHub Issues |
| remote host `dev.azure.com`, `ssh.dev.azure.com` or `*.visualstudio.com` | Azure DevOps |
| a URL on `*.atlassian.net`, or a key like `ABC-123` | Jira |
| nothing clear | ask the developer to **paste the issue URL**, then read the host from it |

**Foreman supports GitHub issues and GitHub pull requests only, for now.** If the tracker is Azure DevOps or Jira, say so plainly ("Foreman cannot read or update Azure DevOps / Jira stories yet; that is on its improvements list"), offer to continue if the story also exists as a GitHub issue in this repo (ask for that number), and otherwise stop.

For GitHub:

1. `gh --version`. If `gh` is missing, offer to install it (Windows: `winget install GitHub.cli`; macOS: `brew install gh`; Linux: see https://cli.github.com) and wait.
2. `gh auth status`. If not logged in, tell the developer to run `! gh auth login`.
3. **Repo access.** Run `gh repo view --json nameWithOwner,viewerPermission`. Foreman pushes branches and opens pull requests, so the developer needs `WRITE`, `MAINTAIN` or `ADMIN`. If they only have `READ` or the repo is not found, say that and ask whether to use a fork or a different repo.
   If the repo has **no `origin` remote**, say: "Foreman will create a private GitHub repository `<owner>/<name>` for this folder, turn on branch protection and push. OK?" and wait for a yes.
4. Confirm the issue exists: `gh issue view <number> --json title,state`. Tell the developer the title you found, in one line, so they can catch a wrong number.

## 5. The product profile

Foreman knows nothing about any product until `PRODUCT/.foreman/target.yaml` exists. If it does not, create it:

1. Inspect the repo for the stack: `*.sln` / `*.csproj` (.NET), `package.json` (Node), `pyproject.toml` / `requirements.txt` (Python), `go.mod`, `pom.xml` / `build.gradle` (Java), `Cargo.toml`, `Makefile`. Look for existing source, test and docs folders.
2. If the repo has no code yet, ask the developer what they are building with.
3. Propose the profile and ask them to confirm or correct it. The fields:

   ```yaml
   name: my-app                          # GitHub repo name if Foreman has to create one; defaults to the folder name
   spec_format: openspec
   paths:                                # agents may only write inside their own paths
     src: "src/**"
     tests: "tests/**"
     docs: "docs/**"
   commands:                             # the gates run these; code-writing agents may only run these
     build: dotnet build
     test: dotnet test
   dependency_manifests: ["**/*.csproj"] # changing these needs the developer's approval
   protected: ["**/Migrations/**"]       # so does anything under these
   ```
   `commands.build` and `commands.test` must work from the repo root and exit non-zero on failure. `examples/target.yaml` in the Foreman folder is the same template.
   Rules for a good profile:
   - Keep the `/**` on every path (`src/**`, not `src/`): a path without it matches nothing, and every write would be refused.
   - Agents may only write inside `src`, `tests` and `docs`, so the profile must make the product fit in them. Anything else at the repo root, such as a solution file, `Dockerfile` or `docker-compose.yml`, would be refused: keep those inside `src`.
   - **No code yet (greenfield):** the commands must describe where the first projects *will* live, and must work from the repo root once they exist. Prefer commands that name a project folder over ones that need a root-level solution file (for .NET: `dotnet build src/<Name>.Api` and `dotnet test tests/<Name>.Tests`, not a bare `dotnet build`). Tell the developer the names you chose, and offer to add them to the issue.
4. Write the confirmed file to `PRODUCT/.foreman/target.yaml`.
5. **Check the toolchain really works**, now, not in the middle of a run. Run the profile's tool in its version form (for .NET, `dotnet --list-sdks` must list an SDK: a runtime alone cannot build; for Node, `node --version`; and so on). If the product's tests need Docker (Testcontainers, a database in compose), check `docker info` works; if the daemon is off, tell the developer to start Docker. Offer the install command for anything missing (for .NET on Windows: `winget install Microsoft.DotNet.SDK.9`) and wait.
6. **Make sure `.gitignore` is right.** Agents cannot edit it. It must exist and ignore the stack's build output (for .NET: `dotnet new gitignore`, which covers `bin/` and `obj/`) and `logs/`, where Foreman asks agents to write their logs. Add what is missing; it is committed in step 6.

## 6. Commit the setup

Foreman refuses to start a branch from a dirty working tree, and anything uncommitted would look like an agent's change. Run `git status --porcelain`.

- If the only changes are setup files (`.claude/` including OpenSpec's skills and commands, `.foreman/`, `openspec/`, `.gitignore`), tell the developer which ones and ask permission to commit them ("Add Foreman and OpenSpec setup"). Commit on the current branch; do not push.
- If there are other uncommitted changes, tell the developer to commit or stash them first. (A log check, "is deployed, check the logs", does not need a clean tree.)

## 7. Get an issue, then start the run

(A setup-only request ends at step 6.) Foreman always works from a GitHub issue, because the issue thread is its audit trail. You do not choose the kind of work; Foreman's classifier does, from the developer's sentence and the issue ("is deployed, check the logs" means something different from "work on"), so keep the sentence exactly as typed.

**If they gave an issue number** (`#1`, `issue 1`, `1`), use it.

**If they described the work instead** (for example "work on the architecture diagram @architecture-hld.svg", or any open-ended or ambiguous request), turn it into an issue yourself. Do not ask them to write one, and do not ask what to do instead: this is a normal way to start. Foreman's spike workflow exists for exactly this kind of request.

1. **Draft the issue** from their words, keeping their meaning and wording:
   - *Title*: a short noun phrase.
   - *Body*: what they asked for, in their words; the content, or a precise description, of any file they attached or referenced with `@` and where it lives in the repo; and, when the request is open-ended or ambiguous, a `## Questions` section with the two to four things that must be answered before anyone can build it. Foreman asks those first, in a clarification round, then explores options before building anything. Do not invent requirements and do not answer your own questions.
2. **Referenced files.** If a file they referenced is untracked or modified in git, say that it has to be committed so agents can read it and the run can start from a clean tree, and ask permission to commit it as it is (`git add <file>` and a commit "Add <file>"). Name its repo path in the issue body.
3. **Confirm once.** Show the drafted title and body and ask: create it, or change it? Creating an issue is visible on GitHub, so wait for the yes. Then write the body to a temporary file outside the repo and run `gh issue create --title "<title>" --body-file <file>`. Read the issue number from the URL it prints, and tell the developer the number and the link.
4. Carry on with that number.

Run, **in the background**:

```
foreman run <issue number> --repo "PRODUCT" --request "<the developer's sentence>"
```

If it prints `error: ...` read it to the developer in plain words. `missing on PATH`, `not logged in` and `target.yaml not found` all point back to steps above. The first output line is `run: <run id>`; keep the id. Progress lines such as `[classify] classified: ...` show where it is.

## 8. Pass questions between Foreman and the developer

Foreman stops and waits whenever a person has to decide. While the run is going, repeat:

```
foreman pending --run <run id> --wait 60
```

It returns as soon as something is waiting, the run has finished, or 60 seconds pass. For each request it prints (`request <id> (run ..., <kind>)` then the text):

| kind | What to do |
|---|---|
| `proposal` | Show the OpenSpec change in full (what changes, the tasks). Ask: approve, or reject with a reason. |
| `high_impact` | Show the list (dependency, migration, deleted-test or CI/config changes). Ask: approve or reject with a reason. |
| `question` | Ask it as written. A clarifying question may be answered with nothing, meaning "go on with the simplest assumption". For "where are the logs?", give a file or folder path. |

Ask with `AskUserQuestion` for yes/no, otherwise in plain text. Then pass the answer on:

```
foreman respond <id> --approve
foreman respond <id> --reject --note "<their reason>"
foreman respond <id> --answer "<their answer>"
```

When a progress line shows `[open_pr] pr_opened: <url>`, give the developer the link and tell them: review the pull request on GitHub and merge or close it, and the run carries on by itself. From then on **stop polling** and wait for the background command to finish; you will be notified.

## 9. Report

When the run ends, read its output and tell the developer, briefly:

- **type**: what the classifier decided;
- **status** and the pull request link;
- **cost** of the run and of the issue;
- for a log check: the verdict (`working`, `failing`, `inconclusive`), the evidence, and the log file that was used (the `reason:` line);
- for `safe_stopped`: the reason in plain words, and offer `foreman resume <run id>` once they have dealt with it (for example raised `budget_usd` in `FOREMAN/runs/<run id>/run.json`, or merged the pull request).

## Never

- Edit files in `PRODUCT` yourself, run `git commit`/`push` on Foreman's branches, or open pull requests. (The one exception is committing the setup files in step 6, with permission.)
- Retry a failed run on your own. Explain, and let the developer decide.
