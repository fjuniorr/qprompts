from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict

import typer

from .utils import (
    DEFAULT_FORMAT,
    DEFAULT_OUTPUT,
    PROMPTS_DIR,
    RESERVED_PROMPTS,
    infer_param_type,
    load_prompt_metadata,
    parse_pr_reference,
    render_prompt_template,
    render_quarto,
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
        help="Output format forwarded to Quarto (default: commonmark).",
    ),
    output: str = typer.Option(
        DEFAULT_OUTPUT,
        "--output",
        help="Where Quarto writes the result ('-' streams to stdout).",
    ),
) -> None:
    """Render the review prompt with the selected pull request."""

    pr_ref = parse_pr_reference(pr, repo)

    params = {
        "pr_number": pr_ref.number,
        "pr_url": pr_ref.url,
        "repo": pr_ref.repo,
    }

    render_prompt_template(
        "review.qmd",
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
        relative_path = file_path.relative_to(PROMPTS_DIR).as_posix()
        render_quarto(
            file_path,
            params=normalized_kwargs,
            fmt=DEFAULT_FORMAT,
            output=DEFAULT_OUTPUT,
            label=relative_path,
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
        elif path.suffix == ".qmd":
            if path.stem in RESERVED_PROMPTS:
                continue
            metadata = load_prompt_metadata(path)
            register_prompt_command(parent, path.stem, path, metadata)
            has_entries = True

    return has_entries


build_cli(app, PROMPTS_DIR)
