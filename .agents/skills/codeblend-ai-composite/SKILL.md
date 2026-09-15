---
name: codeblend-ai-composite
description: Run or explain the complete AI-readiness evaluation for a Git repository with the bundled ai-readiness-eval CLI. Use for combined readiness, composite score, substrate and operation, full AI-readiness, two-axis readiness, requests to evaluate whether a repository is AI-ready, or questions about how the rubric, weights, tiers, consensus, and final score work.
---

# Combined AI-Readiness

Run the bundled evaluator for the host operating system once. The executable
owns repository inspection, evidence collection, Copilot judge consensus,
scoring, conservative execution-aware documentation drift analysis, report
generation, and CSV export; do not reproduce those stages manually.

For questions about how the evaluation is designed or how scores are
calculated, read and cite [`how-it-work.md`](./how-it-work.md). Do not run an
evaluation merely to explain the methodology.

## Workflow

1. Determine the target directory:
   - Use the directory supplied by the user when one is provided.
   - Otherwise use the user's current working directory.
   - Resolve it to the repository root with
     `git -C <target> rev-parse --show-toplevel`.
   - Stop with the Git error if the target is not inside a Git repository.

2. Detect the host operating system and architecture, then resolve the matching
   executable beside this `SKILL.md`:
   - On Windows amd64, use `ai-readiness-eval.exe`.
   - On Linux amd64 (including Ubuntu x86_64), use
     `ai-readiness-eval-linux-amd64`.
   - In an installed plugin, resolve the selected filename under
     `${CLAUDE_PLUGIN_ROOT}/skills/codeblend-ai-composite/`.
   - In the source lab, resolve it in this skill directory.
   - On Linux, run `chmod +x` on the selected executable before invoking it in
     case the plugin installation did not preserve executable permissions.
   - Stop clearly on macOS, Linux ARM64, or another unsupported platform; do
     not try to run the Windows executable through Wine.
   - Stop clearly if the executable is missing; do not fall back to the legacy
     Python composite pipeline or scanner.

   On Windows, require
   `[Runtime.InteropServices.RuntimeInformation]::OSArchitecture` to be `X64`.
   On Linux, require `uname -m` to return `x86_64` or `amd64`. Use the actual
   host result rather than inferring the platform from repository paths or the
   user's shell syntax.

3. Run the evaluation against the resolved repository root:

   Windows PowerShell:

   ```powershell
   & $Evaluator eval $RepoRoot
   ```

   Linux shell:

   ```bash
   "$evaluator" eval "$repo_root"
   ```

   Keep the user's current working directory unchanged. Pass through any
   evaluation flags the user explicitly supplied, such as `--models`,
   `--max-rounds`, `--allow-missing-api-evidence`, `--full-findings`, or
   `--no-cache`.

4. Read the run directory printed by the CLI, then present its `composite.md`.
   Lead with the headline score, AI-ready verdict, substrate level, operation
   tier, highest-priority remediation items, and documentation drift coverage
   and residual action. Treat semantic review as advisory and automatic repair
   as a backstop; only a deterministic repository-wide blocking PR gate is
   complete coverage. Surface the continuous-cleanup automation rating when
   reported; the evaluator refines generic scheduled cleanup guidance only when
   executable cleanup capability is independently
   proven. Also report the run directory so the user can access the JSON and
   CSV artifacts.

## Runtime requirements

- `git` and GitHub Copilot CLI must be available on `PATH`.
- GitHub repositories require an authenticated `gh` session for API evidence.
- Azure DevOps repositories require an authenticated `az` session.
- Do not add `--allow-missing-api-evidence` unless the user explicitly accepts
  a degraded evaluation.

## Feedback

Report skill issues at
https://github.com/gim-home/codeblend/issues.
