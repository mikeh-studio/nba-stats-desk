"""Synthetic exported contexts only; never invoke Docker or a registry."""

import copy
import io
import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from scripts.check_image_context import sha, validate


@pytest.fixture
def context():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("provider/__init__.py", "")
        project = Path(__file__).resolve().parents[1]
        contents = {
            "Dockerfile": (project / "Dockerfile").read_bytes(),
            ".dockerignore": (project / ".dockerignore").read_bytes(),
            "host/app/main.py": b"app = None\n",
            "wheels/provider-1.0-py3-none-any.whl": wheel.getvalue(),
            "locks/runtime.lock": (
                "provider==1.0 --hash=sha256:" + sha(wheel.getvalue())
            ).encode(),
        }
        for name, raw in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        files = {k: sha(v) for k, v in contents.items()}
        data = {
            "schema": "host-image-context/1",
            "base_image": "registry.example/python@sha256:" + "a" * 64,
            "platform": "linux/amd64",
            "host": {"commit": "a" * 40, "tree": "b" * 40, "source_sha256": "c" * 64},
            "wheel": {
                "path": "wheels/provider-1.0-py3-none-any.whl",
                "sha256": files["wheels/provider-1.0-py3-none-any.whl"],
                "source_commit": "d" * 40,
                "source_tree": "e" * 40,
                "input_sha256": "f" * 64,
            },
            "locks": {
                "runtime": files["locks/runtime.lock"],
                "build": "a" * 64,
                "host": "b" * 64,
            },
            "toolchain": {
                "python": "3.11.4",
                "pip": "24.0",
                "builder": "buildkit v0.16.0",
            },
            "files": files,
        }
        (root / "image-context.json").write_text(json.dumps(data))
        yield root, data


def test_valid_allowlisted_context(context):
    root, data = context
    assert validate(root, data)["files"] == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_image", "python:latest"),
        ("base_image", "python:3.11@sha256:" + "a" * 64),
        ("platform", ""),
        ("platform", "darwin/arm64"),
    ],
)
def test_missing_or_mutable_identity(context, field, value):
    root, data = context
    data[field] = value
    with pytest.raises(ValueError):
        validate(root, data)


@pytest.mark.parametrize(
    "name",
    [
        "host/.env",
        "host/.ENV.production",
        "host/captures/raw.json",
        "host/.cache/x.py",
        "host/app/agent/planner.py",
        "host/credentials.json",
        "other.py",
    ],
)
def test_forbidden_paths_even_when_declared(context, name):
    root, data = context
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic")
    data["files"][name] = sha(b"synthetic")
    with pytest.raises(ValueError):
        validate(root, data)


def test_changed_or_missing_bytes_and_undeclared_files(context):
    root, data = context
    (root / "host/app/main.py").write_text("changed")
    with pytest.raises(ValueError):
        validate(root, data)
    (root / "host/app/main.py").unlink()
    with pytest.raises(ValueError):
        validate(root, data)


