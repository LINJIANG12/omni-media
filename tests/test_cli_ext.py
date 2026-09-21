"""CLI 治理层测试：config 子命令、宿主接入/撤销、status、inspect。

这些命令是用户唯一的配置入口，出错会直接影响可用性，所以即使不打网络也要覆盖。
宿主配置一律写到 tmp_path（用 `--config-path` 指定），**绝不碰真实宿主配置或用户目录**。

迁移说明：统一包把 `config` 收敛成 `{init,locate,show}` 三个动作（旧的
`--validate` / `--path` / `--example` 旗标已不存在，校验并入 `show`/`status`），
并把 `apply` 的宿主配置旗标改名为 `--config-path`、删掉了 `--server-config`
与 `--skill-dir`（各宿主适配器已收敛为一个泛型 `BaseHostAdapter`，不再有「随包技能文件」）。
本文件按**当前**surface 重写。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni_media import cli
from omni_media.config import example_config_path

# 注册名 omni-media-ext ⇒ 入口必须把 --mode ext 一起下发，工具面才只有 read_media
_EXT_STDIO_ARGS = ["-m", "omni_media.server", "--mode", "ext"]


@pytest.fixture()
def host_config(tmp_path: Path) -> Path:
    return tmp_path / "host" / "config.json"


@pytest.fixture()
def template() -> dict:
    """打包内的配置模板（唯一真相来源是 `omni_media/config.example.json` 这个文件）。"""
    path = example_config_path()
    assert path.is_file(), f"模板缺失: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# config 子命令
# ---------------------------------------------------------------------------

def test_config_init_writes_default_path_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    """`config init` 写到 `default_config_path()`，且**默认拒绝覆盖**已存在的配置。

    拒绝覆盖这条很关键：默认路径在源码检出里就是仓库根的 `config.json`，
    而那是**活的密钥文件**——无条件覆盖等于静默销毁用户的 api_key。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "config.json"
    monkeypatch.setattr(cli, "default_config_path", lambda: target)

    assert cli.main(["config", "init"]) == 0
    assert target.is_file(), "应写到 default_config_path() 指定的位置"

    # 已存在：默认拒绝，--force 才覆盖
    assert cli.main(["config", "init"]) == 1
    assert "--force" in capsys.readouterr().out
    assert cli.main(["config", "init", "--force"]) == 0
    capsys.readouterr()


