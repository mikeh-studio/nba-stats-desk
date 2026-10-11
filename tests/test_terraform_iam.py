"""Guard against reintroducing broad IAM grants outside the mocked resource tests."""

import re
from pathlib import Path


def test_project_iam_resources_are_limited_to_job_submission():
    terraform = Path(__file__).resolve().parents[1] / "infra" / "terraform"
    resources = []
    for path in terraform.glob("*.tf"):
        resources.extend(
            re.findall(
                r'resource\s+"(google_project_iam_[^"]+)"\s+"([^"]+)"', path.read_text()
            )
        )
    assert resources == [("google_project_iam_member", "bq_job_user")]
