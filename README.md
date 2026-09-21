# 🎛️ OmniMedia — 面向 AI Agent 的音视频听读 MCP 服务

> **一个包，两条听音通道**：实现只有 `omni_media/` 一份，由启动参数 `--mode` 决定暴露哪条通道。
> 宿主自己听得见 → `--mode native`，服务只切音频（零凭证）；宿主只有文本能力 → `--mode ext`，由服务调外部模型代读（配置驱动）。
> 两条通道**分页契约同构**——同一套状态注释、同一套字段、同一套续读循环，切换只换注册名与工具名。

[![M8ven Score](https://m8ven.ai/badge/mcp/linjiang12-omni-media-10kel3?v=4b27fe05e0f109966d63ae1f07557b12)](https://m8ven.ai/mcp/linjiang12-omni-media-10kel3)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=flat-square)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat-square)](#)
[![MCP: >=2.1.0,<3](https://img.shields.io/badge/MCP-%3E%3D2.1.0%2C%3C3-blue.svg?style=flat-square)](#)
[![配套技能: video2book](https://img.shields.io/badge/配套技能-video2book-111827?style=flat-square)](https://github.com/LINJIANG12/video2book)

[简体中文](README.md) · [English](README.en.md)

---

## 🎧 两条通道怎么选

| | 原生听音 `--mode native` | 外部模型代读 `--mode ext` |
| :--- | :--- | :--- |
| 宿主注册名 | `omni-media` | `omni-media-ext` |
| 谁来"听" | **宿主对话模型**的多模态内核（服务只给切片） | **服务自己**调外部模型（Gemini / OpenAI 协议端点） |
| 工具面 | `inspect_media` + `read_audio` | `inspect_media` + `read_media` |
| 凭证 | **零 API Key** | `config.json` 里的端点与 `api_key` |
| 网络 | 全程不出网 | 每次调用把音频切片发给所配置端点 |
| 适合 | 宿主支持音频模态（延迟最低、零成本） | 任何能读文本的 Agent |

两条通道可同时挂载：注册名不同，互不覆盖。`--mode all` 会同时注册两类工具，**只建议人工排查时用**——宿主是按注册名区分通道的，`all` 会让"装的是哪条通道"在工具列表上完全看不出来。

## 📦 安装

**Python ≥ 3.10**；Python 依赖只有 `mcp>=2.1.0,<3`；音频切片另需系统 `ffmpeg`（`ffprobe` 缺失时自动降级为解析 `ffmpeg -i`）。正式分发渠道是 GitHub，不发布 PyPI wheel。

```bash
cd omni-media
pip install -e .            # 运行时
pip install -e ".[dev]"     # 另装 pytest（跑测试时用）
```

```text
omni-media/
├── omni_media/              # 唯一实现包（server / cli / config / adapters / providers / core）
│   └── config.example.json  # 唯一的配置模板（随包分发）
├── tests/                   # 自动化测试（进程内仿真端点，不联网、不需要密钥）
├── examples/                # 手动验证脚本：真端点跑一次 read_audio / read_media
├── selfcheck.py             # 5 项全量自检
└── pyproject.toml           # 依赖与 4 个启动器声明
```

## 🚀 四个启动器

四个入口绑定的都是同一个包 `omni_media`，没有第二份实现：

| 启动器 | 实现入口 | 模式 | 用途 |
| :--- | :--- | :--- | :--- |
| `omni-media-mcp` | `omni_media.server:main_native` | 固定 `native` | 宿主自带音频模态时挂这个（只 `read_audio`） |
| `omni-media-ext-mcp` | `omni_media.server:main_ext` | 固定 `ext` | 纯文本宿主挂这个（只 `read_media`） |
| `omni-media` | `omni_media.cli:main` | 管理 CLI | 注册名报 `omni-media`：`status` / `apply` / `print-config` / `config` / `serve` |
| `omni-media-ext` | `omni_media.cli:main_ext` | 管理 CLI | 同上，注册名报 `omni-media-ext` |

前两个就是 MCP stdio 服务本身（模式焊死在入口里，不需要额外参数）；后两个是管理 CLI，彼此只差"向宿主报告哪个注册名"，所以 `print-config` / `apply` 生成的配置会自动带上正确的 `--mode`。

### 挂载示例

**宿主注册名决定工具面**：`omni-media` → 只暴露 `read_audio`（零凭证）；`omni-media-ext` → 只暴露 `read_media`（需要 `config.json` 里的 `api_key`）。`args` 必须带 `--mode`：

```json
{
  "mcpServers": {
    "omni-media": {
      "command": "python",
      "args": ["-m", "omni_media.server", "--mode", "native"],
      "env": { "PYTHONPATH": "/绝对路径/omni-media" }
    },
    "omni-media-ext": {
      "command": "python",
      "args": ["-m", "omni_media.server", "--mode", "ext"],
      "env": { "PYTHONPATH": "/绝对路径/omni-media" }
    }
  }
}
```

`command` 建议填解释器绝对路径（GUI 宿主未必继承 `PATH`）。不想手写就用 `omni-media print-config`（原生）和 `omni-media-ext print-config`（外部模型）：输出的 `command` 是当前解释器的绝对路径、`PYTHONPATH` 是仓库绝对路径，粘贴即可。

已 `pip install -e .` 时也可以用控制台脚本（模式已内置，无需 `--mode`）：

```json
{
  "mcpServers": {
    "omni-media": { "command": "omni-media-mcp", "args": [] },
    "omni-media-ext": { "command": "omni-media-ext-mcp", "args": [] }
  }
}
```

自动接入已知宿主（`--target` 取值 `antigravity` / `codex` / `dsh` / `opencode` / `zcode` / `all`，写入前先打印 Diff 预览）：

```bash
omni-media status                      # 环境 + 配置 + 各宿主挂载状态与实际配置路径
omni-media apply --target codex        # 接入宿主（原生通道）
omni-media-ext apply --target codex    # 接入宿主（外部模型通道）
omni-media unapply --target codex      # 干净撤销，零残留
```

> `omni-media serve [--mode native|ext|all]` 也能直接起服务，但 `serve` 的默认模式是 `all`；挂载请用上面带 `--mode` 的写法或两个专用 MCP 入口。

## 🤖 AI 首次使用配置指南 (First-Time Setup for AI)

> 💡 **写给正在接管或执行转录任务的 AI Agent**：
> 如果你是首次使用本 MCP 服务处理音视频转录，请务必先核对以下关键配置，避免由于缺漏配置或超时截断导致转录失败：
>
> 1. **系统环境依赖**：
>    - 必须确认宿主系统已安装 `ffmpeg` 与 `ffprobe` 并在环境变量 `PATH` 中（用于音视频探测与切片）：
>      ```bash
>      ffmpeg -version
>      ffprobe -version
>      ```
> 2. **初始化与填写配置（仅 `--mode ext` 外部模型通道必需）**：
>    - 执行命令生成初始配置文件：
>      ```bash
>      omni-media config init
>      ```
>      （源码检出位于仓库根 `config.json`；pip 安装位于 `~/.omni-media/config.json`）
>    - 编辑该 `config.json`，在 `endpoints` 中填入可用多模态模型端点：
>      - `protocol`: `openai`（主流网关反代均支持 OpenAI 兼容格式）或 `gemini`；
>      - `base_url`: 模型 API 端点地址；
>      - `api_key`: 有效的 API 密钥；
>      - `model`: 具备原生音频识别能力的多模态模型（如 `gemini-2.5-flash`、`gemini-1.5-flash` 或 `gpt-4o-audio-preview` 等）。
> 3. **核对推荐性能参数**：
>    - `slice_minutes`: 保持默认 **30** 分钟。现代多模态大模型单次输出上限可达 64k tokens，30 分钟音频转录出的字数（约 1~1.6 万字）完全在预算内，能将转录轮次和网络开销压缩至最低；
>    - `max_payload_mb`: 保持默认 **35** MB（满足 30 分钟音频单片大小，避免超限）；
>    - `max_concurrency`: 推荐默认 **5**。5 并发可在 1 分钟左右完成 150 分钟长音频转录，同时不会触发模型提供商的 RPM 限流（429）。
> 4. **验证与全量自检**：
>    - 验证当前配置状态：
>      ```bash
>      omni-media status
>      omni-media config show
>      ```
>    - 执行自动化自检确保 100% 通过：
>      ```bash
>      python selfcheck.py
>      ```

## 🔑 配置（只有 `--mode ext` 需要）

```bash
omni-media config init      # 由模板生成 config.json（已存在则拒绝；加 --force 覆盖，覆盖前自动备份为 config.json.bak.<时间戳>）
omni-media config locate    # 打印实际生效的配置文件路径
omni-media config show      # 打印脱敏公开视图（api_key 只留首尾）
python -c "import json;print(json.load(open('omni_media/config.example.json',encoding='utf-8'))['active'])"
```

- **模板只有一份**：`omni_media/config.example.json`，随包分发，`config init` 读的就是它（源码检出与 pip 安装两种情况都读得到）。
- **写入位置**：源码检出 → 仓库根 `config.json`；pip 安装 → `~/.omni-media/config.json`（若存在历史遗留的 `~/.omni-media-ext/config.json`，则回退读它）。
- 键：`active`（默认端点名）、`defaults`（`slice_minutes` 默认 30 / `max_payload_mb` 默认 35 / `timeout_sec` / `max_retries` / `max_concurrency` 默认 5）、`endpoints.<名字>`（`protocol` / `base_url` / `api_key` / `model`；OpenAI 协议另有 `openai_mode` / `audio_format` / `text_model` / `language`）。
- **切片与并发说明**：
  - `slice_minutes`：默认切片时长（推荐 30 分钟，大幅减少分卷轮次与吞吐开销）；
  - `max_payload_mb`：单片媒体体积上限（30 分钟音频推荐 35MB）；
  - `max_concurrency`：客户端建议调用并发（默认 5）；底层 FFmpeg 转码切片并发由 `MAX_CONCURRENT_FFMPEG` 控制（默认 5，可通过环境变量 `OMNI_MAX_CONCURRENT_FFMPEG` 动态覆盖）。

## 🧪 自检与测试

```bash
python selfcheck.py                # 5 项：模块导入、工具面与模式切换、宿主适配器、临时目录自清理、CLI 命令集
python -m pytest -q                # 需要 pip install -e ".[dev]"
python -m pytest -q -m network     # 显式运行需要真实外部端点/网络的用例（默认跳过）
```

自动化测试全部用**进程内仿真端点**，不联网、不需要任何密钥。想用**真配置 + 真密钥**端到端跑一次
（会真的调用外部模型并计费），用 `examples/` 下的手动脚本：

```bash
python examples/read_audio.py "<音频文件>"     # 原生听音通道（零凭证）
python examples/read_media.py "<音频文件>"     # 外部模型通道（读 config.json 里的端点）
```

## 📄 分页与续读契约

超长媒体按片返回，状态写在工具回执里的 `<!-- OMNI_STATUS: {...} -->`（当前 `contract_version = 1`），含 `next_start_time` / `next_duration_minutes`，照抄下一卷参数即可续读。

- 单块硬上限 `MAX_ONESHOT_MINUTES = 75` 分钟，超长媒体未显式指定时长时自动按 `DEFAULT_SAFE_SLICE_MINUTES = 30` 分钟分卷。
- `read_audio` 另有 8 MiB 内联体积闸：超出或遇到视频/切片场景，自动改走"切片落盘 + 给路径"通道。
- 切片缓存目录由 `OMNI_MEDIA_CACHE_DIR` 决定，缺省 `~/.cache/omni-media/slices`；`inline` 通道用系统临时目录，用完即清。

## 🔗 配套技能：video2book

本仓库是技能 **[video2book](https://github.com/LINJIANG12/video2book)** 的**阶段一听音通道**：该技能要求"真正处理过本集音频"（不得跳过音频保真直接编造正文），有原生音频模态的宿主走 `read_audio`，纯文本宿主走 `read_media`。

**边界**：本仓库只提供 MCP 服务——不 import 技能代码，也不写技能产物（只读媒体、只写切片缓存与显式指定的转录落盘路径）；技能仓库从不 import 本包，两者仅通过 MCP 协议协作。

## 📄 许可

**MIT**，见 [LICENSE](LICENSE)。
