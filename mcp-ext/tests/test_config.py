"""配置层测试：查找优先级、schema 校验、脱敏、端点解析。

全部不联网、不需要任何密钥。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni_media_ext import config as cfgmod
from omni_media_ext.config import ConfigError

GEMINI_EP = {
    "protocol": "gemini",
    "base_url": "https://generativelanguage.googleapis.com/v1beta/",  # 故意带尾斜杠
    "api_key": "gemini-secret-key-1234567890",
    "model": "gemini-2.5-flash",
}
OPENAI_EP = {
    "protocol": "openai",
    "openai_mode": "transcriptions",
    "base_url": "https://api.openai.com/v1",
    "api_key": "openai-secret-key-0987654321",
    "model": "whisper-1",
    "text_model": "gpt-4o-mini",
    "language": "zh",
}


def make_config_data(**overrides):
    data = {"active": "gem", "endpoints": {"gem": dict(GEMINI_EP), "oai": dict(OPENAI_EP)}}
    data.update(overrides)
    return data


def write_config(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """把仓库根与用户级路径都重定向到 tmp_path，避免碰到真实文件。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    home_cfg = tmp_path / "home" / "config.json"
    monkeypatch.setattr(cfgmod, "repo_root", lambda: repo)
    monkeypatch.setattr(cfgmod, "user_config_path", lambda: home_cfg)
    monkeypatch.setattr(cfgmod, "is_source_checkout", lambda: True)
    return repo, home_cfg


# ---------------------------------------------------------------------------
# 查找优先级
# ---------------------------------------------------------------------------

def test_installed_package_defaults_to_user_config(tmp_path, monkeypatch):
    user_cfg = tmp_path / "home" / "config.json"
    monkeypatch.setattr(cfgmod, "is_source_checkout", lambda: False)
    monkeypatch.setattr(cfgmod, "user_config_path", lambda: user_cfg)
    assert cfgmod.default_config_path() == user_cfg
    assert cfgmod.candidate_paths() == [user_cfg]


def test_precedence_explicit_wins(isolated_paths):
    repo, home_cfg = isolated_paths
    write_config(repo / "config.json", make_config_data(active="gem"))
    write_config(home_cfg, make_config_data(active="oai"))
    explicit = write_config(repo / "custom.json", make_config_data(active="oai"))

    assert cfgmod.load_config().active == "gem"          # 仓库根优先于用户级
    assert cfgmod.load_config(explicit).active == "oai"  # --config 优先于仓库根


def test_precedence_user_level_when_repo_missing(isolated_paths):
    _, home_cfg = isolated_paths
    write_config(home_cfg, make_config_data(active="oai"))
    assert cfgmod.load_config().active == "oai"


def test_missing_config_lists_all_candidates(isolated_paths):
    repo, home_cfg = isolated_paths
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config()
    message = str(excinfo.value)
    assert str(repo / "config.json") in message
    assert str(home_cfg) in message
    assert "config --init" in message


def test_explicit_missing_config_does_not_fall_back(isolated_paths):
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data())  # 存在的默认配置
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config(repo / "nope.json")  # 显式指定但不存在
    assert "不存在" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 语法与语义校验
# ---------------------------------------------------------------------------

