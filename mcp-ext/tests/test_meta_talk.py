"""元话语判别与上游失败提示的回归测试。

背景：外部模型偶尔**不转录**，而是把它自己的写作计划/自查清单当结果返回（HTTP 200、
内容非空）。实测三门课里出现过三次，每次都**被正常落盘当逐字稿用**，而逐字稿是
阶段一长文的唯一事实来源——所以这条静默通道必须堵住。

本文件用**事故现场的真实样本**做夹具，两个方向都钉住：
* 正例：三份真实元话语返回（P16/P32/P27）必须判为元话语；
* 反例：同一门课的 34 份真稿必须零误杀（误杀真稿比漏判更糟——可用语料会被丢掉），
  其中 P05 是关键的边界样本：**真稿 + 开头一句英文元话语前缀**，必须不判为元话语。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni_media_ext.config import Defaults, Endpoint
from omni_media_ext.providers.base import (
    ProviderRequestError,
    count_meta_markers,
    looks_like_model_meta,
    upstream_hint,
)
from omni_media_ext.providers.openai import OpenAIEndpoint

from stub_endpoint import OPENAI_TRANSCRIPTIONS


# ---------------------------------------------------------------------------
# 事故现场的真实元话语返回（逐字保留，只截取到各自的实际长度级别）
# ---------------------------------------------------------------------------

META_P16 = """ending time:

1.  **Segment 1: 00:05 - 00:12**
    - "然后接下来呢我们今天..."
    - Audio: 然后接下来呢，我们今天的重点呢其实给大家介绍一下这个 user interface design.
    - Match: 00:05.500 -> 00:11.700

2.  **Segment 2: 00:14 - 00:20**
    - Audio: 这个刚才来晚的同学可能没看过是吧？那再给大家看一下啊。
    - Match: 00:14.000 -> 00:20.000

5.  **Segment 5: 00:39 - 00:54**
    - Audio: 然后，呃，你，你 interface 首当其冲，呃，要挨骂，啊。
    - Match: 00:39.000 -> 00:54.000

...Continue carefully transcribing each section with accurate timestamps and words. Ensure English words like "user interface design", "place the user in control", "reduce the user's memory load", "make the interface consistent" etc. are transcribed accurately as spoken."""

META_P32 = """The speaker gives a lecture in Mandarin Chinese with slides on software engineering management, scheduling, metrics, and risk management. Some English words are interspersed (e.g. "schedule", "deadline", "effort", "risk", "EVA", "SPI", "SV", "BAC", "ACWP", "BCWS", "BCWP", "CPI", "CV", "RMMM").

Let's transcribe accurately following standard Chinese and the speaker's actual utterances, making sure timings align well.

Key parts to double check:
- 00:06 项目管理的时间、进度安排还有这个追踪。
- 00:15 首先我们讲一些概念...
- 00:30 第一个原因可能设置一个不合理的、不现实的deadline...比如我需要一年多...
- 01:30 对effort，工作量低估了...
- 02:25 风险，risk...不可预见的...

