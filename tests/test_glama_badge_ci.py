"""
2026-11-07: Glama score badge right under the README's H1, and a CI workflow
that runs this suite on Python 3.11 (production) and 3.12 with no secrets
and no deploy step. No network.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GLAMA = "https://glama.ai/mcp/servers/whatevercat-creator/crypto-sentiment-x402"
BADGE = (
    "[![Crypto News Sentiment (x402) MCP server – quality and maintenance score on Glama]"
    f"({GLAMA}/badges/score.svg)]({GLAMA})"
)
TESTS_YML = ROOT / ".github" / "workflows" / "tests.yml"


def test_badge_is_first_thing_under_h1():
    lines = (ROOT / "README.md").read_text().splitlines()
    assert lines[0] == "# Crypto Sentiment API (x402)"
    assert [l for l in lines[1:] if l.strip()][0] == BADGE


def test_glama_todo_removed():
    assert "TODO(0.1.7)" not in (ROOT / "README.md").read_text()


def test_tests_workflow_runs_both_pythons_on_push_and_pr():
    y = TESTS_YML.read_text()
    assert 'python-version: ["3.11", "3.12"]' in y
    assert "push:" in y and "pull_request:" in y and "branches: [main]" in y
    assert "-r requirements.txt -r requirements-mcp.txt" in y
    assert "python -m pytest" in y


def test_tests_workflow_has_no_secrets_or_deploy():
    y = TESTS_YML.read_text().lower()
    assert "secrets." not in y
    assert "render" not in y.replace("render-deploy.yml", "")
    assert "curl" not in y and "deploy_hook" not in y


def test_actions_pinned_to_a_version():
    for line in TESTS_YML.read_text().splitlines():
        if "uses:" in line:
            ref = line.split("uses:")[1].strip()
            assert "@" in ref and ref.split("@")[1] not in ("main", "master", "latest", "")


def test_tests_workflow_installs_the_mcp_package():
    # crypto_sentiment_x402_mcp.__version__ comes from installed metadata, so
    # CI must install the package itself (deps already come from requirements).
    assert "pip install --no-deps ." in TESTS_YML.read_text()
