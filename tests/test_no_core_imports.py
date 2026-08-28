"""Architecture and secret gates.

These tests are the automated form of the project boundary. They fail the build
rather than relying on reviewer memory.

1. The bridge must never import Zero3 Core Python modules. Zero3 is reachable
   only over HTTPS through the Commander Gateway.
2. No credentials, private keys, hardcoded production addresses, or absolute
   user paths may enter the repository.
3. TLS verification must never be disabled.

This file excludes itself from every scan, since it necessarily contains the
patterns it searches for.
"""

from __future__ import annotations

import io
import json
import re
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# Directories that are never scanned: version control, virtualenvs, caches.
EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", "node_modules", "build", "dist", ".eggs"}

TEXT_SUFFIXES = {".py", ".json", ".yml", ".yaml", ".toml", ".md", ".cfg", ".ini",
                 ".txt", ".sh", ".example", ".gitignore", ".gitkeep"}


def iter_files(*relative_dirs: str, suffixes: set[str] | None = None):
    """Yield scannable files, always skipping this test module."""
    roots = [REPO_ROOT / d for d in relative_dirs] if relative_dirs else [REPO_ROOT]
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if not path.is_file():
                continue
            if any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            if path.resolve() == SELF:
                continue
            if suffixes is not None and path.suffix not in suffixes:
                continue
            yield path


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _mask_python(content: str) -> str:
    """Blank out comments and string literals, preserving line numbers.

    Documentation must be able to *state* a prohibition without tripping the
    gate that enforces it. Only executable code is scanned.
    """
    masked = content.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(content).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return content

    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            index = row - 1
            if index >= len(masked):
                continue
            line = masked[index]
            begin = start_col if row == start_row else 0
            finish = end_col if row == end_row else len(line)
            masked[index] = line[:begin] + " " * max(0, finish - begin) + line[finish:]
    return "\n".join(masked)


def _mask_comments(content: str) -> str:
    """Blank out ``#`` comments in non-Python files, preserving line numbers."""
    out = []
    for line in content.splitlines():
        marker = line.find("#")
        out.append(line[:marker] + " " * (len(line) - marker) if marker >= 0 else line)
    return "\n".join(out)


def read_code(path: Path) -> str:
    """Return only the executable portion of a file."""
    content = read(path)
    if path.suffix == ".py":
        return _mask_python(content)
    if path.suffix in {".yml", ".yaml", ".sh", ".toml", ".cfg", ".ini"}:
        return _mask_comments(content)
    return content


# -- 1. Zero3 Core isolation ---------------------------------------------

CORE_IMPORT_PATTERNS = [
    ("Zero3 Core module import", re.compile(r"^\s*from\s+app[.\s]", re.M)),
    ("Zero3 Core module import", re.compile(r"^\s*import\s+app[.\s]", re.M)),
    ("Zero3 Core package path", re.compile(r"\bapp/(cloud|runtime|services)/")),
    ("Zero3 Core source path", re.compile(r"zero-three-self-media-management-system")),
]


def test_no_zero3_core_imports():
    """The bridge must not import or path-reference Zero3 Core.

    Documentation may name the Core repository; executable code and workflows
    may not depend on it.
    """
    violations = []
    for path in iter_files("src", "tests", ".github"):
        content = read_code(path)
        for label, pattern in CORE_IMPORT_PATTERNS:
            for match in pattern.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: {label}: {match.group(0).strip()!r}"
                )

    assert not violations, (
        "Zero3 Core must be reached only over HTTPS via the Commander Gateway:\n"
        + "\n".join(violations)
    )


def test_bridge_package_imports_nothing_from_core():
    """Belt and braces: check every import statement in the package."""
    offenders = []
    for path in iter_files("src", suffixes={".py"}):
        for number, line in enumerate(read_code(path).splitlines(), start=1):
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            if re.match(r"^(from|import)\s+(app|zero_three|zero3_core)\b", stripped):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {stripped}")
    assert not offenders, "forbidden Core imports:\n" + "\n".join(offenders)


# -- 2. Secrets and hardcoded deployment detail ---------------------------

SECRET_PATTERNS = [
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS secret access key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S{40}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("literal bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")),
    (
        "hardcoded credential literal",
        # Requires an actual quoted literal value, so schema field names and
        # environment variable names do not trip the gate.
        re.compile(
            r"""(?i)\b(token|secret|password|passwd|api[_-]?key)\s*[=:]\s*["'][^"'\s${}<>]{16,}["']"""
        ),
    ),
    ("absolute Windows user path", re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9]")),
    ("absolute POSIX home path", re.compile(r"/(home|Users)/[a-z][a-z0-9_-]*/")),
]


