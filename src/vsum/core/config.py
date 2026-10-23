"""配置管理：一个 JSON 文件搞定，便携模式优先。

查找顺序（先命中先用）：

1. ``<exe/脚本同目录>/config.json`` —— 便携模式，方便整个文件夹拷走
2. ``%APPDATA%/VideoSummarizer/config.json`` —— 常规安装模式

写回时优先写 1（若目录可写），否则写 2。
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

APP_NAME = "VideoSummarizer"

#: 大模型服务的常用预设。用户只需填 API Key 即可上手。
LLM_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek（深度求索，性价比高，推荐）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "moonshot": {
        "label": "Moonshot / Kimi（长文本强）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
    },
    "qwen": {
        "label": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    "siliconflow": {
        "label": "硅基流动（免费额度）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "ollama": {
        "label": "Ollama（本机）",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
    },
    "custom": {"label": "自定义（OpenAI 兼容接口）", "base_url": "", "model": ""},
}

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "preset": "deepseek",
        "base_url": LLM_PRESETS["deepseek"]["base_url"],
        "api_key": "",
        "model": LLM_PRESETS["deepseek"]["model"],
        "temperature": 0.3,
        "timeout": 180,
        "max_input_chars": 24000,
        "enabled": True,
    },
    "asr": {
        # auto / subtitle / faster-whisper / cloud
        "engine": "auto",
        "model_size": "small",      # tiny|base|small|medium|large-v3
        "language": "zh",
        "device": "auto",           # auto|cpu|cuda
        "compute_type": "int8",
        "cloud_base_url": "",       # 云 ASR（OpenAI 兼容 /audio/transcriptions）
        "cloud_api_key": "",
        "cloud_model": "whisper-large-v3",
        "max_audio_minutes": 90,    # 超过则只取前 N 分钟，防跑飞

        # 浏览器兜底：抖音/快手/小红书这类强风控平台，HTTP 请求拿不到视频，
        # 改用真实浏览器（Playwright + 系统 Edge）打开页面，等播放器发请求，
        # 直接从响应里取视频字节。
        # 代价：慢 10-25 秒、需要装 playwright、运行时可能弹出浏览器窗口。
        # 关掉则这些平台直接失败，不浪费时间在浏览器上。
        "browser_fallback": True,
        "hf_endpoint": "https://hf-mirror.com",   # 模型下载镜像，国内别改回官方
        "model_dir": "",                          # 留空 = %LOCALAPPDATA%/VideoSummarizer/models
    },
    "media": {
        "ffmpeg_path": "",          # 留空则自动探测（内置 ffmpeg -> PATH）
        "max_height": 720,          # 下载画质上限，ASR 不需要高清
        "keep_audio": False,        # 转写后是否保留音频文件
        "cache_dir": "",            # 留空 = 系统临时目录
        "proxy": "",                # 例：http://127.0.0.1:7890
    },
    "cookies": {                    # 各平台 Cookie 原文，风控时粘贴进来即可
        "bilibili": "",
        "douyin": "",
        "kuaishou": "",
        "xiaohongshu": "",
    },
    "summary": {
        "style": "detailed",        # brief | detailed | bullets
        "with_timeline": True,
        "with_keywords": True,
    },
    "obsidian": {
        "enabled": False,            # 总结后自动入库（关掉就是手动点按钮）
        "vault_path": "",            # 留空 = 自动探测（读 obsidian.json，再扫常见位置）
        # 关注方向：写给 AI 筛选器的评判标准。这一栏是知识库质量的决定性因素 ——
        # 留空时模型只能按「内容是否完整」这种泛泛标准打分，对用户毫无参考价值。
        # 默认值针对「库给 AI 检索用 + 严进 + 重取舍逻辑轻操作步骤」的诉求定制，
        # 可在设置里改。人类可读版见 vault 内 00-MOC/知识库关注方向.txt。
        #
        # 撰写要点（改动前先看这里，避免退化）：
        #   * 排除项一律用「直接丢弃」表述，**不要写分数**。曾写「40 分以下不写」
        #     而 threshold_low=60，导致模型以为 50 分可以存、程序直接丢弃。
        #   * 核心判据要能与 curator.py 的评分维度对齐，否则两套标准打架。
        #   * 必须包含满分锚点，否则模型在 80-85 区间反复犹豫。
        "focus": (
            "这个知识库是给 AI 检索系统读取的，不是给人翻的。"
            "判断标准不是「内容好不好看」，而是："
            "这段内容被检索召回后，能不能让 AI 少犯一个错。\n"
            "\n"
            "【一、硬性要求，不满足直接丢弃】\n"
            "1. 结论必须写成可直接执行的规则或事实，不能写成「我踩过这个坑」这类故事。"
            "例如应写成「os.path.splitext() 只按最后一个点切分，对多级扩展名会误判；"
            "判 md 文件名应用 f[:-3] if f.endswith('.md') else f」，"
            "而不是「我一开始用 splitext 剥扩展名，把 .spec 也剥掉了」。\n"
            "2. 笔记必须能脱离原视频独立成立，"
            "禁止出现「上文第 3 点」「如前所述」这类依赖上下文的表述。\n"
            "3. 专有名词（库名/函数名/协议名/模型名/参数名）一律保持原样不翻译，"
            "因为 RAG 的 query 就是这些词。\n"
            "4. 有结论但没有依据——只说「应该这样做」不说「为什么」——的丢弃。\n"
            "5. 视频场景专属：若文本来源是语音识别，专有名词可能转写错误。"
            "entities 里必须给出你判断的正确写法，并标注 uncertain: true。"
            "宁可漏掉一个专名，也不要写入一个错的——错锚点会持续污染检索。\n"
            "\n"
            "【二、核心判据：这段内容有没有取舍判断】\n"
            "评分看的是「有没有讲清为什么这么选」，不是「有没有操作步骤」。\n"
            "收录：取舍逻辑、现象背后的原理、踩坑的根因（要到根因，不要「重启就好了」）。\n"
            "收录：通过案例体现的判断——如「简历诊断」「面试复盘」这类，"
            "若包含「为什么这样写而不是那样」，同样收录；若只是模板套话则丢弃。"
            "求职类内容的判据由此统一：不看主题，看有没有判断。\n"
            "降权：单个 API 的用法查询（三分钟能百度到的不占名额）。\n"
            "降权：某库某版本的临时教程（过期即污染）。\n"
            "原理能推导出一批做法，结论不行。\n"
            "\n"
            "【三、满分锚点，90 分以上大致是这些形态】\n"
            "- 「X 在 Y 条件下会失效，因为 Z」——带失效边界的原理\n"
            "- 「先用 A，QPS 超过 X 后换 B」——有量化切换点的决策\n"
            "- 「这个看似冗余的步骤，去掉后会在 Y 场景崩」——反直觉且有因果的结论\n"
            "- 「某工具的坑不是文档写的那个，真正原因在源码的某行」——根因定位\n"
            "\n"
            "【四、一律丢弃，不给分不写文件】\n"
            "1. 无实测的 AI 生成内容与聚合页：AI 综述、UGC 知识卡、"
            "自相矛盾的聚合结论。判据是有没有一手来源、具体数字或可复现步骤。"
            "（教训：这类内容曾把数据标注平台清单带偏，结论完全相反。）\n"
            "2. 纯资讯与模型发布新闻——除非带来可复现的行为变更或兼容性结论。\n"
            "3. 标题党、搬运、无具体主张的观点。\n"
            "4. 与我当前方向完全无关且无法迁移的内容。\n"
            "\n"
            "【五、打分倾向】\n"
            "果断打分，不要都给 70-80 分。严进：宁可漏掉不可堆积。"
            "100 篇 85 分的笔记，价值远高于 500 篇 60 分的——重复内容会稀释每次检索。"
            "低于 60 分不写文件。\n"
            "\n"
            "【六、领域优先级，质量相当时按此取舍】\n"
            "1. LLM 应用工程：Agent 架构、RAG/向量检索、上下文工程、推理优化、"
            "提示词工程、模型选型与评测、防幻觉设计；\n"
            "2. 工程能力：Python 进阶、异步、打包分发、跨平台踩坑、调试与性能定位；\n"
            "3. 求职与项目包装：简历、项目叙事、技术面试评判维度、作品集呈现；\n"
            "4. 变现路径：接单平台、AI 工具产品化、一手实操数据"
            "（第 4 类只收录赚过钱的做法，纯理论推演不给分）。\n"
            "\n"
            "【七、非技术类内容：换一把尺子单独评】\n"
            "社科、体制、经济、历史、商业案例类内容，**不适用第二节的取舍逻辑判据**"
            "（它们不讲技术选型，套用会一律落到 40 分以下，这是误判）。"
            "这类内容改按「机制解释质量」评分：\n"
            "- 收录：讲清了机制背后的因果链。哪个制度安排导致了什么结果、"
            "为什么会这样演化、有哪些约束条件在起作用。\n"
            "- 收录：提供了可迁移的分析框架，能用来判断同类新问题"
            "（如「土地财政」这个框架可以用来分析其他地方性财政问题）。\n"
            "- 收录：有具体数据支撑的实证分析（数字、样本、时间跨度可核对）。\n"
            "- 降权：只有立场没有论证的评论、通俗化的「历史科普」"
            "（讲了什么但不解释为什么）、事后归因（用结果倒推单一原因）。\n"
            "- 降权：把复杂现象归因为单一原因的内容。"
            "真实的社会问题几乎都是多因素交互的结果。\n"
            "判别方法：如果你看完能用这套说法去**预测或解释另一个同类现象**，"
            "那它是机制解释；如果只是记住了故事，那是科普，不存。\n"
            "本节不要求有操作步骤——社科内容的价值在于判断力，不在于可执行。"
        ),
        "strict": True,              # 严格模式：宁可少存，不污染知识库
        "threshold_high": 80,        # ≥ 该分数进精选目录
        "threshold_low": 60,         # ≥ 该分数进待阅目录；低于则不写文件
        # 目录名按用户的 PARA 结构预设，可在设置里改
        "folder_high": "20-精选",
        "folder_mid": "30-待定",
        "folder_moc": "00-MOC",      # 视频索引放这里，与已有导航页并列
    },
    "ui": {
        "watch_clipboard": False,
        "auto_summary_on_paste": False,
    },
    "rag": {
        # RAG（语义检索问答）：把字幕 / 笔记切块向量化后建库，之后可以
        # 直接提问「我之前看过的 XX 是怎么说的」，答案带出处。
        "enabled": True,
        # 向量化引擎与向量库，对应插件注册表里的 key
        "embedder": "siliconflow",
        "store": "local-json",

        # 下面两项**留空时会自动复用 asr.cloud_base_url / asr.cloud_api_key**。
        # 两者本来就是同一个服务商，没必要让用户把同一个 Key 抄两遍。
        # 想把 embedding 换成别家（OpenAI / 本地模型）时，填上即可覆盖。
        "base_url": "",
        "api_key": "",
        "embed_model": "BAAI/bge-m3",     # 1024 维；换模型必须重建库

        # 库目录。留空 = 与 config.json 同目录的 rag_store，
        # 这样整个文件夹拷到别的机器时库跟着走。
        "store_path": "",

        # 检索参数
        "top_k": 5,                       # 每次取回多少个片段送进提示词
        # 相似度下限。**别凭感觉设 0.7/0.8** —— bge-m3 在中文上的绝对值偏低
        # （实测相关句约 0.52、无关句约 0.40），阈值要用自己的语料校准。
        "min_score": 0.0,

        # 切块尺寸。字幕按说话节奏切，块可以小一点；文档段落是完整语义单元，
        # 切太碎反而丢上下文，所以给得大一些。
        "subtitle_chars": 600,
        "doc_chars": 800,
        "doc_overlap": 100,

        # 总结完成后自动把这次的内容也入库（做「越用越全」的库时打开）
        "auto_ingest": False,
    },
}


def user_config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def _portable_path() -> str:
    if getattr(sys, "frozen", False):          # PyInstaller 打包后
        root = os.path.dirname(sys.executable)
    else:
        # .../项目根/src/vsum/core/config.py -> 项目根
        root = str(Path(os.path.abspath(__file__)).parents[3])
    return os.path.join(root, "config.json")


def _deep_merge(base: dict, patch: dict) -> dict:
    out = deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """带默认值合并的配置对象，用属性访问：``cfg.llm.model``。"""

    def __init__(self, data: dict | None = None, path: str | None = None):
        self._data = _deep_merge(DEFAULT_CONFIG, data or {})
        self.path = path
        #: 本次是否是「磁盘上一份配置都没有，程序刚替你落了一份默认配置」。
        #: UI 据此提示用户「配置已生成在 X，可以直接用记事本改」——
        #: 否则用户只知道设置界面里那一大段关注方向，却不知道它存在哪个文件里。
        self.created = False

    # ---- 访问 ----
    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            val = self._data[name]
            return _Section(val) if isinstance(val, dict) else val
        raise AttributeError(name)

    def get(self, dotted: str, default=None):
        """按 ``"llm.model"`` 这种点号路径取值。"""
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any):
        cur = self._data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
            if not isinstance(cur, dict):
                return
        cur[parts[-1]] = value

    def apply_preset(self, preset_key: str) -> bool:
        """套用内置服务商预设（只改 base_url/model，不动 api_key）。"""
        p = LLM_PRESETS.get(preset_key)
        if not p:
            return False
        self.set("llm.preset", preset_key)
        if p.get("base_url"):
            self.set("llm.base_url", p["base_url"])
        if p.get("model"):
            self.set("llm.model", p["model"])
        return True

    def as_dict(self) -> dict:
        return deepcopy(self._data)

    # ---- 持久化 ----
    def save(self, path: str | None = None) -> str:
        target = path or self.path or _portable_path()
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            self.path = target
            return target
        except OSError:
            fallback = os.path.join(user_config_dir(), "config.json")
            os.makedirs(user_config_dir(), exist_ok=True)
            with open(fallback, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            self.path = fallback
            return fallback

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        candidates = [path] if path else [_portable_path(),
                                          os.path.join(user_config_dir(), "config.json")]
        for c in candidates:
            if c and os.path.isfile(c):
                try:
                    with open(c, "r", encoding="utf-8") as f:
                        return cls(json.load(f), c)
                except (json.JSONDecodeError, OSError):
                    continue
        # 一个配置文件都没有 —— **立刻在磁盘上落一份**。
        #
        # 不落盘的话，用户拿到的是一套「只活在 exe 内部」的默认值：设置界面里看得见
        # 关注方向那一大段文字，却在磁盘上找不到对应文件，想手工改都无从下手。
        # 这正是用户提出的问题：「关注方向应该是可以写入 config 里根据用户自定义的，
        # 而不是伴随在 EXE 程序本体里面」。
        #
        # 两条护栏，别省：
        #   1. 调用方**显式传了 path**（测试、二次开发）时不自动造文件 ——
        #      否则一次 `Config.load("D:/不存在.json")` 就会把默认值写到
        #      _portable_path() 上，把项目根目录那份真配置覆盖掉。
        #   2. 目标文件已存在就绝不写 —— 走到这里只可能是它读不出来（JSON 损坏），
        #      这时默默覆盖等于把人家残留的 Key 一起抹掉，不如原样留着让用户自己看。
        if path:
            return cls(None, path)
        cfg = cls(None, _portable_path())
        try:
            if not os.path.exists(cfg.path):
                cfg.save()
                cfg.created = True
        except Exception:
            pass
        return cfg


class _Section:
    """让 ``cfg.llm.model`` 这种链式访问可用。"""

    def __init__(self, data: dict):
        self.__dict__["_d"] = data

    def __getattr__(self, name):
        d = self.__dict__["_d"]
        if name in d:
            v = d[name]
            return _Section(v) if isinstance(v, dict) else v
        raise AttributeError(name)

    def get(self, key, default=None):
        return self.__dict__["_d"].get(key, default)

    def to_dict(self):
        return deepcopy(self.__dict__["_d"])
