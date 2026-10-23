"""AI 内容筛选 —— 决定这篇视频值不值得进知识库。

这是整个 Obsidian 功能里最关键的一环。**保存本身没有价值，筛选才有价值**：
不筛的话，vault 三个月后就变成垃圾场，再也回不来了。

为什么必须用大模型而不是本地打分
--------------------------------
本地规则（词频、长度、是否含数字）衡量的是「信息密度」，而知识库真正关心的是
「对我有没有增量」—— 后者必须知道用户的关注方向，只能交给有语义的模型。

面向 AI 检索的评分维度
----------------------
给未来的 RAG 用，标准跟给人看不同。top-k 召回位置有限，宁要 100 篇 80 分的，
也不要 500 篇 60 分的（后者会稀释每次检索）。所以维度是围绕「能不能被准确召回、
召回后有没有信息增量」设计的：

============  ============================================================
维度          为什么对 AI 检索重要
============  ============================================================
取舍逻辑      ``LangGraph``/``RAG`` 这类词是 query 的匹配锚点，
              没专名的笔记语义检索几乎命中不了；
              **主维度**：讲清了「为什么这么选」才能推导出一批做法，
              只有步骤没有判断的内容换个问法就废了
专实体密度    同上，是召回的前提条件
信息增量      大模型已经知道的通识等于零贡献，存进去只占召回位置
可执行性      仅作次要参考（不单独计分，体现在 actionable 字段）
上下文自足    被召回时要能独立看懂，不能依赖「上文第 3 点」
时效性        会快速过期的内容（如某个库的临时教程）价值随时间衰减
============  ============================================================

.. note::
   v2 变更：第一维度由「信息增量」改为「取舍逻辑」，「信息增量」降为第三。
   原因见 ``config.py`` 的 ``obsidian.focus`` —— 用户明确要「原理优先」，
   旧版把「可执行性」单列 20% 会让纯讲原理的内容被无谓扣分，与 focus 打架。
   这两处标准必须同步改，否则模型会在两套判据间撕裂。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.errors import VSumError
from ..core.models import SummaryResult

#: 评分区间约定，跟配置里的阈值对应
TIER_HIGH = "精选"
TIER_MID = "待定"
TIER_LOW = "丢弃"


@dataclass
class Judgement:
    """一次筛选的结论。"""

    score: int = 0                     # 0-100
    tier: str = TIER_MID               # 精选 / 待定 / 丢弃
    reason: str = ""                   # 给用户看的一句话理由
    topics: list[str] = field(default_factory=list)   # 主题标签
    entities: list[str] = field(default_factory=list)  # 专有名词/术语，检索锚点
    actionable: list[str] = field(default_factory=list)  # 可执行动作
    terms: list[str] = field(default_factory=list)      # 需要解释的术语
    ok: bool = False                   # 筛选是否成功（False 表示走了降级）
    note: str = ""                     # 降级原因

    @property
    def keep(self) -> bool:
        """要不要落盘。低分档不写文件，只在界面提示。"""
        return self.tier in (TIER_HIGH, TIER_MID)


_SYSTEM = """你是知识库入库的内容筛选器，同时也是一个面向检索的知识工程师。

你的判断直接决定一段内容是否进入用户的知识库，而这个知识库未来会被 RAG 之类的
检索系统读取，用来回答问题。所以你要判断的不是「这段内容好不好看」，而是：

**如果一段文字被检索系统召回，它对回答问题有没有真实贡献。**

评分维度（0-100 总分）：

1. 取舍逻辑（权重最高，约 35%）
   这段内容有没有讲清「为什么这么选」？具体看：是否说明了一个方案在什么条件下
   优于另一个、某个设计决策背后的权衡、某个坑的根因是什么。
   凡是只有结论没有依据（"应该这样做"却不说"为什么"），本项大幅扣分。
   反过来，能推导出一批做法的原理，给高分。
   注意：本项取代了旧版的「可执行性」——**有操作步骤不是加分项**，
   因为可执行的内容网上到处都是，稀缺的是判断力。

2. 专实体密度（约 20%）
   是否包含可检索的专有名词：产品名、库名、函数名、协议名、公司、模型名、
   技术术语。密度越高越容易被检索命中。空泛的议论几乎没有检索价值。
   若是语音识别转写来的文本，专名可能有误——不确定的正确写法放进 entities，
   并在 reason 里说明哪些存疑，宁可少写也不要写入错锚点。

3. 信息增量（约 20%）
   这段内容里的信息，大模型本身是否已经知道？凡是通用常识、入门科普、
   "首先我们需要理解什么是X"这类铺垫，扣分。给出来源细节、独到观察、
   真实数字与经验、别人踩过的坑，加分。

