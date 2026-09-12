---
name: omni-media-ext
description: Configuration-driven external-model audio/video reading for AI Agents. Transcribes, summarizes and answers grounded questions about local media by calling a configured external endpoint (Gemini protocol or OpenAI protocol), with no dependency on the host model's audio modality.
triggers:
  - 读音频
  - 音视频转录
  - 逐字稿
  - 网课转录
  - read_media
  - inspect_media
  - 外部模型代读
---

# OmniMedia-Ext Skill：外部模型代读音视频

本技能面向**不具备（或不方便使用）音频模态**的智能体：它不要求宿主模型能听音频，
而是由 MCP 服务自己按配置文件调外部模型，把**文本**取回来。

---

## 0. 先做选择：该用哪条通道（重要）

本技能与 `omni-media` 技能是**同一个岗位的两种实现**。开工前先看**你自己当前的工具列表**，
用工具是否存在来判断该走哪条路，不要靠猜：

| 你的工具列表里有什么 | 走哪条路 | 原因 |
| :--- | :--- | :--- |
| 有 **`read_audio`**（来自 `omni-media` 服务） | **优先用 `read_audio`**：切片后用你自己的文件查看工具原生聆听 | 宿主自己有音频模态，零外部凭证、延迟更低、不用花外部模型的钱 |
| 只有 **`read_media`**（来自本服务） | 用本技能的 `read_media` | 宿主听不了音频，只能由外部模型代读 |
| 两者都有 | **优先 `read_audio`**；它报错或你需要 `summarize` / `qa` 这类加工时，再退回 `read_media` | 原生听音更便宜；外部模型更适合「听完还要写教材 / 答题」 |
| 两者都没有 | 本技能用不了：需要先挂载 `omni-media-ext` 并写好配置文件 | —— |

> **分页契约两版同构**：两条通道都用 `start_time` / `duration_minutes` 切片，返回文本都以
> 同一行 `<!-- OMNI_STATUS: {...} -->` 注释开头（`is_finished` / `next_start_time` /
> `next_duration_minutes` 同名同义）。因此**同一段续读循环在两个 MCP 之间可以无感切换**，
> 你不需要为它们写两套逻辑。注意 `mode` 字段在两版里都表示**切片模式**（`oneshot` /
> `chunked`），本版本的任务预设放在 `task` 字段里，别读错。

---

## 1. 工具集

### `read_media`（⭐ 唯一代读入口）

| 参数 | 必填 | 说明 |
| :--- | :--- | :--- |
| `file_path` | ✅ | 本地音视频绝对路径 |
| `mode` | | `transcribe` 逐字稿 / `summarize` 教材级总结 / `qa` 抗幻觉问答 / `custom` |
| `instruction` | | 自定义提示词；`mode="custom"` 必填 |
| `endpoint` | | 配置里的端点名；缺省用配置的 `active` |
| `start_time` | | 切片起始（`"00:10:00"` / `"10:00"` / 秒数） |
| `duration_minutes` | | 本次切片分钟数，> 0 |

### `inspect_media`

本地 ffprobe 探测，**不联网、不需要凭证**。用它先看时长，再决定切片预算。

### 资源 `media://endpoints`

脱敏端点清单（协议、base_url、model、密钥掩码、是否就绪），用来挑 `endpoint`。

---

## 2. 标准工作流

### 工作流 A：一集一片（推荐）

1. **估预算**：`inspect_media(file_path=...)` 拿到总时长；
2. **取第一片**：`read_media(file_path=..., mode="transcribe", duration_minutes=10)`；
3. **解析状态注释**：返回文本首行是机器可读的（与原生听音版同名同义）

   ```text
   <!-- OMNI_STATUS: {"status":"IN_PROGRESS","mode":"chunked","is_finished":false,"start_time":"00:00:00","end_time":"00:10:00","total_duration":"00:32:25","channel":"external-model","task":"transcribe","endpoint":"gemini-proxy","next_start_time":"00:10:00","next_duration_minutes":10.0} -->
   ```

   - `is_finished` / `next_start_time` / `next_duration_minutes` / `mode` 与原生版**同名同义**；
   - `mode` 是**切片模式**（`oneshot` / `chunked`）；本次用的是哪个任务预设看 `task`；
   - `channel` 恒为 `external-model`，便于在一段日志里区分两条通道。

4. **循环续读**：`is_finished=false` 时用 `start_time=<next_start_time>` 再调一次，
   直到 `is_finished=true`；这段循环与原生通道可以完全复用。
5. **换任务不用重听**：已拿到逐字稿后可再次调用并传 `instruction`（或直接拿文本自己处理）。

### 工作流 B：长讲座的总结

- 逐片 `mode="transcribe"` 拿到逐字稿 → 自己汇总；**或者**
- 逐片 `mode="summarize"` 让外部模型直接出教材级总结（更贵，但一次到位）。

### 工作流 C：基于内容问答

`read_media(mode="qa", instruction="讲师对 XX 的结论是什么？", start_time=...)`。
该模式内置抗幻觉约束：结论必须给出时间范围佐证，未涉及的内容必须回答「原音视频中未涉及」。

---

## 3. 使用要点

- **端点选择**：不传 `endpoint` 时用配置的 `active`。想换模型就换端点名，不用改代码。
- **`clamped` 字段**：若状态里 `clamped: true`，说明本次切片被端点载荷预算收窄了，
  按 `next_start_time` 继续读即可，不要以为读完了。
- **`finish_reason: MAX_TOKENS`**：返回正文前会有截断警告，调小 `duration_minutes` 重读本片。
- **`openai_mode=transcriptions` 的端点**：`mode="transcribe"` 只走一段 ASR；
  其余 mode 会自动追加一段纯文本推理（需要配置 `text_model`）。
- **错误信息是可操作的**：配置缺失、端点未就绪、HTTP 状态码与响应体摘要都会原样返回，
  按提示改配置文件即可，不必重试同一个调用。
- **不要在音频上重试鉴权错误**：401/403 不会重试，改完密钥再调。

---

## 4. 环境要求

- 系统 `ffmpeg`（含 `ffprobe`）在 `PATH` 中；
- 服务侧 Python 依赖只有 `mcp`；
- **需要一份配置文件**：`omni-media-ext config --init` 生成后填入 `api_key`；
  用 `omni-media-ext status` 确认端点已就绪。
