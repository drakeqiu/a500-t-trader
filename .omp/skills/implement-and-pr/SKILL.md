
---
name: implement-and-pr
description: Implement a ticket using Matt Pocock engineering skills, run quality gates, make a conventional commit, push and open a draft GitHub PR.
---

# Implement and PR

You are an implementation-to-GitHub-delivery orchestrator.

A user invoking this skill authorizes:
- Creation of a feature branch
- Implementation of the requested work
- Task-related commits
- Push to the configured GitHub origin
- Creation of a draft pull request

The user does NOT authorize force pushes, destructive history
rewrites, bypassing tests, or automatic PR merges.

## Prerequisites

- Run from the Git repository root.
- Require git, gh and bash.
- Verify GitHub authentication.
- Require all relevant Matt Pocock skills to be installed.
- Never infer that a skill was successfully invoked unless
  its instructions were actually loaded and followed.

## Phase 1: Prepare

1. Read AGENTS.md and repository conventions.
2. Resolve the user's ticket or specification.
3. Determine a short kebab-case branch slug.
4. Run:

   bash scripts/agent-ship.sh prepare <slug>

5. Stop if preparation fails.

For ticket #123, a suitable slug is `123-short-description`.

## Phase 2: Implement

Read and follow the installed `implement` skill from
`skill://implement/SKILL.md`.

Follow its TDD, typechecking, testing, code-review and
commit instructions.

Load `tdd` and `code-review` as required.

Do not merely say that the Matt Pocock workflow was used.
Perform the actual implementation and checks.

The implementation skill may already create a commit.
That is expected.

## Phase 3: Review

Before publication:

1. Confirm all acceptance criteria are satisfied.
2. Complete the Matt Pocock code-review workflow.
3. Fix all blocking findings.
4. Check that no unrelated files are being shipped.
5. Review the diff for credentials and secrets.
6. Ensure any uncommitted task changes are explicitly staged.

Never run `git add -A` or `git add .`.

If a pre-existing change cannot be attributed to this task,
stop rather than including it.

## Phase 4: Prepare GitHub metadata

Read `skill://pr/SKILL.md` and follow its PR guidance.

If git-authoring is installed, use its commit and PR
authoring conventions.

Create three temporary files OUTSIDE the repository:

1. commit-message.txt
   A Conventional Commit message, used only if staged changes
   remain after the Matt Pocock implementation workflow.

2. pr-title.txt
   A short and accurate PR title.

3. pr-body.md
   A PR body containing:
   - Summary
   - Evidence / tests
   - Merge Danger and blast radius
   - Issue references where applicable

Do not claim tests passed unless actually executed.

## Phase 5: Publish

Run the deterministic delivery script:

bash scripts/agent-ship.sh publish \
  /absolute/path/commit-message.txt \
  /absolute/path/pr-title.txt \
  /absolute/path/pr-body.md

The script is responsible for:
- Running mandatory project checks
- Committing remaining staged changes
- Refusing dirty working trees
- Checking for changes since branch creation
- Rejecting protected branches and suspicious files
- Pushing without force
- Creating a draft PR if none exists

If a PR already exists, reuse it. Do not create duplicates.

If any operation fails, STOP and report the failure.
Do not silently skip quality gates or repair Git history.

## Phase 6: Report

Return:

- Implementation summary
- Tests run and results
- Code-review findings and resolution
- Branch name
- Commit SHA
- PR URL
- Current CI state, if available
- Any outstanding risks

Never automatically merge the PR.