4. 上下文自足性（约 15%）
   被单独召回时，读者（或者 AI）能否只看这一篇就明白？
   依赖前因后果、只有结论没有依据的内容扣分。

5. 时效风险（约 10%）
   内容是否会快速过期？具体版本号、临时方案、时效性强的结论要扣分并标注。
   稳定的方法论、原理、设计思路加分。

评分务必给得果断，不要所有内容都打 70-80 分。用户宁可少存也不要库被污染。
90 分以上应该有明确形态：带失效边界的原理、有量化切换点的决策、
反直觉且有因果的结论、或根因定位（"真正原因在源码的某行"）。

标签要求：
- topics：1-4 个中文主题标签，用书名号或方括号包裹的规范概念名，不要太泛。
  例如 "LLM推理优化"、"向量检索"、"求职面试"
- entities：2-8 个原文中的专有名词，保持原样不要翻译（代码里的名字尤其要原样）
- actionable：可执行的动作，每条一句话，能直接照做。没有就给空数组，
  不要为了填满而把原文的标题党当动作
- terms：需要解释的术语，凡是非大众概念都要列（如 VAD、TensorRT、wbi 签名）

严格按下面的 JSON 输出，不要输出别的任何内容：

{"score": 85, "reason": "一句话理由，不超过 40 字", "topics": ["标签1"], "entities": ["专有名词"], "actionable": ["可执行动作"], "terms": ["术语"]}"""


def _extract_json(text: str) -> dict:
    """从模型输出里挖出 JSON 对象。

    模型常在 JSON 前后带 ```json 围栏或寒暄，直接 ``json.loads`` 会失败。
    """
    if not text:
        raise VSumError("筛选返回内容为空")
    raw = text.strip()

    # 去掉 markdown 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()

    # 直接解析；失败则找第一个 { 到最后一个 } 的片段
    for candidate in (raw, _brace_slice(raw)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    raise VSumError(f"筛选结果不是合法 JSON：{text[:200]}")


def _brace_slice(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else ""


def _as_list(value) -> list[str]:
    """模型有时给字符串、有时给数组，统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[、,，;；|]", value)
        return [p.strip().strip("\"'[]【】《》") for p in parts if p.strip()][:8]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:8]
    return []


def _clean(items: list[str]) -> list[str]:
    """清洗模型返回的标签：去重、去空白、丢掉明显不是标签的东西。

    大模型偶尔会回一些垃圾（空串、纯标点、整句话）。这些直接写进 tags
    会污染知识库，所以宁可少留。
    """
    out: list[str] = []
    for raw in items:
        s = re.sub(r"\s+", " ", str(raw or "")).strip()
        s = s.strip("\"'[]【】《》#*·|-")
        if not s or len(s) > 30:
            continue
        if not re.search(r"[\w\u4e00-\u9fff]", s):   # 纯标点
            continue
        if re.fullmatch(r"(.)\1*", s) and len(s) >= 4:  # 乱码串
            continue
        if s.lower() in {"无", "没有", "n/a", "none", "null", "待补充", "未知"}:
            continue
        # 口水词：可能有意义的主题词，但作为标签毫无检索价值
        if s in _CHATTER:
            continue
        if s not in out:
            out.append(s)
    return out[:8]


#: 高频虚词/口头禅，绝不该成为标签
_CHATTER = {
    "我们", "你们", "他们", "它们", "这个", "那个", "这些", "那些", "然后",
    "就是", "什么", "怎么", "为什么", "可以", "这样", "那样", "现在", "今天",
    "大家", "其实", "因为", "所以", "但是", "如果", "或者", "而且", "以及",
}


def build_user_prompt(result: SummaryResult, focus: str,
                      strict: bool = True) -> str:
    """构造筛选请求。

    ``focus`` 是用户的关注方向，这是筛选质量的关键 —— 不给方向，
    模型只能用「内容是否完整」这种泛泛标准打分，对用户毫无参考价值。
    """
    meta = result.meta
    # 用摘要而非全文：更省 token，而且摘要已经是浓缩过的信息
    digest = (result.markdown or "")[:6000]

    strict_note = (
        "这是**严格模式**：只有内容对用户当前的学习/工作方向有明确增量才给高分。"
        "一般性内容、不相关的娱乐内容、无实质信息的视频一律给 40 分以下。"
        if strict else
        "这是**宽松模式**：只要内容有实质信息就给中等分，不要过于严苛。"
    )

    focus_block = (
        f"用户当前关注的方向：{focus}\n\n"
        "与关注方向相关的，即使内容本身通俗也应加分；完全无关的内容，"
        "无论信息量多大都应给低分。\n\n"
        if focus else
        "用户没有指定关注方向。请按通用标准评判：优先收录有信息增量、"
        "含专有名词、可指导实践的内容。\n\n"
    )

    return (
        f"请评估下面这个视频的内容，判断它是否值得进入知识库。\n\n"
        f"标题：{meta.title}\n"
        f"作者：{meta.author or '未知'}\n"
        f"平台：{meta.platform_name}\n"
        f"时长：{meta.duration or 0} 秒\n\n"
        f"内容摘要：\n{digest}\n\n"
        f"{focus_block}"
        f"{strict_note}\n\n"
        f"注意：文本来源是"
        + {
            "subtitle": "官方字幕（可能存在识别/翻译误差）",
            "asr": "语音识别文本（可能有识别错误，专业名词容易出错）",
            "metadata": "只有标题和简介，没有正文内容",
            "mixed": "字幕与元信息合并",
        }.get(result.transcript.source, "视频文本")
        + "。"
        "如果是语音识别文本，术语可能有误，请在 entities 里给出你认为正确的写法。"
    )


def judge(result: SummaryResult, engine, focus: str = "",
          strict: bool = True) -> Judgement:
    """让大模型给内容打分。``engine`` 是实现了 ``chat()`` 的 LlmEngine。"""
    system = _SYSTEM
    user = build_user_prompt(result, focus, strict)
    raw = engine.chat(system, user, temperature=0.1, max_tokens=1200)
    data = _extract_json(raw)

    try:
        score = int(float(data.get("score", 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    j = Judgement(
        score=score,
        reason=str(data.get("reason") or "").strip()[:120],
        topics=_clean(_as_list(data.get("topics"))),
        entities=_clean(_as_list(data.get("entities"))),
        actionable=_as_list(data.get("actionable"))[:6],
        terms=_clean(_as_list(data.get("terms"))),
        ok=True,
    )
    return j


def apply_thresholds(j: Judgement, high: int = 80, low: int = 40) -> Judgement:
    """按配置阈值定档。阈值可调 —— 不同人对知识库宽严的偏好差很多。"""
    if j.score >= high:
        j.tier = TIER_HIGH
    elif j.score >= low:
        j.tier = TIER_MID
    else:
        j.tier = TIER_LOW
    return j


def heuristic(result: SummaryResult, focus: str = "") -> Judgement:
    """没配 API Key 时的降级打分。

    刻意做得很保守（分数偏低）：宁可让用户手动存，也不要在没能力判断的时候
    替他做「这个值得存」的承诺。
    """
    text = result.transcript.text or ""
    md = result.markdown or ""
    score = 20
    reasons = []

    if result.transcript.source in ("subtitle", "asr") and len(text) >= 1500:
        score += 18
        reasons.append(f"有较完整的正文（{len(text)} 字）")
    if len(md) >= 400:
        score += 10
    # 专名密度：英文单词是检索锚点，但要过滤掉乱码/重复串
    ents = _extract_entities(text)
    if len(ents) >= 4:
        score += 12
        reasons.append(f"含 {len(ents)} 个专有名词")
    # 有命令/代码/参数，倾向于可执行内容
    if re.search(r"(\$ |```|npm |pip |--\w+=|http)", text):
        score += 10
        reasons.append("含命令或参数，可操作性较强")
    if result.transcript.source == "metadata":
        score -= 10
        reasons.append("仅有元信息，无正文")

    score = max(0, min(100, score))
    j = Judgement(score=score, ok=False,
                  reason="；".join(reasons) or "内容过短，本地无法判断",
                  note="未配置大模型，已用保守的本地规则打分（不如 AI 筛选准确）")
    j.entities = ents[:8]
    return j


