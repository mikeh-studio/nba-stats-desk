"""Validate an exported, allowlisted image context offline; never invoke a builder."""

import argparse
import ast
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
BASE = re.compile(r"[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}")
FORBIDDEN = {
    ".git",
    ".env",
    ".aws",
    ".config",
    "__pycache__",
    ".pytest_cache",
    ".cache",
    "captures",
    "traces",
    "exports",
    "reports",
    "credentials",
    "secrets",
    "shared_platform",
    "shared",
    "node_modules",
    "legacy",
}
# Package namespaces such as openai.types.shared are not repository artifacts.
# Shadow app/bridge packages are checked at their effective import root below.
WHEEL_FORBIDDEN = FORBIDDEN - {
    "shared",
    "shared_platform",
    "legacy",
    "reports",
    "exports",
}
WHEEL_SOURCE_SUFFIXES = {".py", ".pyi"}
LEGACY = {"planner.py", "router.py"}
SECRET = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:AKIA|ASIA)[A-Z0-9]{16}|\"type\"\s*:\s*\"service_account\""
)
# SDKs contain PEM delimiter constants. Require a body, but still inspect code
# literals (including escaped newlines); never exempt Python from content checks.
WHEEL_SECRET = re.compile(
    rb"-----BEGIN ([A-Z ]*PRIVATE KEY)-----\s*"
    rb"(?:[A-Za-z][A-Za-z0-9-]*:[^\r\n]*\r?\n)*\s*"
    rb"(?:[A-Za-z0-9+/=]\s*){64,}-----END \1-----"
    rb'|(?:AKIA|ASIA)[A-Z0-9]{16}|"type"\s*:\s*"service_account"'
)
EXTENSIONS = {
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".js",
    ".svg",
    ".ttf",
    ".txt",
    ".md",
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(name):
    path = PurePosixPath(name)
    parts = path.parts
    if (
        not parts
        or path.is_absolute()
        or str(path) != name
        or ".." in parts
        or "\\" in name
    ):
        raise ValueError("noncanonical context path")
    if any(p.lower() in FORBIDDEN or p.lower().startswith(".env") for p in parts):
        raise ValueError("forbidden context path")
    if name in {"Dockerfile", ".dockerignore", "locks/runtime.lock"}:
        return
    if parts[0] == "wheels" and len(parts) == 2 and path.suffix == ".whl":
        return
    if (
        parts[0] == "host"
        and path.suffix in EXTENSIONS
        and not any(p.startswith(".") for p in parts)
    ):
        if (
            path.name in LEGACY
            or "credential" in name.lower()
            or "secret" in name.lower()
        ):
            raise ValueError("legacy or sensitive context path")
        return
    raise ValueError("path outside context allowlist")


def validate_identity(data):
    if set(data) != {
        "schema",
        "base_image",
        "platform",
        "host",
        "wheel",
        "locks",
        "toolchain",
        "files",
    }:
        raise ValueError("unknown or missing context field")
    if data["schema"] != "host-image-context/1" or not BASE.fullmatch(
        data["base_image"]
    ):
        raise ValueError("explicit immutable base required")
    if data["platform"] not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("explicit supported platform required")
    host = data["host"]
    if (
        set(host) != {"commit", "tree", "source_sha256"}
        or not all(COMMIT.fullmatch(host[k]) for k in ("commit", "tree"))
        or not SHA.fullmatch(host["source_sha256"])
    ):
        raise ValueError("immutable public source identity required")
    wheel = data["wheel"]
    if (
        set(wheel) != {"path", "sha256", "source_commit", "source_tree", "input_sha256"}
        or not all(COMMIT.fullmatch(wheel[k]) for k in ("source_commit", "source_tree"))
        or not all(SHA.fullmatch(wheel[k]) for k in ("sha256", "input_sha256"))
    ):
        raise ValueError("committed wheel provenance required")
    safe_path(wheel["path"])
    if not wheel["path"].startswith("wheels/"):
        raise ValueError("wheel path required")
    if set(data["locks"]) != {"runtime", "build", "host"} or not all(
        SHA.fullmatch(v) for v in data["locks"].values()
    ):
        raise ValueError("runtime/build/host lock digests required")
    if set(data["toolchain"]) != {"python", "pip", "builder"} or any(
        not isinstance(v, str) or not v.strip() or v.lower() in {"latest", "tbd"}
        for v in data["toolchain"].values()
    ):
        raise ValueError("explicit toolchain required")
    if not all(
        re.fullmatch(r"\d+\.\d+(?:\.\d+)?", data["toolchain"][key])
        for key in ("python", "pip")
    ) or not re.fullmatch(
        r"(?:buildkit|docker|podman) v\d+\.\d+\.\d+", data["toolchain"]["builder"]
    ):
        raise ValueError("exact numeric toolchain versions required")


def check_python(raw):
    for node in ast.walk(ast.parse(raw)):
        modules = (
            [node.module or ""]
            + [
                (node.module + "." if node.module else "") + alias.name
                for alias in node.names
            ]
            if isinstance(node, ast.ImportFrom)
            else [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        if any(
            module.startswith(
                ("shared_platform", "app.agent.planner", "app.agent.router")
            )
            or module in {"planner", "router"}
            for module in modules
        ):
            raise ValueError("legacy import in context")


def wheel_destination(name):
    """Map wheel spreading to a canonical path under the image's /usr/local."""
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or str(path) != name.rstrip("/")
    ):
        raise ValueError("noncanonical wheel member")
    parts = path.parts
    if parts[0].endswith(".data"):
        if len(parts) < 3:
            raise ValueError("incomplete wheel relocation")
        scheme, remainder = parts[1], parts[2:]
        if scheme in {"purelib", "platlib"}:
            parts = ("lib", "python3.11", "site-packages", *remainder)
        elif scheme == "scripts":
            parts = ("bin", *remainder)
        elif scheme == "headers":
            parts = ("include", "python3.11", *remainder)
        elif scheme == "data":
            parts = remainder
        else:
            raise ValueError("unknown wheel relocation scheme")
    else:
        parts = ("lib", "python3.11", "site-packages", *parts)
    return PurePosixPath(*parts)


def check_wheel_path(original, effective):
    for path in (PurePosixPath(original), effective):
        parts = path.parts
        if any(
            part.lower() in WHEEL_FORBIDDEN or part.lower().startswith(".env")
            for part in parts
        ) or (
            ("credential" in path.name.lower() or "secret" in path.name.lower())
            and path.suffix not in WHEEL_SOURCE_SUFFIXES
        ):
            raise ValueError("sensitive wheel member")
        if (
            path.suffix in {".pth", ".egg-link"}
            or "__editable__" in str(path)
            or path.name == "direct_url.json"
        ):
            raise ValueError("editable wheel member")
    # Both direct packages and .data/data spreads can land in site-packages.
    parts = effective.parts
    for index, part in enumerate(parts):
        if (
            part in {"site-packages", "dist-packages"}
            and index + 1 < len(parts)
            and parts[index + 1] in {"app", "shared", "shared_platform"}
        ):
            raise ValueError("shadow app or legacy wheel member")


def wheel_has_credentials(raw, *, python_source=False):
    candidates = [raw.replace(b"\\r", b"\r").replace(b"\\n", b"\n")]
    if python_source:
        # AST constants join adjacent literals without executing untrusted code.
        for node in ast.walk(ast.parse(raw)):
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                value = node.value
                candidates.append(
                    value.encode("utf-8", errors="surrogatepass")
                    if isinstance(value, str)
                    else value
                )
    return any(WHEEL_SECRET.search(value) for value in candidates)


def validate(root, data):
    validate_identity(data)
    expected = data["files"]
    if not isinstance(expected, dict) or not {
        "Dockerfile",
        ".dockerignore",
        "locks/runtime.lock",
        "host/app/main.py",
        data["wheel"]["path"],
    } <= set(expected):
        raise ValueError("missing mandatory context inputs")
    actual = set()
    for file in root.rglob("*"):
        if file.is_symlink():
            raise ValueError("context symlinks are forbidden")
        if file.is_dir():
            continue
        name = file.relative_to(root).as_posix()
        if name == "image-context.json":
            continue
        safe_path(name)
        actual.add(name)
        if (
            name not in expected
            or not SHA.fullmatch(expected[name])
            or sha(file.read_bytes()) != expected[name]
        ):
            raise ValueError("undeclared or changed context bytes")
        if file.suffix == ".py":
            check_python(file.read_bytes())
        # Archives are inspected member-by-member below, not as opaque bytes.
        if file.suffix != ".whl" and SECRET.search(file.read_bytes()):
            raise ValueError("credential material in context")
    if actual != set(expected):
        raise ValueError("context input missing")
    if (
        expected[data["wheel"]["path"]] != data["wheel"]["sha256"]
        or expected["locks/runtime.lock"] != data["locks"]["runtime"]
    ):
        raise ValueError("wheel or lock identity mismatch")
    for name in ("Dockerfile", ".dockerignore"):
        if (root / name).read_bytes() != (
            Path(__file__).resolve().parents[1] / name
        ).read_bytes():
            raise ValueError("recipe/context policy differs from reviewed source")
    lock = (root / "locks/runtime.lock").read_text()
    if (
        re.search(r"(?im)^\s*(?:-e|--editable)|git\+|file:|https?://|\.\./", lock)
        or "--hash=sha256:" not in lock
    ):
        raise ValueError("nonhashed/editable/remote dependency")
    for wheel in (root / "wheels").glob("*.whl"):
        if "--hash=sha256:" + sha(wheel.read_bytes()) not in lock:
            raise ValueError("wheel absent from hashed runtime lock")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("duplicate wheel entry")
            destinations = set()
            for member in archive.infolist():
                name = member.filename
                path = PurePosixPath(name)
                if member.is_dir():
                    continue
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise ValueError("linked or special wheel member")
                effective = wheel_destination(name)
                check_wheel_path(name, effective)
                if effective in destinations:
                    raise ValueError("duplicate effective wheel destination")
                destinations.add(effective)
                raw = archive.read(member)
                if path.suffix == ".py":
                    check_python(raw)
                if wheel_has_credentials(raw, python_source=path.suffix == ".py"):
                    raise ValueError("credential material in wheel")
    return {
        "status": "offline context valid; build approval still required",
        "files": len(actual),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                validate(
                    args.context,
                    json.loads((args.context / "image-context.json").read_text()),
                )
            )
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, SyntaxError):
        print("Image context refused: invalid paths, bytes or immutable identities")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
