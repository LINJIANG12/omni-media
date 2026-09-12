# 🎧 OmniMedia-Ext: 配置驱动的外部模型音视频代读 MCP 服务

> **把音视频交给外部模型，而不是交给宿主**：本服务自己按配置文件选定的端点发请求，
> 完成转录、教材级总结与抗幻觉问答，把**文本**返回给调用方。

[![MCP 标准](https://img.shields.io/badge/MCP-mcp%3E%3D1.0.0-blue.svg?style=flat-square)](#)
[![协议](https://img.shields.io/badge/协议-Gemini%20%7C%20OpenAI-111827?style=flat-square)](#)
[![配置](https://img.shields.io/badge/配置-config.json-2ea44f?style=flat-square)](#)

---

## 🧭 设计定位：和原生听音版是什么关系

本目录是 `omni-media-mcp`（同一仓库下的 `mcp/`）的**独立新版本**，不是它的升级包：

| 维度 | `mcp/`（原生听音版） | `mcp-ext/`（本仓库） |
| :--- | :--- | :--- |
| 谁来听音频 | **宿主对话模型**的多模态内核（`read_audio` 返回切片路径 / Audio 块，宿主再 `view_file` 听） | **服务自己**调外部模型 API |
| 凭证 | 零凭证 | 需要外部模型密钥（写在配置文件里） |
| 工具面 | `read_audio` + `inspect_media` | **`read_media`** + `inspect_media` |
| 宿主能力要求 | 宿主必须支持音频模态 | 宿主只需能读文本（任何 Agent 都能用） |
| 安装名 / 命令 / 注册键 | `omni-media-mcp` / `omni-media` / `omni-media` | `omni-media-ext` / `omni-media-ext` / `omni-media-ext` |

**两者可以同时装、同时挂**：发行名、控制台命令、MCP 注册键、配置文件、用户级目录全部不同，
互不覆盖。本服务**刻意不提供** `read_audio`——那个工具的全部意义就是「把音频交给宿主去听」，
与本版本「服务自己调外部模型」的定位直接冲突；两条路并存只会让调用方不知道该走哪条。

> **关于大文件**：本服务不用 Gemini Files API，也不做远端上传/轮询/删除。切片由服务自己
> 控制（默认 10 分钟 16kHz 单声道 ≈ 2~3 MiB），远小于内联上限，因此没必要引入那一整套
> 远端状态与失败面。

---

## 🧩 该挂哪一个：与原生听音版的选择规则

两版是**同一个岗位的两种实现**，按「宿主模型自己能不能听音频」二选一：

| 宿主情况 | 挂哪个 | 用哪个工具 | 为什么 |
| :--- | :--- | :--- | :--- |
| **有原生音频模态**（Gemini / GPT-4o Audio / Codex 等多模态宿主） | `omni-media`（`../mcp`） | `read_audio` | 宿主自己就能听，零外部凭证、延迟更低，也不用花外部模型的钱 |
| **没有原生音频模态**（纯文本宿主 / 只听文本的 CLI） | **本版本** | `read_media` | 宿主听不了，必须由外部模型代读 |
| 想两者兼得 | 两个都挂 | — | 见下方「同挂时的优先级」 |

```bash
# 纯文本宿主：只挂本版本
cd mcp-ext && omni-media-ext apply --target zcode
# 有原生音频的宿主：挂原生版
cd mcp && python src/cli.py apply --target zcode      # 或 omni-media apply --target zcode
```

### 同挂时的优先级

两个服务可以同时注册（注册键分别是 `omni-media` 与 `omni-media-ext`，互不覆盖）。
此时**以 `read_audio` 为先**：宿主能听就别绕外部模型。本版本的技能说明里写明了这条让位规则，
Agent 通过「自己的工具列表里有没有 `read_audio`」来判断，不靠猜。

### 为什么可以无感切换

两版的分页协议是**刻意同构**的（`tests/test_compatibility.py` 与 `selfcheck.py` 逐条钉死）：

| 契约项 | 两版是否一致 |
| :--- | :--- |
| 切片参数名与类型（`file_path` / `start_time` / `duration_minutes`，且 `file_path` 为唯一必填） | ✅ 一致 |
| 状态注释标签 `<!-- OMNI_STATUS: {...} -->` | ✅ 同名 |
| 分页字段 `status` / `mode` / `is_finished` / `start_time` / `end_time` / `total_duration` | ✅ 同名同义 |
| 续读字段 `next_start_time` / `next_duration_minutes`（仅未读完时出现） | ✅ 同名同义 |
| 续读循环（`is_finished=false` → 用 `next_start_time` 再调一次） | ✅ 同一段代码可用 |
| 任务语义（逐字稿 / 总结 / 问答） | ❌ 原生版没有；本版本放在扩展键 `task` 里 |
| 返回形态 | ❌ 原生版给音频切片路径（还要宿主自己听）；本版本直接给文本 |

所以调用方在两个 MCP 之间切换时，只需换工具名（`read_audio` ↔ `read_media`），
分页循环一行都不用改。

---

## 🚀 快速开始

### 1. 系统依赖

音频抽取依赖系统 `ffmpeg`（含 `ffprobe`），需在 `PATH` 中：

- **Windows**：`winget install Gyan.FFmpeg`
- **macOS**：`brew install ffmpeg`
- **Linux (Debian/Ubuntu)**：`sudo apt update && sudo apt install -y ffmpeg`

### 2. 安装（Python ≥ 3.10，依赖只有 `mcp`）

```bash
cd mcp-ext
pip install -e .
```

装好后会得到两个命令：`omni-media-ext`（治理 CLI）与 `omni-media-ext-mcp`（stdio 服务）。

### 3. 写配置文件（本服务**只**读配置文件，不读任何环境变量）

```bash
omni-media-ext config --init      # 在仓库根生成 config.json
omni-media-ext config --path      # 打印实际生效的配置文件路径
omni-media-ext config --validate  # 校验语法与字段
omni-media-ext config --show      # 打印脱敏后的有效配置
omni-media-ext status --probe     # 诊断环境 + 端点可达性
```

配置查找顺序（**首个存在者生效**，全部是绝对路径，与当前工作目录无关）：

1. `--config <路径>`（CLI 参数；`apply --server-config <路径>` 可把它写进宿主注册的启动参数）
2. `<仓库根>/config.json`
3. `~/.omni-media-ext/config.json`

都不存在时报错会把三个候选路径全列出来。**加载期不校验密钥**：`inspect_media`、
`config --validate`、`status` 在没有密钥时都能用，只有真正要发请求时才要求凭证就绪。

### 4. 配置 schema

```jsonc
{
  "active": "gemini-flash",              // 默认端点；工具参数 endpoint 可覆盖
  "defaults": {
    "slice_minutes": 10,                 // 未传 duration_minutes 时的单片预算
    "max_payload_mb": 18,                // 单请求媒体上限（原始字节），超出自动收窄切片
    "timeout_sec": 300,                  // 单次 HTTP 超时
    "max_retries": 2,                    // 仅对 429/5xx/网络错误重试（4xx 鉴权错误绝不重试）
    "max_concurrency": 3                 // ffmpeg 并发上限
  },
  "endpoints": {
    "<端点名>": {
      "protocol": "gemini",              // 只支持 gemini / openai
      "base_url": "https://generativelanguage.googleapis.com/v1beta",
      "api_key": "REPLACE_ME",
      "model": "gemini-2.5-flash",
      "headers": {}                      // 可选：代理网关自定义头（可作为鉴权逃生口）
    }
  }
}
```

- 允许 `//` 与 `/* */` 注释；以下划线开头的键（`_note` 等）当作注释跳过。
- 未知字段是**错误**而不是忽略：`apikey` 这种拼错如果被静默忽略，你会得到一个「没有密钥的端点」，
  只能在发请求时才以 401 暴露。
- `api_key` 留空或仍是模板值 `REPLACE_ME` 时判定为**未就绪**；若你的网关不需要密钥，
  在 `headers` 里给出鉴权头（`Authorization` / `x-goog-api-key` 等）即可满足就绪判定。

### 5. 两种协议

#### `protocol: "gemini"`

`POST {base_url}/models/{model}:generateContent`，音频以 `inlineData`（base64）内联，
鉴权头 `x-goog-api-key`。

- `base_url` 写原生 REST 前缀 `https://generativelanguage.googleapis.com/v1beta`（服务不会自动补 `/v1beta`）；
- 判定被安全策略拦截（`promptFeedback.blockReason`）时会抛明确错误，而不是返回空串；
- `finishReason == MAX_TOKENS` 时正常返回，但正文前会加一条「建议缩小 duration_minutes」的提示。

#### `protocol: "openai"`

由 `openai_mode` 选择线上形状：

**`openai_mode: "chat"`（默认）** —— 单段完成，四个 mode 全支持：

```jsonc
{
  "protocol": "openai",
  "openai_mode": "chat",
  "base_url": "https://api.openai.com/v1",
  "api_key": "REPLACE_ME",
  "model": "gpt-4o-audio-preview",
  "audio_format": "mp3"          // input_audio 只接受 mp3/wav；本服务负责真转码
}
```

`POST {base_url}/chat/completions`，content 里放 `{"type":"text"}` +
`{"type":"input_audio","input_audio":{"data":<base64>,"format":"mp3"}}`。
切片本身是 m4a(AAC)，服务会用 ffmpeg **真转码**成 mp3（把 m4a 字节谎称成 mp3 会得到损坏载荷）。
`model` 名里含 `audio` 时才附带 `"modalities": ["text"]`（该字段会让部分通用兼容端点 400）。

**`openai_mode: "transcriptions"`** —— 兼容任何 Whisper 系端点：

```jsonc
{
  "protocol": "openai",
  "openai_mode": "transcriptions",
  "base_url": "https://api.openai.com/v1",
  "api_key": "REPLACE_ME",
  "model": "whisper-1",
  "text_model": "gpt-4o-mini",   // 第二段（纯文本）用；mode != transcribe 时必填
  "language": "zh"               // 可选：给 ASR 的语言提示
}
```

`POST {base_url}/audio/transcriptions`（multipart 原样上传 m4a，无 base64 膨胀）。
它本身只产出逐字稿，因此：`mode="transcribe"` 时一段结束；`summarize` / `qa` / `custom`
时**自动追加第二段纯文本** `chat/completions`，把逐字稿交给 `text_model` 处理。
若 `text_model` 没填而 mode 又需要第二段，会给出明确的配置错误（不会拿 `whisper-1` 当对话模型用）。
端点若返回 `segments`，逐字稿会渲染成 `[HH:MM:SS]` 时间线。

---

## 🛠️ 暴露给 Agent 的工具

### `read_media`（⭐ 唯一代读入口）

| 参数 | 必填 | 说明 |
| :--- | :--- | :--- |
| `file_path` | ✅ | 本地音视频绝对路径（mp4/mkv/mov/flv/webm/mp3/wav/m4a/aac/flac…） |
| `mode` | | `transcribe`（逐字稿）/ `summarize`（教材级总结）/ `qa`（抗幻觉问答）/ `custom` |
| `instruction` | | 自定义附加提示词；`mode="custom"` 时必填 |
| `endpoint` | | 配置里的端点名；缺省用 `active` |
| `start_time` | | 切片起始（`'HH:MM:SS'` / `'MM:SS'` / 秒数） |
| `duration_minutes` | | 本次切片分钟数，> 0；缺省用 `defaults.slice_minutes` |

**一次调用 = 一个切片 = 一次模型响应**。超长媒体由调用方按返回的续读参数循环推进，
这样输出体积天然有界，失败重试粒度也最小。

返回是固定契约的 Markdown：

```text
<!-- OMNI_STATUS: {"status":"COMPLETED|IN_PROGRESS","mode":"oneshot|chunked","is_finished":true,"start_time":"00:00:00","end_time":"00:10:00","total_duration":"00:32:25","channel":"external-model","task":"transcribe","endpoint":"gemini-proxy","protocol":"openai","model":"gemini-3.8-flash-high","elapsed_sec":42.1,"clamped":false} -->

<模型返回正文>

> ⏱️ **续读下一分卷参数**: `start_time="00:10:00", duration_minutes=10.0`
```

**状态注释的前半段与原生听音版逐字同构**（标签同名、键名同义），详见下一节。
本版本独有的信息只放在不冲突的扩展键里：`channel` / `task` / `endpoint` / `protocol` /
`model` / `elapsed_sec` / `clamped` / `finish_reason`。

> **注意 `mode` 与 `task` 的分工**：`mode` 是**切片模式**（`oneshot` = 整篇一次读完，
> `chunked` = 分卷），与原生版语义一致；**任务预设**在 `task` 里
> （`transcribe` / `summarize` / `qa` / `custom`）。同一个键在两版里含义不同会被静默误读，
> 所以刻意拆开。

未读完时状态注释里会多出 `next_start_time` / `next_duration_minutes`；
切片被载荷预算收窄时 `clamped: true`，正文后另有一条说明。

### `inspect_media`

毫秒级本地探测（ffprobe）时长、编码、轨道、体积与 token 预算，**不联网、不需要任何凭证**，
因此配置还没写好时也能用它估预算。

### 资源 `media://endpoints`

返回脱敏后的端点清单 + `active` + 配置文件路径，供调用方挑选 `endpoint` 参数。

---

## 🖥️ CLI 治理

```bash
# 1. 诊断：环境 / 配置文件 / 端点（脱敏）/ 宿主挂载状态
omni-media-ext status
omni-media-ext status --probe          # 额外对每个端点 GET /models 探测可达性

# 2. 配置治理（本服务唯一的配置入口）
omni-media-ext config --init           # 生成 config.json（已存在则拒绝，除非 --force）
omni-media-ext config --init "D:/my/config.json"
omni-media-ext config --path | --validate | --show | --example

# 3. 本地探测（不联网）
omni-media-ext inspect "D:/courses/x/audio/P01.m4a"

# 4. 接入 / 撤销宿主（注册键 omni-media-ext，与原版 omni-media 互不覆盖）
omni-media-ext apply --target zcode
omni-media-ext apply --target all --yes
omni-media-ext apply --target dsh --server-config "D:/my/config.json"   # 把配置路径写进启动参数
omni-media-ext apply --target codex --skill-dir "<项目>/.agents/skills"  # 技能装到项目级而非用户级
omni-media-ext unapply --target zcode

# 5. 启动 stdio 服务（宿主注册的就是这条）
omni-media-ext serve
omni-media-ext serve --config "D:/my/config.json"
```

支持的宿主：`opencode` / `zcode` / `dsh` / `codex` / `antigravity`。
接入只写入**服务启动命令与 `PYTHONPATH`**，不写入任何凭证——密钥留在配置文件里
（`selfcheck.py` 会断言每个适配器的 `env` 恰好只有 `PYTHONPATH`）。

> **codex 适配器**还会顺带安装 `SKILL.md`：默认装到**用户级** `~/.agents/skills/omni-media-ext/`，
> 想装到项目里就用 `--skill-dir "<项目>/.agents/skills"`（默认不写当前工作目录，避免把
> MCP 技能文件塞进无关仓库）。

---

## ✅ 测试与自检

```bash
python selfcheck.py                     # 16 项静态不变量自检（离线、零密钥、无网络）
python -m pytest tests/ -q              # 109 项：配置 / 协议载荷 / CLI 治理 / MCP 端到端 / 与原版兼容
python test_mcp_read_media.py "<音频>" --duration 1     # 真端点实测（需密钥，手动）
```

自动化测试**不需要任何真实密钥**：`tests/stub_endpoint.py` 在 `127.0.0.1` 上起一个仿真端点，
实现四个路由（Gemini `generateContent`、OpenAI `chat/completions`、`audio/transcriptions`、`models`），
并把收到的请求原样记录，测试据此断言**线上载荷的精确形状**——协议改坏了会立刻暴露。
端到端测试会真的用 `mcp.client.stdio` 拉起服务、真的用 ffmpeg 合成媒体、真的走完分页串联。

---

## 🩺 故障排查

| 现象 | 原因与处理 |
| :--- | :--- |
| **`/v1/models/M:generateContent` 返回 404**（Gemini 反代最常见的坑） | `protocol` 与 `base_url` 不匹配：Gemini 原生形状拼的是 `{base_url}/models/M:generateContent`，所以 `base_url` 必须以 **`/v1beta`** 结尾。若网关给的路径是 **`/v1`**（OpenAI 兼容形状），就该配 `protocol: "openai"`。两者不可混用——绝大多数反代（one-api / new-api / antigravity-tools 等）**同时**提供两条路：`/v1/chat/completions` 与 `/v1beta/models/M:generateContent` |
| `未找到配置文件` | 三个候选路径都不存在。跑 `omni-media-ext config --init`，或 `--config` 指定路径 |
| `--config 指定的配置文件不存在` | 显式路径必须存在（不静默回退到默认位置，否则你以为改动生效了） |
| `配置文件 JSON 语法错误: … 第 N 行第 M 列` | JSONC 去注释后仍解析失败，按行列定位 |
| `顶层存在未知字段` / `endpoints.x 存在未知字段` | 字段拼错了（防静默失效），按报错列出的合法字段改 |
| `端点 x 未配置 api_key（或仍是模板占位值 REPLACE_ME）` | 填 `api_key`，或在 `headers` 里给鉴权头 |
| `端点返回 HTTP 401/403` | 密钥无效或无权限；确认 `base_url` 是否指向正确的区域/网关 |
| `端点返回 HTTP 404` | `base_url` 少写或写错前缀（Gemini 要 `/v1beta`，OpenAI 要 `/v1`），或 `model` 名不存在 |
| `若该端点不支持 input_audio，请把 openai_mode 改成 transcriptions` | 第三方兼容端点常不支持 `input_audio`，改走 `/audio/transcriptions` |
| `HTTP 429/5xx` 后失败 | 已按 `max_retries` 指数退避重试；仍失败就调大 `max_retries` 或降低并发 |
| 正文被截断、状态里 `finish_reason: MAX_TOKENS` | 调小 `duration_minutes` 重读本片 |
| 状态里 `clamped: true` | 切片被 `max_payload_mb` 收窄了，按续读参数继续即可 |
| 行内公式显示成 `$…$` 源码 | 阅读侧一次性设置：Typora → 偏好设置 → Markdown → 勾选「内联公式」 |

---

## 📦 依赖与凭证

- **运行时依赖**：`mcp>=1.0.0`（仅此一项）。HTTP 走标准库 `urllib`，不引入 `google-genai` / `openai` / `httpx`。
- **系统依赖**：`ffmpeg`（含 `ffprobe`）。
- **凭证**：只来自配置文件；源码里**不读任何 `*_API_KEY` 环境变量**（`selfcheck.py` 有硬断言）。
  `config.json` 已在 `.gitignore` 中排除，`status` / `config --show` / `media://endpoints` /
  diff 预览一律只显示脱敏形式（`abcd...wxyz`）。**请勿分享该文件**。

## 许可证

MIT License（见 [LICENSE](LICENSE)）。
