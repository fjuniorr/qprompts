from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import frontmatter
from jinja2 import Environment, StrictUndefined, TemplateError
import typer


PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
PROMPTS_DIR = TEMPLATES_DIR
DEFAULT_FORMAT = "commonmark"
DEFAULT_OUTPUT = "-"
SUPPORTED_FORMATS = {DEFAULT_FORMAT, "markdown"}
TEMPLATE_SUFFIX = ".md.jinja"
RESERVED_PROMPTS = {"review"}


@dataclass
class PullRequestReference:
    repo: str
    number: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


@dataclass
class IssueReference:
    repo: str
    number: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/issues/{self.number}"


def load_prompt_metadata(file_path: Path) -> Dict[str, Any]:
    """Return prompt metadata pulled from the file front matter."""

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
            if (
                default_value is None
                and "value" not in param_value
                and "default" not in param_value
            ):
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


def copy_to_clipboard(text: str) -> bool:
    """Attempt to copy text to the system clipboard."""

    if not text:
        return False

    commands: List[List[str]] = []
    platform = sys.platform
    if platform == "darwin":
        commands = [["pbcopy"]]
    elif platform.startswith("linux"):
        commands = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    elif platform.startswith("win"):
        commands = [["clip"]]
    else:
        return False

    for command in commands:
        try:
            subprocess.run(command, input=text, check=True, text=True)
            return True
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError:
            continue

    return False


def render_prompt_template(
    prompt_name: str | Path,
    *,
    params: Optional[Dict[str, Any]] = None,
    fmt: str = DEFAULT_FORMAT,
    output: str = DEFAULT_OUTPUT,
    prompts_dir: Path = PROMPTS_DIR,
) -> None:
    """Load a prompt, render it with Jinja2, and write to the requested output."""

    if fmt not in SUPPORTED_FORMATS:
        typer.secho(
            f"Unsupported format '{fmt}'. Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    prompt_relative = Path(prompt_name)
    if prompt_relative.is_absolute():
        prompt_path = prompt_relative
    else:
        prompt_path = prompts_dir / prompt_relative
    if not prompt_path.exists():
        typer.secho(
            f"Prompt '{prompt_name}' not found inside '{prompts_dir}'.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    post = frontmatter.load(prompt_path)
    metadata = post.metadata or {}
    template_str = post.content or ""

    env = Environment(
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    context: Dict[str, Any] = {"metadata": metadata}
    if params:
        context.update(params)

    try:
        rendered = env.from_string(template_str).render(**context)
    except TemplateError as exc:
        typer.secho(
            f"Unable to render template '{prompt_path.name}': {exc}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if output == "-":
        if rendered and not rendered.endswith("\n"):
            rendered += "\n"
        typer.echo(rendered, nl=False)
        if rendered.strip() and not copy_to_clipboard(rendered):
            typer.secho(
                "Unable to copy output to the clipboard (no clipboard command found).",
                fg=typer.colors.YELLOW,
                err=True,
            )
    else:
        try:
            Path(output).write_text(rendered, encoding="utf-8")
        except OSError as exc:
            typer.secho(
                f"Failed to write rendered prompt to '{output}': {exc}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)


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
            raise typer.BadParameter(
                "Missing pull request number after '#'.", param_hint="--pr"
            )
        return PullRequestReference(repo=repo, number=number)

    digits = value.lstrip("#")
    if digits.isdigit():
        return PullRequestReference(repo=default_repo, number=digits)

    raise typer.BadParameter(
        "Provide a PR number (e.g. 768), owner/repo#number, or a full GitHub URL.",
        param_hint="--pr",
    )


def parse_issue_reference(reference: str, default_repo: str) -> IssueReference:
    """Parse an issue reference that may be a number, owner/repo#number, or URL."""

    value = reference.strip()
    if not value:
        raise typer.BadParameter(
            "Issue identifier cannot be empty.", param_hint="--issue"
        )

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
        if len(segments) >= 4 and segments[-2] in {"issues", "issue"}:
            repo = "/".join(segments[:2])
            number = segments[-1]
            return IssueReference(repo=repo, number=number)
        raise typer.BadParameter(
            "Unable to extract repository and issue number from the provided URL.",
            param_hint="--issue",
        )

    if "#" in value:
        repo_part, number_part = value.split("#", 1)
        repo = repo_part.strip() or default_repo
        if "/" not in repo:
            repo = default_repo
        number = number_part.strip().lstrip("#")
        if not number:
            raise typer.BadParameter(
                "Missing issue number after '#'.", param_hint="--issue"
            )
        return IssueReference(repo=repo, number=number)

    digits = value.lstrip("#")
    if digits.isdigit():
        return IssueReference(repo=default_repo, number=digits)

    raise typer.BadParameter(
        "Provide an issue number (e.g. 768), owner/repo#number, or a full GitHub URL.",
        param_hint="--issue",
    )


__all__ = [
    "DEFAULT_FORMAT",
    "DEFAULT_OUTPUT",
    "PROMPTS_DIR",
    "TEMPLATES_DIR",
    "RESERVED_PROMPTS",
    "TEMPLATE_SUFFIX",
    "SUPPORTED_FORMATS",
    "IssueReference",
    "PullRequestReference",
    "infer_param_type",
    "load_prompt_metadata",
    "parse_issue_reference",
    "parse_pr_reference",
    "render_prompt_template",
]