def test_config_init_content_is_the_packaged_template(tmp_path, monkeypatch, template, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "config.json"
    monkeypatch.setattr(cli, "default_config_path", lambda: target)

    assert cli.main(["config", "init"]) == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    # 逐字一致：模板与生成物之间不允许有第二份真相
    assert written == template
    out = capsys.readouterr().out
    assert "模板来源" in out


def test_config_init_force_backs_up_the_live_config(tmp_path, monkeypatch, template, capsys):
    """`--force` 覆盖前必须先备份。

    第二阶段 A11：`--force` 是**就地覆盖**，而这份文件可能带着真实 `api_key`
    （实测已造成一次事故，只能靠手工留存的副本恢复）。备份名必须能被 .gitignore 挡住，
    否则明文密钥会随备份进版本库。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "config.json"
    live = {"active": "gem", "endpoints": {"gem": {"api_key": "sk-真密钥-不要外泄"}}}
    target.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cli, "default_config_path", lambda: target)

    assert cli.main(["config", "init", "--force"]) == 0

    backups = sorted(repo.glob("config.json.bak.*"))
    assert len(backups) == 1, [p.name for p in repo.iterdir()]
    # 备份必须留着**原内容**，否则备份没有意义
    assert json.loads(backups[0].read_text(encoding="utf-8")) == live
    # 目标文件确实被模板替换了
    assert json.loads(target.read_text(encoding="utf-8")) == template
    assert str(backups[0]) in capsys.readouterr().out


def test_config_init_backups_are_gitignored():
    """自动备份含明文密钥，必须被 .gitignore 挡住（A11 的安全前提）。"""
    gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "config.json.bak.*" in gitignore, gitignore


def test_config_init_without_force_leaves_no_backup(tmp_path, monkeypatch, capsys):
    """默认拒绝覆盖时不得留下备份文件（没覆盖就没有要备份的东西）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "config.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "default_config_path", lambda: target)

    assert cli.main(["config", "init"]) == 1
    assert not list(repo.glob("*.bak.*")), [p.name for p in repo.iterdir()]


def test_config_locate_prints_resolved_path(tmp_path, template, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["config", "locate", "--config", str(cfg)]) == 0
    assert str(cfg) in capsys.readouterr().out


def test_config_locate_fails_when_nothing_found(tmp_path, capsys):
    missing = tmp_path / "nope.json"
    assert cli.main(["config", "locate", "--config", str(missing)]) == 1
    err = capsys.readouterr().err
    assert "未定位到有效配置" in err
    assert str(missing) in err, "候选路径必须列出来，否则用户无从下手"


def test_config_show_masks_secrets(tmp_path, template, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["config", "show", "--config", str(cfg)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown["endpoints"]) == {"gemini-flash", "openai-audio", "whisper-compatible"}
    assert shown["endpoints"]["gemini-flash"]["api_key"] == "***"
    assert "REPLACE_ME" not in json.dumps(shown)
    assert shown["config_file"] == str(cfg)


def test_config_show_fails_on_unusable_config(tmp_path, capsys):
    assert cli.main(["config", "show", "--config", str(tmp_path / "nope.json")]) == 1
    assert "配置错误" in capsys.readouterr().err


def test_print_config_reports_ext_registration_name_and_mode(capsys):
    """ext 入口的注册名与 `--mode` 必须成对出现，否则宿主看到的是 all 模式。"""
    assert cli.main_ext(["print-config"]) == 0
    data = json.loads(capsys.readouterr().out)
    entry = data["mcpServers"]["omni-media-ext"]
    assert entry["args"] == _EXT_STDIO_ARGS
    assert set(entry["env"]) == {"PYTHONPATH"}
    assert entry["command"]


# ---------------------------------------------------------------------------
# apply / unapply
# ---------------------------------------------------------------------------

def test_apply_and_unapply_dsh(host_config: Path, capsys):
    assert cli.main(
        ["apply", "--target", "dsh", "--config-path", str(host_config), "--yes"]
    ) == 0
    capsys.readouterr()

    data = json.loads(host_config.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["omni-media"]
    assert entry["args"] == ["-m", "omni_media.server", "--mode", "native"]
    # 只注入 PYTHONPATH：不带凭证，也不带本版本已不存在的旋钮
    assert set(entry["env"]) == {"PYTHONPATH"}

    # 二次 apply 幂等
    assert cli.main(
        ["apply", "--target", "dsh", "--config-path", str(host_config), "--yes"]
    ) == 0
    assert "无需修改" in capsys.readouterr().out

    assert cli.main(
        ["unapply", "--target", "dsh", "--config-path", str(host_config)]
    ) == 0
    capsys.readouterr()
    data = json.loads(host_config.read_text(encoding="utf-8"))
    assert "omni-media" not in data["mcpServers"]


def test_apply_unknown_target_is_rejected_with_actionable_message(host_config: Path, capsys):
    """未知宿主必须给出可照做的错误、退出码 1，且**不写任何文件**。

    第二阶段 B3：以前 `get_adapter` 抛出的 `ValueError` 没被 CLI 兜住，用户打错一个
    宿主名就看到栈回溯；现在改成打印消息并 `return 1`。
    """
    assert cli.main(
        ["apply", "--target", "ghost", "--config-path", str(host_config), "--yes"]
    ) == 1
    err = capsys.readouterr().err
    assert "未知宿主目标" in err
    assert "ghost" in err
    # 消息必须列出合法取值，用户才知道该改成什么
    assert "dsh" in err
    assert not host_config.exists()


def test_unapply_unknown_target_is_rejected_with_actionable_message(host_config: Path, capsys):
    """B3：`unapply` 同样要兜住，不能只修 `apply` 那一半。"""
    assert cli.main(
        ["unapply", "--target", "ghost", "--config-path", str(host_config)]
    ) == 1
    err = capsys.readouterr().err
    assert "未知宿主目标" in err and "ghost" in err
    assert not host_config.exists()


# ---------------------------------------------------------------------------
# status / inspect
# ---------------------------------------------------------------------------

def test_status_runs_with_valid_config(tmp_path, template, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["status", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "[PASS] 配置文件" in out
    assert "激活端点" in out
    assert "gemini-flash" in out and "whisper-compatible" in out
    assert "REPLACE_ME" not in out             # 密钥只以掩码形式出现


def test_status_survives_missing_config(tmp_path, capsys):
    """配置缺失时 status 仍应正常退出，并指出是哪个 --config 不存在。"""
    missing = tmp_path / "nope.json"
    assert cli.main(["status", "--config", str(missing)]) == 0
    out = capsys.readouterr().out
    assert "指定的配置文件不存在" in out
    assert str(missing) in out


def test_inspect_real_media(audio_m4a: Path, capsys):
    assert cli.main(["inspect", str(audio_m4a)]) == 0
    out = capsys.readouterr().out
    assert "媒体文件探测报告" in out
    assert "Qwen" not in out and "DeepSeek" not in out


def test_inspect_missing_file_fails(tmp_path, capsys):
    assert cli.main(["inspect", str(tmp_path / "nope.m4a")]) == 1
    capsys.readouterr()
