"""CLI 治理层测试：config 子命令、宿主接入/撤销、status。

这些命令是用户唯一的配置入口，出错会直接影响可用性，所以即使不打网络也要覆盖。
宿主配置一律写到 tmp_path（用 `--config` 指定），**绝不碰真实宿主配置或用户目录**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni_media_ext import cli
from omni_media_ext.config import EXAMPLE_CONFIG


@pytest.fixture()
def host_config(tmp_path: Path) -> Path:
    return tmp_path / "host" / "config.json"


# ---------------------------------------------------------------------------
# config 子命令
# ---------------------------------------------------------------------------

def test_config_init_writes_repo_template(tmp_path, monkeypatch, capsys):
    """`config --init`（不带值）必须写到仓库根，而不是当前工作目录。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "default_config_path", lambda: repo / "config.json")

    assert cli.main(["config", "--init", "--config", str(tmp_path / "none.json")]) == 0
    written = repo / "config.json"
    assert written.is_file(), "应写到 <repo_root>/config.json"

    # 已存在时拒绝覆盖；--force 才覆盖
    assert cli.main(["config", "--init", "--config", str(tmp_path / "none.json")]) == 1
    assert cli.main(["config", "--init", "--force", "--config", str(tmp_path / "none.json")]) == 0
    capsys.readouterr()


def test_config_init_writes_explicit_path(tmp_path, capsys):
    target = tmp_path / "custom" / "my.json"
    assert cli.main(["config", "--init", str(target)]) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == EXAMPLE_CONFIG
    capsys.readouterr()