def test_no_secrets_committed():
    violations = []
    for path in iter_files(suffixes=TEXT_SUFFIXES):
        content = read(path)
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append(f"{path.relative_to(REPO_ROOT)}:{line}: {label}")

    assert not violations, "possible credentials committed:\n" + "\n".join(violations)


ALLOWED_ADDRESSES = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "1.0.0.0"}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def test_no_hardcoded_production_addresses():
    """Deployment addresses come from the environment, never from the repo.

    The legacy Core workflow pinned a public production IP in its ``env:``
    block. This bridge reads ``ZERO3_COMMANDER_BASE_URL`` instead.
    """
    violations = []
    for path in iter_files("src", ".github", suffixes=TEXT_SUFFIXES):
        content = read(path)
        for match in IPV4.finditer(content):
            address = match.group(0)
            if address in ALLOWED_ADDRESSES:
                continue
            octets = address.split(".")
            if any(int(o) > 255 for o in octets):
                continue  # a version string, not an address
            line = content[: match.start()].count("\n") + 1
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line}: {address}")

    assert not violations, "hardcoded IP addresses:\n" + "\n".join(violations)


# -- 3. TLS must stay verified -------------------------------------------

TLS_WEAKENING = [
    ("requests verify disabled", re.compile(r"verify\s*=\s*False")),
    ("certificate check disabled", re.compile(r"CERT_NONE")),
    ("hostname check disabled", re.compile(r"check_hostname\s*=\s*False")),
    ("unverified ssl context", re.compile(r"_create_unverified_context")),
    ("curl insecure", re.compile(r"curl\b[^\n]*\s(-k|--insecure)\b")),
]


def test_tls_verification_is_never_disabled():
    violations = []
    for path in iter_files("src", ".github", suffixes=TEXT_SUFFIXES):
        content = read_code(path)
        for label, pattern in TLS_WEAKENING:
            for match in pattern.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append(f"{path.relative_to(REPO_ROOT)}:{line}: {label}")

    assert not violations, "TLS verification must stay enabled:\n" + "\n".join(violations)


# -- 4. No domain knowledge in the transport ------------------------------

DOMAIN_TERMS = [
    re.compile(r"(?i)\bstoryboard\s*[=!<>]=?\s*\d"),
    re.compile(r"(?i)\bscenes?\s*[=!<>]=?\s*\d"),
    re.compile(r"(?i)\bbeats?\s*[=!<>]=?\s*\d"),
    re.compile(r"(?i)\bvisual_beats\s*[=!<>]=?\s*\d"),
]


def test_transport_contains_no_domain_assertions():
    """No storyboard, scene, or beat count rules may live in the transport."""
    violations = []
    for path in iter_files("src", ".github", suffixes=TEXT_SUFFIXES):
        content = read_code(path)
        for pattern in DOMAIN_TERMS:
            for match in pattern.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: {match.group(0)!r} "
                    "belongs to the Zero3 Core domain validator"
                )
    assert not violations, "domain rules in the transport layer:\n" + "\n".join(violations)


# -- 5. Declared capabilities match the implementation --------------------


def test_capabilities_match_enabled_commands():
    """capabilities.json must describe what the bridge actually does."""
    from zero3_pilot_commander_bridge.ingestor import ENABLED_COMMANDS

    declared = json.loads((REPO_ROOT / "bridge" / "capabilities.json").read_text(encoding="utf-8"))
    enabled = {name for name, value in declared["commands"].items() if value}

    # execution.status is served by the client rather than the command mailbox.
    assert ENABLED_COMMANDS <= enabled
    assert enabled == {"execution.submit", "execution.status"}


def test_enabled_capabilities_name_a_real_endpoint():
    declared = json.loads((REPO_ROOT / "bridge" / "capabilities.json").read_text(encoding="utf-8"))
    endpoints = declared["endpoints"]
    for name, value in declared["commands"].items():
        if value:
            assert name in endpoints, f"{name} is enabled but names no gateway endpoint"


@pytest.mark.parametrize("name", ["health.json", "capabilities.json"])
def test_bridge_documents_validate(name):
    from zero3_pilot_commander_bridge.validation import validate_against

    document = json.loads((REPO_ROOT / "bridge" / name).read_text(encoding="utf-8"))
    schema = "bridge-health.schema.json" if name == "health.json" else "capabilities.schema.json"
    validate_against(document, schema)
