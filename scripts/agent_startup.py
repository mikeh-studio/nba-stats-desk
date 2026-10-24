"""Read-only agent preflight. Explicit intent is not permission to publish.

This standalone utility is maintained in the public platform and vendored in the
private product independently of its unchanged runtime dependency pin.
No network, credential reads, checkout changes, or configuration writes occur.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .ownership_policy import MAP_PATH, parse_map, resolve
else:
    from ownership_policy import MAP_PATH, parse_map, resolve

REPOS = {
    "public-platform": "mikeh-studio/nba-stats-desk",
    "private-product": "mikeh-studio/nba-stats-desk-product",
}
INTENTS = {
    "product": {"private-product"},
    "product-ui": {"public-platform"},
    "shared-fix": {"public-platform"},
    "reference-maintenance": {"public-platform"},
    "public-promotion": {"public-platform"},
    "policy": set(REPOS),
}
POINTERS = {
    "CLAUDE.md": "@AGENTS.md\n",
    "GEMINI.md": "@./AGENTS.md\n",
    ".github/copilot-instructions.md": (
        "Read and follow the repository-root AGENTS.md before editing or publishing.\n"
        "It defines the thin-split ownership policy and required startup check.\n"
    ),
}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if result.returncode:
        # Never echo raw Git stderr: remote URLs can contain credentials.
        raise ValueError(
            "Git preflight failed; check the checkout and origin configuration"
        )
    return result.stdout.strip()


def configured(root: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "config", "--get", key], capture_output=True, text=True
    )
    if result.returncode not in (0, 1):
        raise ValueError("Unable to inspect Git push routing")
    return result.stdout.strip()


def repository(url: str) -> str:
    """Normalize canonical GitHub HTTPS/SSH URLs without printing credentials."""
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid origin URL") from exc
        if parsed.hostname != "github.com" or parsed.query or parsed.fragment:
            raise ValueError("origin must use a canonical GitHub repository URL")
        if parsed.scheme == "https" and not parsed.username and not parsed.password:
            if port not in (None, 443):
                raise ValueError("Unsupported origin port")
        elif (
            parsed.scheme == "ssh" and parsed.username == "git" and not parsed.password
        ):
            if port not in (None, 22):
                raise ValueError("Unsupported origin port")
        else:
            raise ValueError("origin must use credential-free HTTPS or GitHub SSH")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise ValueError("Invalid origin repository path")
    return path.casefold()


def indexed(root: Path, path: str) -> str:
    return git(root, "show", f":{path}")


def check_policy(root: Path) -> dict:
    try:
        policy = json.loads(indexed(root, "docs/agent-policy.json"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Missing staged agent policy; refresh this stale checkout from main"
        ) from exc
    role = policy.get("role")
    if policy.get("version") != 2 or role not in REPOS:
        raise ValueError("Unknown agent policy version or repository role")
    boundary = (
        "docs/boundary.md"
        if role == "private-product"
        else "docs/public-private-boundary.md"
    )
    if policy.get("boundary") != boundary or not isinstance(policy.get("checks"), list):
        raise ValueError("Invalid agent policy boundary or checks")
    if policy.get("repository") != REPOS[role]:
        raise ValueError("Agent policy repository does not match its role")
    expected_intents = sorted(name for name, roles in INTENTS.items() if role in roles)
    if policy.get("intents") != expected_intents or policy.get("ownership") != MAP_PATH:
        raise ValueError("Agent policy intents or ownership map do not match its role")
    parse_map(indexed(root, MAP_PATH))
    for path, expected in POINTERS.items():
        if indexed(root, path) != expected.strip():
            raise ValueError(
                f"{path}: adapter must point to AGENTS.md without separate policy"
            )
    for path in (
        "AGENTS.md",
        "docs/agent-workflow.md",
        "docs/repository-ownership.md",
        policy["boundary"],
    ):
        if not indexed(root, path):
            raise ValueError(f"{path}: required policy file is empty")
    agents = indexed(root, "AGENTS.md")
    for token in (
        "scripts/agent_startup.py",
        "docs/agent-workflow.md",
        "thin split",
    ):
        if token not in agents:
            raise ValueError(f"AGENTS.md must reference {token}")
    return policy


def check_staged_ownership(root: Path) -> None:
    """Reject accidental private-intelligence edits in public UI/policy work."""
    ownership = parse_map(indexed(root, MAP_PATH))
    # Disable rename detection so both a private source and destination are checked.
    paths = git(root, "diff", "--cached", "--name-only", "--no-renames", "-z").split(
        "\0"
    )
    private_paths = []
    for path in filter(None, paths):
        rule = resolve(ownership, path)
        if rule is not None and rule["owner"] == "private":
            private_paths.append(path)
    if private_paths:
        raise ValueError(
            "Staged private-owned paths are outside public UI/policy scope: "
            + ", ".join(sorted(private_paths))
            + ". Use the owning repository; maintenance or promotion requires its explicit scope."
        )


def check(root: Path, intent: str, *, policy_only: bool = False) -> dict:
    root = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    if git(root, "rev-parse", "--show-superproject-working-tree"):
        raise ValueError(
            "Do not start development inside a dependency; use the owning repository root"
        )
    policy = check_policy(root)
    expected = policy["repository"]
    for args in (
        ("remote", "get-url", "--all", "origin"),
        ("remote", "get-url", "--push", "--all", "origin"),
    ):
        urls = git(root, *args).splitlines()
        if not urls or any(repository(url) != expected for url in urls):
            raise ValueError(
                "origin fetch/push destination does not match this repository's policy"
            )
    if intent not in policy["intents"] and not policy_only:
        if intent == "product":
            raise ValueError(
                "Production intelligence belongs in mikeh-studio/nba-stats-desk-product; use product-ui for public app work"
            )
        raise ValueError(
            "This intent belongs in the public platform; prepare a minimal sanitized change there"
        )
    branch = git(root, "branch", "--show-current")
    if configured(root, "remote.pushDefault") not in ("", "origin") or (
        branch and configured(root, f"branch.{branch}.pushRemote") not in ("", "origin")
    ):
        raise ValueError(
            "Alternate Git push routing configured; use the reviewed origin explicitly"
        )
    if not policy_only:
        if not branch.startswith("codex/"):
            raise ValueError(
                "Start implementation on a fresh codex/ branch; main and detached HEAD are read-only"
            )
        # Refuse stale/tampered local instructions even if staged policy is valid.
        for path in (
            "AGENTS.md",
            "docs/agent-policy.json",
            "docs/agent-workflow.md",
            "docs/repository-ownership.md",
            MAP_PATH,
            policy["boundary"],
            *POINTERS,
        ):
            if (root / path).read_text().strip() != indexed(root, path):
                raise ValueError(
                    f"{path}: worktree and staged guidance differ; review and stage policy edits first"
                )
        if (root / "AGENTS.override.md").exists():
            raise ValueError(
                "AGENTS.override.md shadows shared guidance; reconcile it before starting"
            )
    if (
        not policy_only
        and policy["role"] == "public-platform"
        and intent in {"product-ui", "policy"}
    ):
        check_staged_ownership(root)
    changed = git(root, "status", "--porcelain", "--untracked-files=normal")
    return {
        "repository": expected,
        "root": str(root),
        "branch": branch or "detached (policy-only CI check)",
        "role": policy["role"],
        "intent": intent,
        "working_changes": bool(changed),
        "policy_only": policy_only,
        "ownership": MAP_PATH,
        "checks": policy["checks"],
        "push_destination": f"origin ({expected})",
        "publication_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--intent", choices=INTENTS, default="product")
    parser.add_argument(
        "--check-policy",
        action="store_true",
        help="CI layout validation only; never authorizes implementation",
    )
    args = parser.parse_args()
    try:
        report = check(args.root, args.intent, policy_only=args.check_policy)
    except (ValueError, OSError, KeyError) as exc:
        print(f"Agent startup blocked: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    print(
        "Preserve existing work. Read AGENTS.md and the ownership map before editing."
    )
    print(
        "This check does not grant public publishing, merge, or production permission."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
