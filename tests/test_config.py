"""配置层测试：查找优先级、JSONC 容错、脱敏、端点解析、模板自洽。

全部不联网、不需要任何密钥。

迁移说明：本文件来自已删除的 `mcp-ext/tests/`，而那个子包只是 `omni_media` 的
**重导出 shim**——它测的是一套更早的、更厚的配置层（`validate_file()` /
`EXAMPLE_CONFIG` / `write_example_config()` / 一整套 schema 校验）。这些符号在
`omni_media/config.py` 里**从来就不存在**（`HEAD` 版也没有），因此相关用例已删除。
第一阶段留下的两条 characterization 用例（非法值抛裸 `ValueError`、`base_url` 尾斜杠
不归一化）已在第二阶段按 `第二阶段待办.md` B1/B2/B5 修复，现改为断言修复后的行为。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni_media import config as cfgmod
from omni_media.config import ConfigError

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
    paths = cfgmod.candidate_paths()
    assert paths[0] == user_cfg, "安装态首选用户级配置"
    # 旧安装态目录仍作为回退候选保留：升级上来的用户不该因为改名而丢配置
    assert any("omni-media-ext" in str(p) for p in paths), paths


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
    # 提示的必须是**能照抄执行**的命令：`init` 是位置参数，写 `config --init` 会被 argparse 拒绝
    assert "config init" in message
    assert "config --init" not in message


def test_explicit_missing_config_does_not_fall_back(isolated_paths):
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data())  # 存在的默认配置
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config(repo / "nope.json")  # 显式指定但不存在
    assert "不存在" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 语法容错
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
    # json 模块的原文是 "line 3 column 3"，直接透传即可——用户要的是行号，不是中文行号
    assert "line 3" in str(excinfo.value)


def test_empty_config_file_rejected(isolated_paths):
    repo, _ = isolated_paths
    (repo / "config.json").write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigError):
        cfgmod.load_config()


def test_comment_keys_are_ignored(isolated_paths):
    repo, _ = isolated_paths
    data = make_config_data()
    data["_note"] = "说明"
    data["endpoints"]["gem"]["_note"] = "也是说明"
    write_config(repo / "config.json", data)
    assert cfgmod.load_config().resolve("gem").model == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# 就绪判定与脱敏
# ---------------------------------------------------------------------------

def test_placeholder_key_is_not_ready():
    """模板里的 `REPLACE_ME` 必须被判为「未就绪」——否则用户会以为装完就能用。"""
    config = cfgmod.load_config(cfgmod.example_config_path())
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
# 模板（唯一真相来源 = 打包内的 config.example.json 这个文件）
# ---------------------------------------------------------------------------

def test_packaged_template_is_loadable_and_self_consistent():
    """模板必须自己能加载（否则 `config init` 出来的文件一用就报错）。"""
    path = cfgmod.example_config_path()
    assert path.is_file(), f"缺少打包模板: {path}"
    assert path.name == cfgmod.EXAMPLE_FILENAME

    config = cfgmod.load_config(path)
    assert config.active == "gemini-flash"
    assert set(config.endpoints) == {"gemini-flash", "openai-audio", "whisper-compatible"}
    # 占位密钥 => 未就绪
    assert config.resolve().auth_ready() is False
    # 公开字典里不得出现占位原文
    assert "REPLACE_ME" not in json.dumps(config.to_public_dict(), ensure_ascii=False)


def test_template_is_the_only_source_no_code_duplicate():
    """模板只能是那个文件——代码里**不得**再有一份等价的常量（两份必然漂移）。"""
    assert not hasattr(cfgmod, "EXAMPLE_CONFIG")
    assert not hasattr(cfgmod, "example_config_text")
    assert not hasattr(cfgmod, "write_example_config")


# ---------------------------------------------------------------------------
# 第二阶段已关闭的缺口：B1（裸 ValueError）/ B2（无校验）/ B5（尾斜杠）
# ---------------------------------------------------------------------------

def test_invalid_defaults_value_raises_config_error_with_key(isolated_paths):
    """B1：非法 `defaults` 值必须是 `ConfigError`，且点明键名与收到的值。"""
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data(defaults={"slice_minutes": "soon"}))
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config()
    message = str(excinfo.value)
    assert "slice_minutes" in message, message
    assert "soon" in message, message


@pytest.mark.parametrize(
    "defaults",
    [
        {"slice_minutes": "soon"},   # 类型不可转
        {"slice_minutes": None},
        {"slice_minutes": 0},        # 必须为正
        {"slice_minutes": -3},
        {"max_payload_mb": 0},
        {"max_payload_mb": "many"},
        {"timeout_sec": -1},
        {"max_retries": -1},         # 允许 0，不允许负
    ],
)
def test_invalid_defaults_are_rejected(isolated_paths, defaults):
    """B2：`defaults` 数值必须为正（`max_retries` 允许 0），且错误带键名。"""
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data(defaults=defaults))
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config()
    assert next(iter(defaults)) in str(excinfo.value)


@pytest.mark.parametrize(
    "defaults, expected",
    [
        ({"slice_minutes": 10}, 10.0),
        ({"slice_minutes": 0.5}, 0.5),
        ({"max_retries": 0}, 0),
        ({"max_payload_mb": 1}, 1),
    ],
)
def test_valid_defaults_are_accepted(isolated_paths, defaults, expected):
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data(defaults=defaults))
    cfg = cfgmod.load_config()
    key = next(iter(defaults))
    assert getattr(cfg.defaults, key) == expected


def test_unknown_defaults_keys_are_ignored(isolated_paths):
    """实配里有历史遗留的 `max_concurrency`：未知键必须被忽略，不能因此拒绝加载。"""
    repo, _ = isolated_paths
    write_config(
        repo / "config.json",
        make_config_data(defaults={"slice_minutes": 10, "max_concurrency": 3}),
    )
    assert cfgmod.load_config().defaults.slice_minutes == 10.0


def test_protocol_whitelist_matches_registry():
    """`config.SUPPORTED_PROTOCOLS` 与 `providers.registry.ENDPOINT_MAP` 必须一致。

    两处名单若漂移，就会退化成「配置校验放行、实例化时才报未实现协议」的晚失败——
    正是 B2 想消掉的那种报错点离根因很远的情形。
    """
    from omni_media.providers.registry import ENDPOINT_MAP

    assert set(cfgmod.SUPPORTED_PROTOCOLS) == set(ENDPOINT_MAP)


@pytest.mark.parametrize(
    "endpoint, needle",
    [
        ({"protocol": "telepathy", "base_url": "https://x.test/v1", "model": "m"}, "protocol"),
        ({"protocol": "gemini", "base_url": "127.0.0.1:8045", "model": "m"}, "base_url"),
        ({"protocol": "gemini", "base_url": "", "model": "m"}, "base_url"),
        ({"protocol": "gemini", "base_url": "https://x.test/v1", "model": ""}, "model"),
        (
            {"protocol": "openai", "base_url": "https://x.test/v1", "model": "m",
             "openai_mode": "telepathy"},
            "openai_mode",
        ),
    ],
)
def test_invalid_endpoint_is_rejected(isolated_paths, endpoint, needle):
    """B2：协议白名单 / `base_url` 形如 URL / 必填字段 / `openai_mode` 全部在加载期拦下。"""
    repo, _ = isolated_paths
    write_config(
        repo / "config.json",
        make_config_data(active="bad", endpoints={"bad": endpoint}),
    )
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config()
    assert needle in str(excinfo.value)


def test_endpoint_must_be_object(isolated_paths):
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data(active="bad", endpoints={"bad": 42}))
    with pytest.raises(ConfigError) as excinfo:
        cfgmod.load_config()
    assert "bad" in str(excinfo.value)


def test_base_url_trailing_slash_is_normalized(isolated_paths):
    """B5：`base_url` 的尾斜杠必须归一化，否则各处直接拼接会拼出 `//`。"""
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data())
    # GEMINI_EP 的 base_url 故意带尾斜杠
    assert cfgmod.load_config().resolve("gem").base_url == (
        "https://generativelanguage.googleapis.com/v1beta"
    )


def test_active_pointing_nowhere_still_loads_for_config_show(isolated_paths):
    """`active` 故意不在加载期校验：`config show` 必须能把坏配置显示出来给人看。"""
    repo, _ = isolated_paths
    write_config(repo / "config.json", make_config_data(active="ghost"))
    cfg = cfgmod.load_config()
    assert cfg.active == "ghost"
    with pytest.raises(ConfigError) as excinfo:
        cfg.resolve()
    assert "ghost" in str(excinfo.value)
