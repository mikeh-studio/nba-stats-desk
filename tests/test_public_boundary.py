import subprocess
from pathlib import Path

import pytest
from scripts.check_public_boundary import check_index, private_path


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        "app/.env.local",
        "AGENTS.local.md",
        "nested/AGENTS.override.md",
        "reports/review.html",
        "local_notes/history.json",
        "exports/warehouse.csv",
        "quarantine/rows.json",
        "notebooks/analysis.py",
        "draft.ipynb",
        "history.jsonl",
        "model.joblib",
        "embeddings.npy",
        "model.safetensors",
        "request.log",
        "infra/main.tfstate.backup",
        "infra/local.tfvars",
        "infra/local.auto.tfvars.json",
        "keys/account.pem",
        "dbt/profiles/.user.yml",
    ],
)
def test_private_artifacts_are_rejected(path):
    assert private_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        ".env.example",
        "app/.env.example",
        "infra/terraform/terraform.tfvars.example",
        "tests/fixtures/semantics/cases.json",
        "docs/images/ask-page.png",
        "app/agent/reporting_evidence.py",
        "docs/evaluation.md",
        "contracts/game_logs.yml",
    ],
)
def test_public_code_examples_and_fixtures_are_allowed(path):
    assert not private_path(path)


def init_repo(path: Path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def stage(path: Path, filename: str, content: str):
    (path / filename).write_text(content)
    subprocess.run(["git", "add", "-f", "--", filename], cwd=path, check=True)


def test_forced_private_files_and_staged_secrets_cannot_hide_in_worktree(tmp_path):
    init_repo(tmp_path)
    stage(tmp_path, ".gitignore", ".env\n")
    stage(tmp_path, ".env", "PLACEHOLDER=value\n")
    secret = "sk-" + "ant-" + "x" * 40
    stage(tmp_path, "settings.py", f'API_KEY = "{secret}"\n')
    (tmp_path / "settings.py").write_text('API_KEY = "redacted"\n')

    findings = check_index(tmp_path)
    assert (".env", "private artifact path") in findings
    assert ("settings.py", "provider token") in findings
    assert secret not in repr(findings)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "x" * 40,
        "sk-" + "proj-" + "x" * 40,
        "AKIA" + "A" * 16,
        "ghp_" + "a" * 36,
        "AIza" + "a" * 35,
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_credentials_are_rejected_without_echoing_content(tmp_path, secret):
    init_repo(tmp_path)
    stage(tmp_path, "accidental.txt", secret)
    findings = check_index(tmp_path)
    assert len(findings) == 1
    assert secret not in repr(findings)


def test_untracked_private_data_is_not_read(tmp_path):
    init_repo(tmp_path)
    stage(tmp_path, "AGENTS.md", "Shared instructions\n")
    (tmp_path / "AGENTS.local.md").write_text("Private local guidance\n")
    assert check_index(tmp_path) == []
