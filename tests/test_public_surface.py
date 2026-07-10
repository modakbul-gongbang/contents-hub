from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOTS = [
    "README.md",
    "RELEASE_NOTES.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "install.md",
    ".env.example",
    "docs",
    "skills",
    "src",
    "tests",
    "pyproject.toml",
]
IGNORED_DIRS = {
    ".git",
    ".venv",
    ".contents-hub",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}


def _public_files() -> list[Path]:
    paths: list[Path] = []
    for public_root in PUBLIC_ROOTS:
        path = ROOT / public_root
        if path.is_file():
            paths.append(path)
            continue
        for candidate in path.rglob("*"):
            if candidate.is_dir():
                continue
            if any(part in IGNORED_DIRS for part in candidate.relative_to(ROOT).parts):
                continue
            if candidate.suffix in {".pyc", ".png", ".jpg", ".jpeg", ".gif"}:
                continue
            paths.append(candidate)
    return paths


def test_public_surface_uses_canonical_contents_hub_names_only():
    forbidden = [
        "llm" + "_wiki",
        "llm" + "-wiki",
        "LLM" + "_WIKI",
        "." + "llm" + "-wiki",
    ]
    offenders: list[str] = []
    for path in _public_files():
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert offenders == []


def test_public_surface_does_not_contain_private_paths_or_secret_literals():
    forbidden = [
        "/" + "Users/",
        "ANTHROPIC" + "_API_KEY",
        "TELE" + "GRAM",
        "BOT" + "_TOKEN",
        "i" + "Cloud",
        "." + "hoy" + "eon",
    ]
    offenders: list[str] = []
    for path in _public_files():
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert offenders == []


def test_public_skill_surface_is_single_contents_hub_skill():
    skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert [path.relative_to(ROOT).as_posix() for path in skill_files] == [
        "skills/contents-hub/SKILL.md"
    ]


def test_launch_docs_keep_first_success_and_followups_clear():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "install.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    channels = (ROOT / "docs" / "channels.md").read_text(encoding="utf-8")
    launch = (ROOT / "docs" / "launch.md").read_text(encoding="utf-8")
    runtime_matrix = (ROOT / "docs" / "runtime-matrix.md").read_text(encoding="utf-8")
    schedulers = (ROOT / "docs" / "schedulers.md").read_text(encoding="utf-8")
    hermes_setup = (ROOT / "docs" / "hermes-setup.md").read_text(encoding="utf-8")
    openclaw_setup = (ROOT / "docs" / "openclaw-setup.md").read_text(encoding="utf-8")
    skill = (ROOT / "skills" / "contents-hub" / "SKILL.md").read_text(encoding="utf-8")

    assert "Reliable first-launch path" in readme
    assert "uv sync\nuv run contents-hub --help" in readme
    assert "manual content, local digest generation" in install
    assert "Manual URL/text is the shortest first-launch path" in quickstart
    assert "automatic `manual-inbox`" in launch
    assert "does not ship built-in Telegram, Slack, or Discord bot packages" in channels
    assert "ok`, `count`, `items`, `groups`, and `delivery_instruction`" in channels
    assert "raw_item` and `digest` are the only" in channels
    assert "openclaw skills install ./skills/contents-hub --as contents-hub --global" in install
    assert "OpenClaw Git installs expect `SKILL.md` at repo root" in skill
    assert "hermes skills install skills-sh/yansfil/contents-hub/skills/contents-hub --yes" in install
    assert "hermes cron create" in schedulers
    assert "delivery record" in schedulers
    assert "Skill Registration Notes" in runtime_matrix
    assert "docs/hermes-setup.md" in runtime_matrix
    assert "docs/openclaw-setup.md" in runtime_matrix
    assert "Profile-Aware Install" in hermes_setup
    assert "Existing Vault Safety" in hermes_setup
    assert "Recommended Cron Topology" in hermes_setup
    assert "Production-Like No-Agent Topology" in hermes_setup
    assert "Reference Hourly Adapter Script Shape" in hermes_setup
    assert '"hermes", "--profile", HERMES_PROFILE' in hermes_setup
    assert "HERMES_SEND_TARGET must include an explicit channel id" in hermes_setup
    assert "telegram_raw_item_messages" in hermes_setup
    assert "Adapter Delivery" in hermes_setup
    assert "OpenClaw Setup Runbook" in openclaw_setup
    assert "openclaw cron create" in openclaw_setup
    assert "test -f" not in openclaw_setup
    assert "if [ -f" in openclaw_setup
    assert "--payload-type digest" in openclaw_setup
    assert "Runtime final response" in openclaw_setup
    assert "save_and_promote` inserts the item into `saved_items`" in channels
    assert "Current contents-hub migrations copy" in channels
    assert "Setup Mode" in skill
    assert "github.releases" in skill
    assert "substack.tag" in skill
    assert "Reactions only work when a per-card adapter preserves" in skill
    assert "Do not ask the user to install a separate init skill" in skill
    assert "version: 0.2.0" in skill
    assert "platform demo" in launch
    public_launch_docs = (
        readme + install + quickstart + channels + launch + runtime_matrix + schedulers
        + hermes_setup + openclaw_setup + skill
    )
    assert "contents-hub-explore" not in public_launch_docs
    assert "--raw-item-id 1" not in public_launch_docs
    assert "digest section/item cards" not in public_launch_docs
