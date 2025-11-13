from __future__ import annotations

import inspect
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional

import typer

from .utils import (
    DEFAULT_FORMAT,
    DEFAULT_OUTPUT,
    PROMPTS_DIR,
    RESERVED_PROMPTS,
    TEMPLATE_SUFFIX,
    SUPPORTED_FORMATS,
    infer_param_type,
    load_prompt_metadata,
    parse_issue_reference,
    parse_pr_reference,
    render_prompt_template,
)


app = typer.Typer()
review_app = typer.Typer(
    help="Render a GitHub pull request review prompt.", invoke_without_command=True
)


@review_app.callback()
def review(
    pr: str = typer.Option(
        ..., "--pr", help="PR number, owner/repo#number, or GitHub URL to review."
    ),
    repo: str = typer.Option(
        "Ficks-Music/ficks",
        "--repo",
        help="Repository used when --pr is a bare number.",
    ),
    to: str = typer.Option(
        DEFAULT_FORMAT,
        "--to",
        help="Output format for the rendered prompt (default: commonmark).",
    ),
    output: str = typer.Option(
        DEFAULT_OUTPUT,
        "--output",
        help="Where the rendered prompt is written ('-' streams to stdout).",
    ),
    issue: str | None = typer.Option(
        None,
        "--issue",
        help="Issue number, owner/repo#number, or GitHub URL to include in the prompt.",
    ),
) -> None:
    """Render the review prompt with the selected pull request."""

    pr_ref = parse_pr_reference(pr, repo)

    if to not in SUPPORTED_FORMATS:
        typer.secho(
            f"Unsupported format '{to}'. Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    owner_part, repo_part = (pr_ref.repo.split("/", 1) + [""])[:2]
    if not repo_part:
        repo_part = owner_part

    params = {
        "pr_number": pr_ref.number,
        "pr_url": pr_ref.url,
        "pr_overview": _run_text_command(
            ["gh", "pr", "view", pr_ref.url],
            "retrieving pull request details",
        ),
        "pr_comments": _run_text_command(
            ["gh", "pr", "view", pr_ref.url, "--comments"],
            "retrieving pull request discussion",
        ),
        "user": owner_part,
        "repo": repo_part,
        "git_log": _collect_git_log(),
    }

    if issue:
        issue_ref = parse_issue_reference(issue, repo)
        issue_owner_part, issue_repo_part = (
            issue_ref.repo.split("/", 1) + [""]
        )[:2]
        if not issue_repo_part:
            issue_repo_part = issue_owner_part

        params["issue"] = {
            "number": issue_ref.number,
            "url": issue_ref.url,
            "user": issue_owner_part,
            "repo": issue_repo_part,
            "overview": _run_text_command(
                ["gh", "issue", "view", issue_ref.url],
                "retrieving issue details",
            ),
            "comments": _run_text_command(
                ["gh", "issue", "view", issue_ref.url, "--comments"],
                "retrieving issue discussion",
            ),
        }
    else:
        params["issue"] = None

    render_prompt_template(
        "review.md.jinja",
        params=params,
        fmt=to,
        output=output,
    )


app.add_typer(review_app, name="review")


def register_prompt_command(
    parent: typer.Typer,
    command_name: str,
    file_path: Path,
    metadata: Dict[str, Any],
) -> None:
    """Register a Typer command for a prompt file."""

    params: Dict[str, Dict[str, Any]] = metadata.get("params", {})
    description = metadata.get("description")

    option_aliases: Dict[str, str] = {}

    def prompt_cmd(**kwargs: Any) -> None:
        normalized_kwargs = {
            option_aliases.get(key, key): value for key, value in kwargs.items()
        }
        relative_path = file_path.relative_to(PROMPTS_DIR)
        render_prompt_template(
            relative_path,
            params=normalized_kwargs,
            fmt=DEFAULT_FORMAT,
            output=DEFAULT_OUTPUT,
        )

    prompt_cmd.__name__ = f"{command_name}_command"
    prompt_cmd.__doc__ = description

    signature_parameters = []

    for original_name, param_info in params.items():
        python_name = original_name.replace("-", "_")
        option_aliases[python_name] = original_name

        default_value = param_info.get("default")
        help_text = param_info.get("help")
        annotation = infer_param_type(default_value)

        option_kwargs: Dict[str, Any] = {}
        if help_text:
            option_kwargs["help"] = help_text

        is_required = (
            annotation is not bool
            and (default_value is None or default_value == "" or default_value == [])
        )

        if is_required:
            option_default = typer.Option(..., **option_kwargs)
        elif annotation is bool:
            option_default = typer.Option(bool(default_value), **option_kwargs)
        else:
            option_kwargs.setdefault("show_default", True)
            option_default = typer.Option(default_value, **option_kwargs)

        parameter = inspect.Parameter(
            name=python_name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=option_default,
            annotation=annotation,
        )
        signature_parameters.append(parameter)

    prompt_cmd.__signature__ = inspect.Signature(signature_parameters)
    parent.command(name=command_name, help=description)(prompt_cmd)


def build_cli(parent: typer.Typer, directory: Path) -> bool:
    """Recursively register Typer commands mirroring the prompt folder structure."""

    if not directory.exists():
        return False

    has_entries = False
    for path in sorted(directory.iterdir()):
        if path.name.startswith("."):
            continue

        if path.is_dir():
            sub_app = typer.Typer()
            if build_cli(sub_app, path):
                parent.add_typer(sub_app, name=path.name)
                has_entries = True
        elif path.is_file() and path.name.endswith(TEMPLATE_SUFFIX):
            command_name = path.name[: -len(TEMPLATE_SUFFIX)]
            if command_name in RESERVED_PROMPTS:
                continue
            metadata = load_prompt_metadata(path)
            register_prompt_command(parent, command_name, path, metadata)
            has_entries = True

    return has_entries


build_cli(app, PROMPTS_DIR)


def _run_text_command(
    command: List[str],
    description: str,
    *,
    check: bool = True,
) -> str:
    """Run a CLI command, returning its stdout or exiting with a helpful message."""

    try:
        completed = subprocess.run(
            command,
            check=check,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        typer.secho(
            f"Required command '{command[0]}' not found while {description}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError as exc:
        _report_command_failure(command, description, exc.returncode, exc.stdout, exc.stderr)
        raise typer.Exit(code=exc.returncode)

    if check and completed.returncode != 0:
        _report_command_failure(
            command, description, completed.returncode, completed.stdout, completed.stderr
        )
        raise typer.Exit(code=completed.returncode)

    return (completed.stdout or "").rstrip()


def _report_command_failure(
    command: List[str],
    description: str,
    returncode: int,
    stdout: Optional[str],
    stderr: Optional[str],
) -> None:
    typer.secho(
        f"Command '{' '.join(command)}' failed while {description} (exit code {returncode}).",
        fg=typer.colors.RED,
        err=True,
    )
    output = (stderr or "").strip() or (stdout or "").strip()
    if output:
        typer.echo(output, err=True)


def _collect_git_log() -> str:
    base_branch = _determine_base_branch()
    candidates: List[List[str]] = [["git", "log", "--oneline"]]

    if base_branch:
        candidates.insert(0, ["git", "log", "--oneline", f"{base_branch}..HEAD"])
        candidates.insert(1, ["git", "log", "--oneline", f"origin/{base_branch}..HEAD"])

    for command in candidates:
        try:
            return _run_text_command(command, "collecting git history")
        except typer.Exit as exc:
            exit_code = getattr(exc, "exit_code", None)
            if command is candidates[-1] or exit_code != 128:
                raise
            continue

    return ""


def _determine_base_branch() -> Optional[str]:
    branch = _extract_symbolic_origin_head()
    if branch:
        return branch

    branch = _extract_origin_head_from_remote()
    if branch:
        return branch

    for candidate in ("main", "master", "trunk", "develop"):
        if _git_ref_exists(candidate) or _git_ref_exists(f"origin/{candidate}"):
            return candidate

    return None


def _extract_symbolic_origin_head() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        typer.secho(
            "Unable to locate 'git'. Install Git to continue.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if result.returncode == 0:
        value = (result.stdout or "").strip()
        if value.startswith("origin/"):
            value = value.split("/", 1)[1]
        return value or None

    return None


def _extract_origin_head_from_remote() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "remote", "show", "origin"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        typer.secho(
            "Unable to locate 'git'. Install Git to continue.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    if result.returncode != 0:
        return None

    for line in (result.stdout or "").splitlines():
        if "HEAD branch:" in line:
            branch = line.split(":", 1)[1].strip()
            return branch or None

    return None


def _git_ref_exists(ref: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
            check=False,
        )
    except FileNotFoundError:
        typer.secho(
            "Unable to locate 'git'. Install Git to continue.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    if result.returncode == 0:
        return True

    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{ref}"],
        check=False,
    )
    return result.returncode == 0
@app.command("issue")
def issue(
    reference: str = typer.Argument(
        ...,
        help="Issue number, owner/repo#number, or GitHub URL to inspect.",
    ),
    repo: str = typer.Option(
        "Ficks-Music/ficks",
        "--repo",
        help="Repository used when --issue is a bare number.",
    ),
    to: str = typer.Option(
        DEFAULT_FORMAT,
        "--to",
        help="Output format for the rendered prompt (default: commonmark).",
    ),
    output: str = typer.Option(
        DEFAULT_OUTPUT,
        "--output",
        help="Where the rendered prompt is written ('-' streams to stdout).",
    ),
) -> None:
    """Render the issue prompt with the selected GitHub issue."""

    issue_ref = parse_issue_reference(reference, repo)

    if to not in SUPPORTED_FORMATS:
        typer.secho(
            f"Unsupported format '{to}'. Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    owner_part, repo_part = (issue_ref.repo.split("/", 1) + [""])[:2]
    if not repo_part:
        repo_part = owner_part

    params = {
        "issue_number": issue_ref.number,
        "issue_url": issue_ref.url,
        "user": owner_part,
        "repo": repo_part,
        "issue_overview": _run_text_command(
            ["gh", "issue", "view", issue_ref.url],
            "retrieving issue details",
        ),
        "issue_comments": _run_text_command(
            ["gh", "issue", "view", issue_ref.url, "--comments"],
            "retrieving issue discussion",
        ),
    }

    render_prompt_template(
        "issue.md.jinja",
        params=params,
        fmt=to,
        output=output,
    )


@app.command("pr")
def pr(
    reference: str = typer.Argument(
        ...,
        help="PR number, owner/repo#number, or GitHub URL to inspect.",
    ),
    repo: str = typer.Option(
        "Ficks-Music/ficks",
        "--repo",
        help="Repository used when PR reference is a bare number.",
    ),
    to: str = typer.Option(
        DEFAULT_FORMAT,
        "--to",
        help="Output format for the rendered prompt (default: commonmark).",
    ),
    output: str = typer.Option(
        DEFAULT_OUTPUT,
        "--output",
        help="Where the rendered prompt is written ('-' streams to stdout).",
    ),
) -> None:
    """Render the PR prompt with the selected GitHub pull request."""

    pr_ref = parse_pr_reference(reference, repo)

    if to not in SUPPORTED_FORMATS:
        typer.secho(
            f"Unsupported format '{to}'. Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    owner_part, repo_part = (pr_ref.repo.split("/", 1) + [""])[:2]
    if not repo_part:
        repo_part = owner_part

    params = {
        "pr_number": pr_ref.number,
        "pr_url": pr_ref.url,
        "user": owner_part,
        "repo": repo_part,
        "pr_overview": _run_text_command(
            ["gh", "pr", "view", pr_ref.url],
            "retrieving pull request details",
        ),
        "pr_comments": _run_text_command(
            ["gh", "pr", "view", pr_ref.url, "--comments"],
            "retrieving pull request discussion",
        ),
    }

    render_prompt_template(
        "pr.md.jinja",
        params=params,
        fmt=to,
        output=output,
    )