def test_config_validate_path_show(tmp_path, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(EXAMPLE_CONFIG, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["config", "--validate", "--config", str(cfg)]) == 0
    assert "校验通过" in capsys.readouterr().out

    assert cli.main(["config", "--path", "--config", str(cfg)]) == 0
    assert str(cfg) in capsys.readouterr().out

    assert cli.main(["config", "--show", "--config", str(cfg)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown["endpoints"]) == {"gemini-flash", "openai-audio", "whisper-compatible"}
    assert shown["endpoints"]["gemini-flash"]["api_key"] == "***"  # REPLACE_ME 被掩码
    assert "REPLACE_ME" not in json.dumps(shown)


def test_config_validate_reports_and_fails(tmp_path, capsys):
    broken = tmp_path / "config.json"
    broken.write_text('{"endpoints": {}}', encoding="utf-8")
    assert cli.main(["config", "--validate", "--config", str(broken)]) == 1
    assert "校验未通过" in capsys.readouterr().out


def test_config_path_targets_default_when_missing(tmp_path, capsys):
    missing = tmp_path / "nope.json"
    assert cli.main(["config", "--path", "--config", str(missing)]) == 1
    assert str(missing) in capsys.readouterr().out


def test_config_example_prints_template(capsys):
    assert cli.main(["config", "--example"]) == 0
    assert json.loads(capsys.readouterr().out) == EXAMPLE_CONFIG


def test_print_config_is_host_neutral_json(capsys):
    assert cli.main(["print-config"]) == 0
    data = json.loads(capsys.readouterr().out)
    entry = data["mcpServers"]["omni-media-ext"]
    assert entry["args"] == ["-m", "omni_media_ext.server"]
    assert set(entry["env"]) == {"PYTHONPATH"}
    assert entry["command"]


def test_print_config_can_include_server_config(tmp_path, capsys):
    cfg = tmp_path / "service.json"
    assert cli.main(["print-config", "--config", str(cfg)]) == 0
    data = json.loads(capsys.readouterr().out)
    entry = data["mcpServers"]["omni-media-ext"]
    assert entry["args"] == [
        "-m", "omni_media_ext.server", "--config", str(cfg.resolve()),
    ]


# ---------------------------------------------------------------------------
# apply / unapply
# ---------------------------------------------------------------------------

def test_apply_and_unapply_dsh(host_config: Path, capsys):
    assert cli.main(["apply", "--target", "dsh", "--config", str(host_config), "--yes"]) == 0
    capsys.readouterr()

    data = json.loads(host_config.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["omni-media-ext"]
    assert entry["args"][:2] == ["-m", "omni_media_ext.server"]
    # 只注入 PYTHONPATH：不带凭证，也不带本版本已不存在的旋钮
    assert set(entry["env"]) == {"PYTHONPATH"}

    # 二次 apply 幂等
    assert cli.main(["apply", "--target", "dsh", "--config", str(host_config), "--yes"]) == 0
    assert "无需修改" in capsys.readouterr().out

    assert cli.main(["unapply", "--target", "dsh", "--config", str(host_config), "--yes"]) == 0
    capsys.readouterr()
    data = json.loads(host_config.read_text(encoding="utf-8"))
    assert "omni-media-ext" not in data["mcpServers"]


def test_apply_server_config_injected(host_config: Path, tmp_path: Path, capsys):
    server_cfg = tmp_path / "server-config.json"
    assert cli.main([
        "apply", "--target", "dsh", "--config", str(host_config),
        "--server-config", str(server_cfg), "--yes",
    ]) == 0
    capsys.readouterr()

    entry = json.loads(host_config.read_text(encoding="utf-8"))["mcpServers"]["omni-media-ext"]
    assert entry["args"] == [
        "-m", "omni_media_ext.server", "--config", str(server_cfg.resolve()),
    ]


def test_apply_skill_dir_is_honored_and_user_dir_untouched(host_config: Path, tmp_path: Path, capsys):
    """`--skill-dir` 必须生效：codex 适配器默认会写用户级 ~/.agents/skills。"""
    skill_dir = tmp_path / "project-skills"
    assert cli.main([
        "apply", "--target", "codex", "--config", str(host_config),
        "--skill-dir", str(skill_dir), "--yes",
    ]) == 0
    capsys.readouterr()

    assert (skill_dir / "SKILL.md").is_file(), "技能应写到 --skill-dir 指定的目录"
    user_skill = Path.home() / ".agents" / "skills" / "omni-media-ext" / "SKILL.md"
    assert not user_skill.exists(), "不得写到用户级目录"
    assert json.loads(host_config.read_text(encoding="utf-8"))["mcpServers"]["omni-media-ext"]


def test_codex_apply_rejects_missing_bundled_skill(tmp_path: Path, monkeypatch):
    from omni_media_ext.adapters.codex import CodexAdapter

    adapter = CodexAdapter(
        custom_config_path=tmp_path / "codex.json",
        custom_skill_dir=tmp_path / "skills",
    )
    monkeypatch.setattr(adapter, "get_bundled_skill_path", lambda: tmp_path / "missing.md")
    ok, message = adapter.apply()
    assert ok is False
    assert "缺失" in message
    assert not (tmp_path / "codex.json").exists()


def test_apply_unknown_target_fails(host_config: Path, capsys):
    assert cli.main(["apply", "--target", "ghost", "--config", str(host_config), "--yes"]) == 1
    assert "未知目标" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# status / inspect
# ---------------------------------------------------------------------------

def test_status_runs_with_valid_config(tmp_path, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(EXAMPLE_CONFIG, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["status", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "生效文件" in out
    assert "gemini-flash" in out and "whisper-compatible" in out
    assert "REPLACE_ME" not in out             # 密钥只以掩码形式出现
    assert "支持的协议: ['gemini', 'openai']" in out


def test_status_survives_missing_config(tmp_path, capsys):
    """配置缺失时 status 仍应正常退出，并把候选路径全列出来。"""
    assert cli.main(["status", "--config", str(tmp_path / "nope.json")]) == 0
    out = capsys.readouterr().out
    assert "配置不可用" in out
    assert "跳过：配置未就绪" in out


def test_inspect_real_media(audio_m4a: Path, capsys):
    assert cli.main(["inspect", str(audio_m4a)]) == 0
    out = capsys.readouterr().out
    assert "媒体文件探测报告" in out
    assert "Qwen" not in out and "DeepSeek" not in out


def test_inspect_missing_file_fails(tmp_path, capsys):
    assert cli.main(["inspect", str(tmp_path / "nope.m4a")]) == 1
    capsys.readouterr()
