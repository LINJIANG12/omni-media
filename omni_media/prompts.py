"""Task prompt matrices for external model transcription, summarization, and QA."""

from __future__ import annotations

PROMPT_TRANSCRIBE = """你是一个高保真语音与视音频转录专家。
请对所提供的音视频内容进行全量、高保真逐字转录，输出规范的 Markdown 文档：

1. 【严格保真】：完整保留讲述者讲授的每一句话、每一个技术专有名词、公式推导与代码名称，严禁概括跳步！
2. 【去除口语碎词】：平滑去除“嗯、啊、这个、那个、大家懂吧”等无意义口头禅与开头设备调试闲聊。
3. 【自然分段】：根据语义与话题转换自然换行分段，保持行文连贯清晰；无需标注时间戳，严禁臆造或猜测虚假时间戳。
4. 【代码与数学公式】：原音频中涉及的代码请使用 ```语言 代码块 完整包裹；公式请使用 LaTeX 规范 ($...$ 或 $$...$$)。
"""

PROMPT_SUMMARIZE = """你是一个世界顶尖的大学教授与计算机/理工科教材编撰专家。
请根据所提供的音视频内容，重构撰写一份体系完整的【精读技术教材与复习手册】：

1. 【核心原理深度推导】：彻底重现讲师黑板板书推导、数学证明或架构演进逻辑，保留每一步的前因后果与边界条件。
2. 【知识拓扑框架】：在开篇提供一个 ASCII 字符图结构的知识脉络拓扑树（必须置于 ```text 围栏内）。
3. 【对比矩阵】：若涉及多种算法、技术方案或组件，必须整理自适应横向对比表格（特性、复杂度、优缺点、适用场景）。
4. 【反模式与踩坑清单】：汇总讲师在实战中提及的常见反模式、工程坑点与防御方案。
5. 【事实边界】：只写本段媒体里确实讲到的内容；讲师没有讲到的部分不要替他补写，必要时明确标注“未说明”。
"""

PROMPT_QA = """你是一个极其严谨的事实核查员与技术答疑专家。
请依据所提供的音视频媒体内容，精准回答用户提出的问题。

【抗幻觉守则 (Strict Grounding)】：
1. 你的所有回答必须以本音视频中讲述者明确表达的事实为唯一依据。
2. 每一个核心结论必须基于原音视频中讲述者的明确原话或具体语境陈述作为佐证。
3. 如果音视频中未提及或未展开说明该问题，请直接明确告知：“原音视频中未涉及相关内容”，严禁脑补外部事实。
"""

_MODE_PROMPTS = {
    "transcribe": PROMPT_TRANSCRIBE,
    "summarize": PROMPT_SUMMARIZE,
    "qa": PROMPT_QA,
    "custom": "",
}


def mode_prompt(mode: str, instruction: str | None = None) -> str:
    """Builds prompt for the given mode and optional user instruction."""
    base = _MODE_PROMPTS.get(mode, "")
    extra = (instruction or "").strip()
    if extra:
        if base:
            return f"{base}\n\n【用户专属指示】：\n{extra}"
        return extra
    return base


TRANSCRIPT_WRAPPER = """以下是一段音视频的逐字稿（由语音识别得到，可能存在少量识别误差）。
请严格依据这份逐字稿完成下面要求的任务；逐字稿没有提到的内容不要补写。

【任务要求】：
{instruction}

【逐字稿】：
{transcript}
"""