Transcription aligns smoothly with standard spoken text. Formatting neatly as requested."""

META_P27 = """Self-correction during drafting:
- The speaker introduces chapters in Software Engineering (likely Pressman's book Chinese translation):
  - 第20章: WebApp测试 (Testing Web Applications)
  - 24章: 项目进度的安排 (Project Scheduling)
- Pay attention to terminology:
  - WebApp 测试
  - 语法错误 / 语义错误
  - 导航测试 (Navigation testing)
  - 可用性测试 (Usability testing)

Transcribing chunk by chunk with proper punctuation, ensuring accuracy."""


# ---------------------------------------------------------------------------
# 真稿样本（逐字取自本门课已交付的逐字稿）
# ---------------------------------------------------------------------------

REAL_P01 = """我们这门课叫软件工程 Software Engineering。 然后我是陈跃。然后大家呢如果还有人不知道我的 email 地址的话，你现在赶紧记一下。 然后有问題可以交流。那下面呢有两个网址。 这两个网址呢，第一个网址你要记一下，其实也很好记的，就是我们 CS 的 那个 主页后面加个 /se，是我们课程的网站。 那个网站上对大家比较有 意义的是它有一套那个自测题。 自测题然后大概有500多道题。 所以我的建议呢也是，就说你每天上完课以后花个5分钟去把那个自测题过一遍，你这一个学期都会觉得它不是负担。如果你要到考试之前去过500多道题的话，你会昏倒的。 然后下面那一个网址呢，就是我们4C98，然后我的答疑板。 然后课程的全部的资料，我每次下课以后会打包放到那个板上去。"""

REAL_P12 = """上一次课我们讲解什么? 上一次课我们开始讲这个design设计的问题。 呃然后按照 这门课的国际惯例啊,每次在讲一个概念之前,先要讲一堆它的意义,然后它的基本概念,对吧?然后有有一些基本的设计原则,这是我们上一次课给大家介绍的。呃那什么是一个好的设计?什么是不好的设计? 那设计中间我们特别看重得比如说 这个模块的抽象,数据结构的抽象,还有模块的独立性。 我们说模块独立性有两个衡量指标是什么来着? 哎cohesion,还有一个 还有一个叫什么?cohesion是什么? 哎别啊, cohesion变成耦合了。 cohesion是内聚。 哎就是说我们这个模块在多大程度上在做一件事情。 对吧? 功能的内聚性,然后我们希望这cohesion是越强越好。 然后耦合是什么? 耦合是coupling,哎coupling我们希望它越 越轻越好。"""

# P05 的实测形态：真稿，但开头残留一句英文元话语前缀（原始文件里后面直接接时间码）。
# 这是「标记只出现在开头」的边界样本——它**必须**不被判为元话语。
P05_SHAPE = (
    "The speech transcript is as follows:"
    "00:14 那么今天呢我们讲这个第三章，00:18 过程模型。"
    "00:19 我们上一章呢我们介绍过，00:21 第二章的时候，"
    "00:23 给大家介绍过这个00:25 过程的一个一般性的这个框架，00:28 框架啊。"
    "那么今天呢我们要讲一些过程的一些具体的这个模型，00:35 模型。"
    "那么先给大家介绍一个，比较传统的模型，叫瀑布模型，waterfall model。"
    "这个模型的历史很久，它是最早被提出来的一个过程模型，"
    "强调的是阶段之间的顺序推进，每个阶段做完才能进入下一个阶段。"
)


# ---------------------------------------------------------------------------
# 判别函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sample", [META_P16, META_P32, META_P27], ids=["P16", "P32", "P27"])
def test_real_meta_talk_is_flagged(sample: str):
    assert looks_like_model_meta(sample) is True
    assert count_meta_markers(sample) >= 2


@pytest.mark.parametrize("sample", [REAL_P01, REAL_P12], ids=["P01", "P12"])
def test_real_transcript_is_not_flagged(sample: str):
    assert looks_like_model_meta(sample) is False


def test_transcript_with_meta_prefix_is_not_flagged():
    """真稿开头残留一句英文元话语前缀（实测 P05）——只出现一次，不能误杀。"""
    assert count_meta_markers(P05_SHAPE) == 1
    assert looks_like_model_meta(P05_SHAPE) is False


@pytest.mark.parametrize("sample", ["", "   ", "\n\n"])
def test_blank_text_is_not_flagged(sample: str):
    assert looks_like_model_meta(sample) is False


# ---------------------------------------------------------------------------
# 上游失败提示
# ---------------------------------------------------------------------------

def test_upstream_hint_matches_gateway_token_failure():
    hint = upstream_hint(503, "Token acquisition timeout (5s) - system too busy or deadlock detected")
    assert "上游" in hint
    assert "/audio/transcriptions" in hint  # 明确告诉操作者**不要**去查这个


@pytest.mark.parametrize(
    "status, body",
    [
        (503, "multipart 解析失败"),          # 5xx 但不是上游凭证问题
        (401, "unauthorized"),                 # 上游特征命中，但不是 5xx（配置/密钥错）
        (400, "bad request"),                  # 4xx
        (200, ""),                             # 成功
    ],
)
def test_upstream_hint_ignores_other_failures(status: int, body: str):
    assert upstream_hint(status, body) == ""


# ---------------------------------------------------------------------------
# _transcribe：元话语有限次重试后报错；正常返回不受影响
# ---------------------------------------------------------------------------

def _transcriptions_endpoint(base_url: str, max_retries: int) -> OpenAIEndpoint:
    return OpenAIEndpoint(
        Endpoint(
            name="oai",
            protocol="openai",
            base_url=base_url,
            model="gemini-3.8-flash-high",
            api_key="test-key",
            openai_mode="transcriptions",
            language="zh",
        ),
        Defaults(
            slice_minutes=10.0,
            max_payload_mb=18,
            timeout_sec=30,
            max_retries=max_retries,
            max_concurrency=2,
        ),
    )


def test_transcribe_retries_meta_talk_then_raises(stub, audio_m4a: Path):
    """元话语返回必须重试到上限后报错，而不是当成功交出去。"""
    stub.set_json(OPENAI_TRANSCRIPTIONS, {"text": META_P27})
    endpoint = _transcriptions_endpoint(stub.base_openai, max_retries=2)

    with pytest.raises(ProviderRequestError) as excinfo:
        endpoint.process(audio_m4a, "请转录", "transcribe")

    assert len(stub.requests_for(OPENAI_TRANSCRIPTIONS)) == 3  # max_retries + 1
    message = str(excinfo.value)
    assert "提纲/计划" in message
    assert "duration_minutes" in message  # 给出可照做的下一步


def test_transcribe_recovers_when_second_attempt_is_real(stub, audio_m4a: Path):
    """第一次元话语、第二次真稿：应自愈返回真稿（这正是 P16/P32 的现场形态）。"""
    good = "这一段是真实的讲课内容，讲的是软件过程模型与瀑布模型的取舍。"
    state = {"n": 0}

    original = stub.response_for

    def flaky(route, body):
        if route == OPENAI_TRANSCRIPTIONS:
            state["n"] += 1
            payload = {"text": META_P32} if state["n"] == 1 else {"text": good}
            import json

            return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
        return original(route, body)

    stub.response_for = flaky  # type: ignore[assignment]
    endpoint = _transcriptions_endpoint(stub.base_openai, max_retries=2)

    assert endpoint.process(audio_m4a, "请转录", "transcribe").text == good
    assert state["n"] == 2


def test_transcribe_happy_path_unchanged(stub, audio_m4a: Path):
    """正常返回仍是单次请求、原样返回（成功路径零新增开销）。"""
    endpoint = _transcriptions_endpoint(stub.base_openai, max_retries=2)
    result = endpoint.process(audio_m4a, "请转录", "transcribe")

    assert len(stub.requests_for(OPENAI_TRANSCRIPTIONS)) == 1
    assert "STUB-ASR" in result.text