#: 出现太多次就说明是语音识别跑偏产生的乱码，不是真专名
_REPEAT_LIMIT = 3
#: 同一个字符连续超过这么多次，一定是乱码（如 xxxxxxxx）
_RUNAWAY = 4


def _extract_entities(text: str, top: int = 8) -> list[str]:
    """从正文里粗提取专有名词。

    只在没有大模型时使用，所以是启发式：按词频排序，过滤掉重复、乱码串
    和无意义的口水词。这里宁可漏掉一些，也**不能把垃圾写进 tags** ——
    污染的标签比缺失的标签危害大得多。
    """
    from collections import Counter

    words = re.findall(r"\b[A-Za-z][A-Za-z0-9+_.-]{2,}\b", text or "")
    counts = Counter(w.strip(".-_") for w in words)
    out: list[str] = []
    for word, n in counts.most_common():
        if len(word) < 3 or len(word) > 40:
            continue
        # 同一个字母连成一片 —— ASR 出错或纯噪声
        if re.fullmatch(r"(.)\1*", word) and len(word) >= _RUNAWAY:
            continue
        if n > _REPEAT_LIMIT and len(out) >= 3:
            # 高频词可能是"我们/这个"之类口水词；已在 out 里攒够实体就跳过
            continue
        out.append(word)
        if len(out) >= top * 3:
            break
    return out[:top]

