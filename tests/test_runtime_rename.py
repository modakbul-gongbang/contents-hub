from __future__ import annotations

import json
import plistlib
import shutil
import subprocess

import yaml
from fastapi.testclient import TestClient

from contents_hub import daemon as daemon_module
from contents_hub.cli import build_parser, main
from contents_hub.config import WikiConfig, load_config, resolve_vault_path, save_schedule_defaults
from contents_hub.db import init_db
from contents_hub.launchd import generate_plist, install, status, uninstall
from contents_hub.web.app import create_app


def test_vault_resolution_uses_explicit_then_env_then_cwd(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    env_vault = tmp_path / "env"
    for path in (explicit, env_vault):
        path.mkdir()

    monkeypatch.setenv("CONTENTS_HUB_VAULT", str(env_vault))

    assert resolve_vault_path() == env_vault.resolve()
    assert resolve_vault_path(explicit) == explicit.resolve()

    monkeypatch.delenv("CONTENTS_HUB_VAULT")
    monkeypatch.chdir(tmp_path)
    assert resolve_vault_path() == tmp_path.resolve()


def test_load_config_uses_canonical_metadata(tmp_path):
    meta = tmp_path / ".contents-hub"
    meta.mkdir()
    (meta / "state.db").write_bytes(b"state")

    cfg = load_config(tmp_path)

    assert cfg.meta_dir == ".contents-hub"
    assert cfg.meta_path == meta


def test_config_reads_and_writes_canonical_yaml(tmp_path):
    (tmp_path / ".contents-hub.yaml").write_text(
        yaml.safe_dump({"schedule": {"defaults": {"rss": 7}}, "keep": "yes"}),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    assert cfg.schedule.interval_for("rss") == 7
    assert cfg.config_file == tmp_path / ".contents-hub.yaml"

    save_schedule_defaults(
        tmp_path,
        global_interval=None,
        global_cron="*/15 * * * *",
        per_type={"rss": 9},
    )

    canonical = yaml.safe_load((tmp_path / ".contents-hub.yaml").read_text())
    assert canonical["keep"] == "yes"
    assert canonical["schedule"]["defaults"]["rss"] == 9
    assert canonical["schedule"]["global_cron"] == "*/15 * * * *"


def test_init_creates_canonical_metadata_and_database(tmp_path, capsys):
    vault = tmp_path / "vault"

    rc = main(["init", str(vault)])

    out = capsys.readouterr().out
    assert rc == 0
    assert (vault / ".contents-hub").is_dir()
    assert (vault / ".contents-hub" / "state.db").exists()
    assert "contents_hub" in out
    assert "contents-hub" in out


def test_cli_help_uses_contents_hub_branding():
    help_text = build_parser().format_help()

    assert "contents-hub" in help_text


def test_installed_contents_hub_command_works(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    init_db(WikiConfig(vault_path=vault)).close()

    contents_hub = shutil.which("contents-hub")
    assert contents_hub is not None

    help_result = subprocess.run(
        [contents_hub, "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "contents-hub" in help_result.stdout

    sub_list = subprocess.run(
        [contents_hub, "--vault", str(vault), "sub", "list", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert sub_list.returncode == 0
    assert json.loads(sub_list.stdout) == []


def test_dashboard_uses_contents_hub_branding(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)
    init_db(cfg).close()

    app = create_app(cfg)
    client = TestClient(app)

    response = client.get("/")

    assert app.title == "contents-hub Dashboard"
    assert response.status_code == 200
    assert "contents-hub" in response.text


def test_direct_init_db_uses_canonical_metadata_for_new_configs(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)

    conn = init_db(cfg)
    conn.close()

    assert (tmp_path / ".contents-hub" / "state.db").exists()


def test_launchd_plist_uses_canonical_label_logs_and_env(tmp_path):
    cfg = WikiConfig(vault_path=tmp_path)

    plist = plistlib.loads(generate_plist(cfg).encode("utf-8"))

    assert plist["Label"] == "com.contents-hub.daemon"
    assert plist["ProgramArguments"][2] == "contents_hub.daemon"
    assert plist["ProgramArguments"][3:6] == ["--vault", str(tmp_path), "loop"]
    assert plist["EnvironmentVariables"] == {"CONTENTS_HUB_VAULT": str(tmp_path)}
    assert ".contents-hub/daemon.log" in plist["StandardOutPath"]
    assert ".contents-hub/daemon-error.log" in plist["StandardErrorPath"]


def test_launchd_plist_program_arguments_parse_for_daemon_loop(tmp_path, monkeypatch):
    cfg = WikiConfig(vault_path=tmp_path)
    plist = plistlib.loads(generate_plist(cfg).encode("utf-8"))
    argv_after_module = plist["ProgramArguments"][3:]
    seen: dict[str, object] = {}

    async def fake_daemon_loop(config, *, interval_minutes, on_complete=None, max_ticks=None):
        seen["vault_path"] = config.vault_path
        seen["interval_minutes"] = interval_minutes
        seen["on_complete"] = on_complete
        seen["max_ticks"] = max_ticks

    monkeypatch.setattr(daemon_module, "daemon_loop", fake_daemon_loop)

    rc = daemon_module.main(argv_after_module)

    assert rc == 0
    assert seen["vault_path"] == tmp_path.resolve()


def test_launchd_install_reloads_canonical_plist(tmp_path, monkeypatch):
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist_path = launch_agents / "com.contents-hub.daemon.plist"
    plist_path.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    msg = install(WikiConfig(vault_path=tmp_path))

    assert plist_path.exists()
    assert ["launchctl", "unload", str(plist_path)] in calls
    assert ["launchctl", "load", str(plist_path)] in calls
    assert "com.contents-hub.daemon.plist" in msg


def test_launchd_status_reports_canonical_loaded_daemon(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd == ["launchctl", "list"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            "123\t0\tcom.contents-hub.daemon\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    msg = status()

    assert "Daemon is running" in msg
    assert "PID: 123" in msg


def test_launchd_uninstall_removes_canonical_plist(tmp_path, monkeypatch):
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist_path = launch_agents / "com.contents-hub.daemon.plist"
    plist_path.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    msg = uninstall()

    assert not plist_path.exists()
    assert ["launchctl", "unload", str(plist_path)] in calls
    assert "Daemon uninstalled" in msg