def test_jsonc_comments_are_tolerated(isolated_paths):
    repo, _ = isolated_paths
    (repo / "config.json").write_text(
        """
        {
          // 单行注释
          "active": "gem",   /* 行尾块注释 */
          "endpoints": {
            "gem": {
              "protocol": "gemini",
              "base_url": "http://127.0.0.1:1234/v1beta",  // 注释里含有 // 的假象
              "api_key": "k-1234567890abcdef",
              "model": "m"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    config = cfgmod.load_config()
    assert config.resolve().base_url == "http://127.0.0.1:1234/v1beta"


def test_broken_json_reports_line_and_column(isolated_paths):
    repo, _ = isolated_paths
    (repo / "config.json").write_text('{\n  "active": "gem",\n  oops\n}', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config()
    assert "第 3 行" in str(excinfo.value)


def test_empty_config_file_rejected(isolated_paths):
    repo, _ = isolated_paths
    (repo / "config.json").write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigError):
        cfgmod.load_config()


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.update(activ="gem"), "顶层存在未知字段"),
        (lambda d: d["endpoints"]["gem"].update(apikey="x"), "未知字段"),
        (lambda d: d["endpoints"]["gem"].update(protocol="anthropic"), "protocol"),
        (lambda d: d["endpoints"]["gem"].update(base_url="ftp://x"), "http://"),
        (lambda d: d["endpoints"]["gem"].update(base_url=""), "base_url"),
        (lambda d: d["endpoints"]["gem"].update(model=""), "model"),
        (lambda d: d["endpoints"].update(bad={"protocol": "gemini"}), "base_url"),
        (lambda d: d.update(active="nope"), "不在 endpoints"),
        (lambda d: d.update(endpoints={}), "endpoints"),
        (lambda d: d.update(defaults={"slice_minutes": 0}), "slice_minutes"),
        (lambda d: d.update(defaults={"max_payload_mb": 9999}), "max_payload_mb"),
        (lambda d: d.update(defaults={"timeout_sec": "soon"}), "必须是数字"),
        (lambda d: d.update(defaults={"nope": 1}), "未知字段"),
        (lambda d: d["endpoints"]["oai"].update(openai_mode="websocket"), "openai_mode"),
        (lambda d: d["endpoints"]["oai"].update(audio_format="ogg"), "audio_format"),
    ],
)
def test_validation_rejects_bad_config(isolated_paths, mutate, needle):
    repo, _ = isolated_paths
    data = make_config_data()
    mutate(data)
    write_config(repo / "config.json", data)
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config()
    assert needle in str(excinfo.value)


def test_comment_keys_are_ignored(isolated_paths):
    repo, _ = isolated_paths
    data = make_config_data()
    data["_note"] = "说明"
    data["endpoints"]["gem"]["_note"] = "也是说明"
    write_config(repo / "config.json", data)
    assert cfgmod.load_config().resolve("gem").model == "gemini-2.5-flash"


def test_base_url_trailing_slash_normalized(isolated_paths):
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data())
    assert cfgmod.load_config().resolve("gem").base_url.endswith("/v1beta")


def test_validate_file_returns_problems_without_raising(isolated_paths):
    repo, _ = isolated_paths
    path, problems = cfgmod.validate_file()
    assert path is None and problems

    write_config(repo / "config.json", make_config_data())
    path, problems = cfgmod.validate_file()
    assert path is not None and problems == []


# ---------------------------------------------------------------------------
# 就绪判定与脱敏
# ---------------------------------------------------------------------------

def test_placeholder_key_is_not_ready(isolated_paths):
    repo, _ = isolated_paths
    write_config(repo / "config.json", cfgmod.EXAMPLE_CONFIG)
    config = cfgmod.load_config()
    assert config.resolve().auth_ready() is False
    with pytest.raises(ConfigError) as excinfo:
        config.resolve().require_ready()
    assert "REPLACE_ME" in str(excinfo.value) or "api_key" in str(excinfo.value)


def test_auth_header_makes_keyless_endpoint_ready(isolated_paths):
    repo, _ = isolated_paths
    data = make_config_data()
    data["endpoints"]["gem"]["api_key"] = ""
    data["endpoints"]["gem"]["headers"] = {"Authorization": "Bearer gateway-token"}
    write_config(repo / "config.json", data)
    endpoint = cfgmod.load_config().resolve("gem")
    assert endpoint.auth_ready() is True
    endpoint.require_ready()  # 不抛


def test_masking_never_leaks_full_key(isolated_paths):
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data())
    config = cfgmod.load_config()
    dumped = json.dumps(config.to_public_dict(), ensure_ascii=False)

    assert "gemini-secret-key-1234567890" not in dumped
    assert "openai-secret-key-0987654321" not in dumped
    assert "gemi...7890" in dumped

    # 短密钥直接打星
    assert cfgmod.mask_secret("short") == "***"
    assert cfgmod.mask_secret("") == "(未配置)"


# ---------------------------------------------------------------------------
# 端点解析
# ---------------------------------------------------------------------------

def test_resolve_endpoint_variants(isolated_paths):
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data())
    config = cfgmod.load_config()

    assert config.resolve("oai").model == "whisper-1"   # 按名取
    assert config.resolve().name == "gem"               # 取 active
    assert config.resolve("").name == "gem"             # 空串等同未指定

    with pytest.raises(ConfigError) as excinfo:
        config.resolve("ghost")
    assert "可用端点" in str(excinfo.value)


def test_resolve_falls_back_to_first_when_active_blank(isolated_paths):
    repo, _ = isolated_paths
    data = make_config_data()
    data["active"] = ""
    write_config(repo / "config.json", data)
    config = cfgmod.load_config()
    assert config.resolve().name == "gem"  # 字典序第一个


def test_openai_mode_defaults_to_chat(isolated_paths):
    repo, _ = isolated_paths
    data = make_config_data()
    data["endpoints"]["oai"].pop("openai_mode")
    write_config(repo / "config.json", data)
    endpoint = cfgmod.load_config().resolve("oai")
    assert endpoint.openai_mode == "chat"
    assert endpoint.audio_format == "mp3"


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

def test_example_template_is_self_consistent(isolated_paths, tmp_path):
    """模板本身必须能通过校验（否则 --init 出来的文件一用就报错）。"""
    target = cfgmod.write_example_config(tmp_path / "config.json")
    config = cfgmod.load_config(target)
    assert config.active == "gemini-flash"
    assert set(config.endpoints) == {"gemini-flash", "openai-audio", "whisper-compatible"}

    # 目标已存在时必须拒绝覆盖，除非显式 force
    with pytest.raises(ConfigError):
        cfgmod.write_example_config(target)
    assert cfgmod.write_example_config(target, force=True) == target


def test_shipped_example_file_matches_code_template():
    """仓库里的 config.example.json 必须与代码里的模板完全一致（防两处漂移）。"""
    shipped = cfgmod.repo_root() / cfgmod.EXAMPLE_FILENAME
    assert shipped.is_file(), f"缺少模板文件: {shipped}"
    assert shipped.read_text(encoding="utf-8") == cfgmod.example_config_text()

    # 代码里的模板解析为 Python 对象后应与 --example 输出等价
    assert json.loads(cfgmod.example_config_text()) == cfgmod.EXAMPLE_CONFIG