def test_escaping_symlink(context, tmp_path):
    root, data = context
    (root / "host/link.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(ValueError):
        validate(root, data)


@pytest.mark.parametrize(
    "raw",
    [
        b"from app.agent.planner import build_query_plan",
        b"import shared_platform",
        b'creds = {"type": "service_account"}',
    ],
)
def test_legacy_import_and_credential_content(context, raw):
    root, data = context
    (root / "host/app/main.py").write_bytes(raw)
    data["files"]["host/app/main.py"] = sha(raw)
    with pytest.raises(ValueError):
        validate(root, data)


@pytest.mark.parametrize(
    "line",
    [
        "-e ../package",
        "provider @ https://example.invalid/a.whl",
        "provider @ file:/tmp/a.whl",
    ],
)
def test_editable_or_remote_lock(context, line):
    root, data = context
    raw = (line + " --hash=sha256:" + "a" * 64).encode()
    (root / "locks/runtime.lock").write_bytes(raw)
    data["files"]["locks/runtime.lock"] = data["locks"]["runtime"] = sha(raw)
    with pytest.raises(ValueError):
        validate(root, data)


def test_unknown_fields_and_wheel_substitution(context):
    root, data = context
    bad = copy.deepcopy(data)
    bad["credentials"] = "forbidden"
    with pytest.raises(ValueError):
        validate(root, bad)
    data["wheel"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate(root, data)


@pytest.mark.parametrize(
    "member",
    [
        "../escape.py",
        "/absolute.py",
        "app/main.py",
        "provider.pth",
        "provider/__editable__.py",
        "provider/.env",
    ],
)
def test_malicious_wheel_members_are_rejected(context, member):
    root, data = context
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(member, "synthetic")
    raw = stream.getvalue()
    path = data["wheel"]["path"]
    (root / path).write_bytes(raw)
    data["files"][path] = data["wheel"]["sha256"] = sha(raw)
    lock = ("provider==1.0 --hash=sha256:" + sha(raw)).encode()
    (root / "locks/runtime.lock").write_bytes(lock)
    data["files"]["locks/runtime.lock"] = data["locks"]["runtime"] = sha(lock)
    with pytest.raises(ValueError):
        validate(root, data)


def test_recipe_substitution_is_rejected_even_with_matching_hash(context):
    root, data = context
    raw = b"FROM python:latest\n"
    (root / "Dockerfile").write_bytes(raw)
    data["files"]["Dockerfile"] = sha(raw)
    with pytest.raises(ValueError):
        validate(root, data)


def replace_wheel(context, member, content=b"synthetic = True"):
    root, data = context
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(member, content)
    raw = stream.getvalue()
    path = data["wheel"]["path"]
    (root / path).write_bytes(raw)
    data["files"][path] = data["wheel"]["sha256"] = sha(raw)
    lock = ("provider==1.0 --hash=sha256:" + sha(raw)).encode()
    (root / "locks/runtime.lock").write_bytes(lock)
    data["files"]["locks/runtime.lock"] = data["locks"]["runtime"] = sha(lock)


@pytest.mark.parametrize(
    "scheme,remainder",
    [
        ("purelib", "provider/extra.py"),
        ("platlib", "provider/extra.py"),
        ("scripts", "provider-tool.py"),
        ("headers", "provider/header.h"),
        ("data", "share/provider/info.txt"),
    ],
)
def test_benign_wheel_relocations_are_validated(context, scheme, remainder):
    replace_wheel(context, "provider-1.0.data/" + scheme + "/" + remainder)
    assert validate(*context)["files"] == 5


@pytest.mark.parametrize(
    "member",
    [
        "provider-1.0.data/purelib/app/main.py",
        "provider-1.0.data/platlib/app/main.py",
        "provider-1.0.data/purelib/shared_platform/__init__.py",
        "provider-1.0.data/data/lib/python3.11/site-packages/app/main.py",
        "provider-1.0.data/scripts/.env",
        "provider-1.0.data/headers/credentials.json",
        "provider-1.0.data/data/captures/raw.json",
        "provider-1.0.data/purelib/provider.pth",
        "provider-1.0.data/unknown/provider.py",
    ],
)
def test_wheel_relocation_cannot_hide_shadow_sensitive_or_editable_paths(
    context, member
):
    replace_wheel(context, member)
    with pytest.raises(ValueError):
        validate(*context)


@pytest.mark.parametrize("scheme", ["purelib", "platlib", "scripts", "data", "headers"])
def test_legacy_import_scan_applies_after_every_wheel_relocation(context, scheme):
    replace_wheel(
        context,
        "provider-1.0.data/" + scheme + "/provider/extra.py",
        b"from app.agent.planner import build_query_plan",
    )
    with pytest.raises(ValueError):
        validate(*context)


@pytest.mark.parametrize(
    "member",
    [
        "openai/types/shared/__init__.py",
        "openai/resources/realtime/client_secrets.py",
        "google/auth/credentials.py",
    ],
)
def test_dependency_code_names_are_not_private_artifacts(context, member):
    replace_wheel(context, member, b"VALUE = None")
    assert validate(*context)["files"] == 5


def test_wheel_pem_marker_constant_is_not_key_material(context):
    raw = b'HEADER = "-----BEGIN ' + b'RSA PRIVATE KEY-----"\n'
    replace_wheel(context, "google/auth/crypt/_python_rsa.py", raw)
    assert validate(*context)["files"] == 5


@pytest.mark.parametrize("source_literal", [False, True])
def test_wheel_key_material_is_rejected_even_inside_python(context, source_literal):
    pem = (
        b"-----BEGIN "
        + b"PRIVATE KEY-----\n"
        + b"A" * 96
        + b"\n-----END "
        + b"PRIVATE KEY-----"
    )
    member = "provider/key.py" if source_literal else "provider/key.pem"
    raw = b"KEY = " + repr(pem).encode() if source_literal else pem
    replace_wheel(context, member, raw)
    with pytest.raises(ValueError, match="credential material"):
        validate(*context)


def test_public_certificate_bundle_in_dependency_namespace_is_allowed(context):
    replace_wheel(
        context,
        "grpc/_cython/_credentials/roots.pem",
        b"-----BEGIN CERTIFICATE-----\n" + b"A" * 96 + b"\n-----END CERTIFICATE-----",
    )
    assert validate(*context)["files"] == 5


@pytest.mark.parametrize("form", ["adjacent_literals", "encrypted_pem"])
def test_wheel_pem_body_cannot_hide_behind_literal_or_encryption_headers(context, form):
    header = b"-----BEGIN " + b"RSA PRIVATE KEY-----\n"
    footer = b"\n-----END " + b"RSA PRIVATE KEY-----"
    if form == "adjacent_literals":
        raw = (
            b"KEY = ("
            + repr(header).encode()
            + b"\n"
            + repr(b"A" * 96).encode()
            + b"\n"
            + repr(footer).encode()
            + b")"
        )
        member = "provider/key.py"
    else:
        raw = (
            header
            + b"Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,00000000000000000000000000000000\n\n"
            + b"A" * 96
            + footer
        )
        member = "provider/key.pem"
    replace_wheel(context, member, raw)
    with pytest.raises(ValueError, match="credential material"):
        validate(*context)


def test_noncredential_unicode_constant_in_wheel_is_allowed(context):
    replace_wheel(context, "provider/unicode_helpers.py", b'LOW_SURROGATE = "\\udc80"')
    assert validate(*context)["files"] == 5
