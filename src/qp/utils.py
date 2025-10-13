from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import frontmatter
import typer


PROMPTS_DIR = Path("prompts")
DEFAULT_FORMAT = "commonmark"
DEFAULT_OUTPUT = "-"
RESERVED_PROMPTS = {"review"}


@dataclass
class PullRequestReference:
    repo: str
    number: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


def load_prompt_metadata(file_path: Path) -> Dict[str, Any]:
    """Return prompt metadata pulled from the Quarto front matter."""

    try:
        post = frontmatter.load(file_path)
    except FileNotFoundError:
        return {"description": None, "params": {}}

    front_matter = post.metadata or {}

    raw_params = front_matter.get("params", {})
    parsed_params: Dict[str, Dict[str, Any]] = {}
    for param_name, param_value in raw_params.items():
        default_value: Any = param_value
        help_text: Optional[str] = None

        if isinstance(param_value, dict):
            default_value = param_value.get("value", param_value.get("default"))
            help_text = param_value.get("description") or param_value.get("help")
            if default_value is None and "value" not in param_value and "default" not in param_value:
                default_value = param_value

        parsed_params[param_name] = {"default": default_value, "help": help_text}

    return {
        "description": front_matter.get("description"),
        "params": parsed_params,
    }


def infer_param_type(value: Any) -> Any:
    """Best-effort type inference for CLI options."""

    if isinstance(value, bool):
        return bool
    if isinstance(value, int):
        return int
    if isinstance(value, float):
        return float
    return str


def serialize_param_value(value: Any) -> str:
    """Convert a parameter value to a CLI-safe string."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def render_quarto(
    file_path: Path,
    params: Optional[Dict[str, Any]] = None,
    *,
    fmt: str = DEFAULT_FORMAT,
    output: str = DEFAULT_OUTPUT,
    label: Optional[str] = None,
) -> None:
    """Invoke Quarto with the provided parameters."""

    command = [
        "quarto",
        "render",
        str(file_path),
        "--to",
        fmt,
        "--output",
        output,
    ]

    if params:
        for name, value in params.items():
            if value is None:
                continue
            command.extend(["-P", f"{name}:{serialize_param_value(value)}"])

    try:
        subprocess.run(command, check=True)
    except FileNotFoundError:
        typer.secho(
            "Unable to find the 'quarto' executable. Install Quarto and ensure it is on your PATH.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError as exc:
        target = label or str(file_path)
        typer.secho(
            f"Quarto command failed for '{target}' (exit code {exc.returncode}).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=exc.returncode)


def render_prompt_template(
    prompt_name: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    replacements: Optional[Dict[str, str]] = None,
    fmt: str = DEFAULT_FORMAT,
    output: str = DEFAULT_OUTPUT,
    prompts_dir: Path = PROMPTS_DIR,
) -> None:
    """Load a prompt, apply replacements, and render it with Quarto."""

    prompt_path = prompts_dir / prompt_name
    if not prompt_path.exists():
        typer.secho(
            f"Prompt '{prompt_name}' not found inside '{prompts_dir}'.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    post = frontmatter.load(prompt_path)
    metadata = post.metadata or {}

    if params:
        param_section = metadata.setdefault("params", {})
        param_section.update(params)

    content = post.content
    if replacements:
        for key, value in replacements.items():
            content = content.replace(f"{{{{{key}}}}}", value)

    post.metadata = metadata
    post.content = content

    with tempfile.NamedTemporaryFile(
        "w", suffix=prompt_path.suffix, delete=False, encoding="utf-8"
    ) as temp_file:
        temp_file.write(frontmatter.dumps(post))
        temp_path = Path(temp_file.name)

    try:
        label = prompt_path.relative_to(prompts_dir).as_posix()
        render_quarto(temp_path, params=params, fmt=fmt, output=output, label=label)
    finally:
        temp_path.unlink(missing_ok=True)


def parse_pr_reference(reference: str, default_repo: str) -> PullRequestReference:
    """Parse a PR reference that may be a number, owner/repo#number, or URL."""

    value = reference.strip()
    if not value:
        raise typer.BadParameter("PR identifier cannot be empty.", param_hint="--pr")

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
        if len(segments) >= 4 and segments[-2] in {"pull", "pulls"}:
            repo = "/".join(segments[:2])
            number = segments[-1]
            return PullRequestReference(repo=repo, number=number)
        raise typer.BadParameter(
            "Unable to extract repository and PR number from the provided URL.",
            param_hint="--pr",
        )

    if "#" in value:
        repo_part, number_part = value.split("#", 1)
        repo = repo_part.strip() or default_repo
        if "/" not in repo:
            repo = default_repo
        number = number_part.strip().lstrip("#")
        if not number:
            raise typer.BadParameter("Missing pull request number after '#'.", param_hint="--pr")
        return PullRequestReference(repo=repo, number=number)

    digits = value.lstrip("#")
    if digits.isdigit():
        return PullRequestReference(repo=default_repo, number=digits)

    raise typer.BadParameter(
        "Provide a PR number (e.g. 768), owner/repo#number, or a full GitHub URL.",
        param_hint="--pr",
    )


__all__ = [
    "DEFAULT_FORMAT",
    "DEFAULT_OUTPUT",
    "PROMPTS_DIR",
    "RESERVED_PROMPTS",
    "PullRequestReference",
    "infer_param_type",
    "load_prompt_metadata",
    "parse_pr_reference",
    "render_prompt_template",
    "render_quarto",
]

