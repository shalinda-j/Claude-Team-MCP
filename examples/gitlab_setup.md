# GitLab CI/CD setup

The repo ships with a ready-to-run GitLab pipeline (`.gitlab-ci.yml` at the
repo root): the full pytest suite on Python 3.10–3.13 in parallel, plus a
package job that builds the sdist/wheel and smoke-tests the `claude-team-mcp`
entry point. Artifacts (the built wheel) are kept for a week on every pipeline.

It covers the same ground as the GitHub Actions CI except for that workflow's
two `mcp` SDK compatibility legs (which pin `mcp<2` and `mcp>=2` separately)
and its `doctor` smoke step — those stay GitHub-only for now, so treat GitHub
as the source of truth for dependency-matrix coverage.

There are three ways to connect the project to GitLab — pick one.

---

## Option 1 — GitLab as a second remote (simplest)

Create an empty project on GitLab (no README), then push this repo to it:

```bash
git remote add gitlab https://gitlab.com/<your-user>/claude-team-mcp.git
git push -u gitlab main --tags
```

The pipeline runs automatically on the first push — GitLab picks up
`.gitlab-ci.yml` from the repo root with zero extra configuration.

To keep both remotes updated with one command, add a pushurl:

```bash
git remote set-url --add --push origin https://github.com/<your-user>/Claude-Team-MCP.git
git remote set-url --add --push origin https://gitlab.com/<your-user>/claude-team-mcp.git
git push   # now pushes to GitHub AND GitLab
```

## Option 2 — automatic mirroring from GitHub (recommended)

The repo includes `.github/workflows/mirror-gitlab.yml`: every push to `main`
on GitHub is force-pushed to your GitLab project, which then runs the GitLab
pipeline. It stays dormant until you configure two things in the GitHub repo
(**Settings → Secrets and variables → Actions**):

| Kind     | Name                | Value                                                    |
|----------|---------------------|----------------------------------------------------------|
| Variable | `GITLAB_MIRROR_URL` | `gitlab.com/<your-user>/claude-team-mcp.git` (no scheme) |
| Secret   | `GITLAB_TOKEN`      | A GitLab personal access token                           |

Create the token on GitLab under **User Settings → Access tokens** with the
`write_repository` scope (a project access token scoped to just the mirror
project also works, and is safer).

Once both are set, mirroring is fully automatic; you can also trigger it by
hand from the Actions tab (`workflow_dispatch`).

## Option 3 — GitLab pull mirroring (GitLab Premium)

If your GitLab tier has it: **Project → Settings → Repository → Mirroring
repositories → Pull** and point it at
`https://github.com/<your-user>/Claude-Team-MCP.git`. GitLab then polls GitHub
and runs the pipeline on every change. Free tier only offers *push* mirroring
(GitLab → elsewhere), which is the wrong direction here — use Option 2
instead.

---

## What the pipeline runs

| Job       | Stage | Image            | What it does                                          |
|-----------|-------|------------------|-------------------------------------------------------|
| `test`    | test  | python:3.10–3.13 | `pip install -e .[dev]` then `pytest -v` (4× parallel) |
| `package` | build | python:3.12      | `python -m build`, installs the wheel, checks `main()` |

Pipelines run for merge requests, pushes to the default branch, and manual
(web) runs. Pip downloads are cached between runs per job.

## Self-hosted GitLab / runners

Nothing in the pipeline assumes gitlab.com — any GitLab instance with Docker
runners (`python:*` images) works. On a self-hosted instance just change the
URLs above to your instance's host.
