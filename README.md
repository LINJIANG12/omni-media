# 🎛️ OmniMedia — 面向 AI Agent 的音视频听读 MCP 服务

> **同一岗位的两种实现**：宿主自己听得见，就把切片交给宿主听（零凭证）；宿主只有文本能力，就由服务调外部模型代读（配置驱动）。
> 两条通道**分页契约同构**——同一套状态注释、同一套字段、同一套续读循环，切换只需换工具名。

[![M8ven Score](https://m8ven.ai/badge/mcp/linjiang12-omni-media-10kel3?v=4b27fe05e0f109966d63ae1f07557b12)](https://m8ven.ai/mcp/linjiang12-omni-media-10kel3)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=flat-square)](mcp/LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat-square)](#)
[![MCP: >=1.0.0](https://img.shields.io/badge/MCP-%3E%3D1.0.0-blue.svg?style=flat-square)](#)
[![配套技能: video2book](https://img.shields.io/badge/配套技能-video2book-111827?style=flat-square)](https://github.com/LINJIANG12/video2book)

本仓库收纳两个互为补充的 MCP 服务，用于把长视频、系列网课与会议录音变成 Agent 真正"读过"的材料：

| | [`mcp/`](mcp/) | [`mcp-ext/`](mcp-ext/) |
| :--- | :--- | :--- |
| 定位 | **宿主原生听音** | **外部模型代读** |
| 谁来听音频 | **宿主对话模型**的多模态内核（服务只给切片） | **服务自己**调外部模型（Gemini / OpenAI 协议端点） |
| 工具面 | `read_audio` + `inspect_media` | `read_media` + `inspect_media` |
| 凭证 | **零 API Key** | `config.json` 里的端点与 api_key（已 gitignore） |
| 发行名 / 命令 / 注册键 | `omni-media-mcp` / `omni-media` / `omni-media` | `omni-media-ext` / `omni-media-ext` / `omni-media-ext` |
| 宿主要求 | 宿主必须支持音频模态 | 任何能读文本的 Agent |

**怎么选**：宿主模型有原生音频模态 → 用 `mcp/`（延迟最低、零成本）；宿主只有文本能力 → 用 `mcp-ext/`。
**两者可以同时挂**：发行名、控制台命令、注册键、配置文件、用户级目录全部不同，互不覆盖。

---

## 📦 安装

两个服务各自独立安装。**Python ≥ 3.10**，Python 依赖只有 `mcp>=1.0.0`；音频切片另需系统 `ffmpeg`（含 `ffprobe`）。

```bash
# 原生听音版（零凭证）
cd mcp && pip install -e .

# 外部模型代读版（需要外部模型端点）
cd mcp-ext && pip install -e .
```

挂到宿主（`--target` 的可用取值以 `omni-media status` 的实际输出为准）：

```bash
omni-media status                       # 诊断系统依赖 + 各宿主挂载状态与实际配置路径
omni-media apply --target codex         # 显式接入某宿主（先显示 Diff 预览再写入）
omni-media apply --target all --yes     # 非交互脚本模式
omni-media unapply --target codex       # 干净撤销，零残留

omni-media-ext config --init            # 生成 config.json，填入端点与 api_key
omni-media-ext status --probe           # 环境 + 配置 + 端点可达性 + 该挂哪一个
```

自检（两个目录各自独立）：`python selfcheck.py`，`mcp-ext` 另可 `python -m pytest tests -q`。

---

## 🗂️ 目录结构

```text
omni-media/
├── mcp/          ← omni-media-mcp：宿主原生听音（read_audio），零凭证
│   ├── omni_media_mcp/     包源码（server / cli / adapters / preprocessor）
│   ├── selfcheck.py        工具契约与适配器自检
│   └── README.md           详细文档（含各宿主配置接入规范）
└── mcp-ext/      ← omni-media-ext：外部模型代读（read_media），配置驱动
    ├── omni_media_ext/     包源码（server / cli / config / payloads）
    ├── tests/              兼容契约与端到端测试
    └── README.md           详细文档
```

**两个服务互不 import**，任一方缺席都不影响另一方。二者唯一的共同约定是分页与续读状态注释
`<!-- OMNI_STATUS: {...} -->`——契约由 `mcp-ext/tests/test_compatibility.py` 与两侧 `selfcheck.py` 钉死。

---

## 🔗 配套技能：video2book

这两个服务是技能 **[video2book](https://github.com/LINJIANG12/video2book)** 的**阶段一听音通道**。

该技能把 B 站、YouTube、抖音长视频 / 系列网课或本地音视频重构为结构化教材长文、模块合辑全书与思维导图复习笔记；
它的阶段一要求"真正处理过本集音频"（不得跳过音频保真直接编造正文），而这一步正是通过挂载本仓库的
任一服务完成——有原生音频模态的宿主走 `read_audio`，纯文本宿主走 `read_media`。

| | |
| :--- | :--- |
| 技能仓库 | <https://github.com/LINJIANG12/video2book> |
| 听音通道与各宿主工具名映射 | 技能侧 `skills/video2book/references/host-tools/` |
| 技能侧运行前置 | Python 3.10+、系统 `ffmpeg`、以及本仓库任一服务提供的听音通道 |

**边界**：本仓库只提供 MCP 服务——不 import 技能代码，也不写技能产物（只读媒体、只写切片缓存）；
反向同样成立，技能仓库从不 import 本包，两者仅通过 MCP 协议（`omni-media:read_audio` /
`omni-media-ext:read_media`）协作。

---

## 📄 许可

两个服务均为 **MIT**，各自目录下有独立 `LICENSE`。
