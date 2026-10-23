# 架构说明与文件清单

## 1. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│ 入口层                                                        │
│   run_ui.py（桌面界面）      run_cli.py（命令行）              │
└───────────────┬──────────────────────────┬───────────────────┘
                │                          │
┌───────────────▼──────────────────────────▼───────────────────┐
│ 编排层  core/pipeline.py :: Pipeline                          │
│   提取链接 → 匹配平台 → 拿文本 → 摘要 → SummaryResult          │
│                                    ↓ 可选，失败不影响上面       │
│                          obsidian/（AI 筛选 → 写 .md）        │
└───────┬──────────────┬───────────────┬──────────────┬────────┘
        │              │               │              │
┌───────▼────┐ ┌───────▼──────┐ ┌──────▼─────┐ ┌──────▼────────┐
│ platforms/ │ │ media/       │ │ asr/       │ │ llm/          │
│ 平台适配器 │ │ yt-dlp +ffmpeg│ │ 语音转写   │ │ 摘要 + 对话   │
└────────────┘ └──────────────┘ └────────────┘ └───────────────┘
        ▲              ▲               ▲              ▲
        └──────────────┴───────────────┴──────────────┘
                    core/registry.py 插件注册表
                  （@register_platform / _asr / _llm）
```

**核心思想**：一切能力都是插件。流水线只认接口，不认具体实现，
所以新增平台/引擎不需要动主流程，`plugins/` 目录里的 `.py` 会自动被加载。

## 2. 文件清单

### 入口

| 文件 | 作用 |
| --- | --- |
| `run_ui.py` | 启动本地服务 + 桌面窗口（可 `--browser` 强制浏览器） |
| `run_cli.py` | 命令行：`vsum "链接" [--asr] [--style] [--out] [--json] [--list]` |

### `src/vsum/core/`

| 文件 | 作用 |
| --- | --- |
| `pipeline.py` | **主流水线**。串起全流程，负责每一步的降级与进度上报 |
| `registry.py` | 插件注册表：`register_platform/asr/llm`、`autoload`、`load_external_plugins` |
| `config.py` | 配置对象（点号路径读写、JSON 持久化、便携模式优先） |
| `context.py` | 运行时上下文：配置 / HTTP / 工作目录 / 进度回调 |
| `models.py` | `VideoMeta` `Segment` `Transcript` `SummaryResult` + SRT/时间格式化 |
| `errors.py` | 异常体系，`friendly=True` 的会直接展示给用户（不再抛堆栈） |
| `net.py` | HTTP 客户端：重试、UA 轮换、Cookie、代理、短链还原 |
| `ffmpeg.py` | ffmpeg 定位（内置 → PATH → 常见目录 → 联网装）与音频抽取 |
| `share.py` | 从「分享文案」里抠链接与标题（处理 URL 后紧跟中文的情况） |
| `exporters.py` | 导出 Markdown / 纯文本 / SRT / JSON |

### `src/vsum/platforms/`

| 文件 | 平台 | 等级 |
| --- | --- | --- |
| `base.py` | 适配器基类（含 `meta_only_transcript` 兜底） | — |
| `bilibili.py` | 哔哩哔哩：wbi 签名、字幕接口、弹幕 | ★★★ |
| `douyin.py` | 抖音：短链还原、`RENDER_DATA`/`_ROUTER_DATA` 提取 | ★★ |
| `kuaishou.py` | 快手 | ★ |
| `xiaohongshu.py` | 小红书 | ★ |
| `weixin_channels.py` | 微信视频号 | ★ |
| `share_page.py` | 上面三家的公共基类（短链 + meta + 内嵌 JSON） | — |
| `article.py` | 网页文章抽取（`ArticleAdapter`）+ **本地文档 / 纯文本**（`PlainTextAdapter`），拖拽进来的 PDF/Word/Excel/PPT/EPUB/RTF/字幕/网页存档都走这里 | ★★★ |
| `generic_ytdlp.py` | 通用兜底（yt-dlp），覆盖上千站点 | ★★ |

#### 本地文档支持（`PlainTextAdapter`）

拖拽进来的文件走「上传 → 落盘 → 当本地路径解析」这条路，支持：

| 后缀 | 解析器 | 备注 |
| --- | --- | --- |
| `.txt .md .csv .tsv .json .xml .yaml .ini .toml .log .tex .sql .rst` | 内置读取 | 依次尝试 UTF-8-BOM / UTF-8 / GBK / Big5 / Latin-1 |
| `.pdf` | `pypdf` | 多页拼接；**无文字层（扫描件）明确报错**，不退化成「拿文件名当标题」 |
| `.docx` | `python-docx` | 按文档流遍历段落与表格，表格不会被挪到末尾 |
| `.xlsx` | `openpyxl` | 每个工作表逐行读，单元格用 `\|` 分隔；`data_only=True` 取公式结果 |
| `.xls` | `xlrd` | 旧版二进制表格。xlrd 2.x 只支持 `.xls`，正好对口 |
| `.pptx` | `python-pptx` | 每页文字 + 表格 + 演讲备注，递归展开 group 形状 |
| `.epub` | 标准库 `zipfile` | 按 OPF spine 顺序拼章节，**零新依赖** |
| `.odt .ods .odp` | 标准库 `zipfile` + `ElementTree` | OpenDocument 三件套，与 EPUB 同源思路 |
| `.rtf` | 标准库（自研剥离器） | 见下方「RTF 为什么要按字节解析」 |
| `.srt .vtt .ass .ssa .sbv` | 标准库（清洗） | 洗掉序号/时间轴/样式标签，相邻重复行合并 |
| `.html .htm .mhtml` | `core/article.py` + 标准库 `email` | 抽不到正文时退回整页清洗；`.mhtml` 取 `text/html` 段 |

可选依赖都是「延迟 import」：缺了只丢一条带安装命令的错误，不影响其他功能。
打包时见 `VideoSummarizer.spec` 的 `doc_mods` / `doc_datas` —— **docx/pptx 自带模板数据文件，
漏收会在运行时报「找不到 default.pptx」**。

#### 按内容识别（`_sniff_container` / `sniff_document`）

**后缀只是线索，内容才是准绳。** 读取前先看文件头：

| 文件头 | 判定 |
| --- | --- |
| `%PDF-` | `.pdf` |
| `{\rtf` | `.rtf` |
| `D0 CF 11 E0 A1 B1 1A E1` | OLE2 —— doc/xls/ppt 97-2003 **共用**这个头，要再靠内部流名细分 |
| `PK\x03\x04` | zip 容器 → 看内部条目：`word/document.xml`→docx、`ppt/presentation.xml`→pptx、`xl/workbook.xml`→xlsx、`mimetype`→odt/ods/odp/epub |
| `From:` / `MIME-Version:` | `.mhtml`（**必须先判**，因为 MHTML 正文里就嵌着 `<html><head>`） |
| `<!doctype html` / `<head>` | `.html` |

于是三种以前必然失败的场景现在都能救回来：**WPS 存成 `.wps/.et/.dps`**、
**用户手动改错后缀**、**浏览器下载丢了后缀**。

OLE2 不好细分，但也不是没辙 —— `_ole2_kind()` 扫内部流名
（`WordDocument` / `Workbook` / `PowerPoint Document`，以 UTF-16LE 明文存放），
比完整实现复合文档解析划算得多。认出来是 `.xls` 就直接交给 xlrd 读，
认出来是 `.doc`/`.ppt` 才给「另存为」的指引。

> **一条容易踩的坑**：`.xls` 也是 OLE2，如果只按「OLE2 = 老格式读不了」处理，
> 真 `.xls` 会被误拒（这个 bug 是冒烟测试当场抓出来的）。

> 另一条：本地路径**没有主机名**（`urlparse("D:/a.pdf").netloc` 是空串）。
> `Pipeline.detect()` 里有一层「是不是文章站」的判断，如果让本地路径也走那层逻辑，
> 会被判成「视频站」而转给 generic（yt-dlp）——症状是拖进去的 PDF 永远解析不了。
> 所以 `PlainTextAdapter` 命中时**直接返回**，不参与主机判断。

> **RTF 为什么要按字节解析**：RTF 里中文有两条写法 —— `\uNNNN`（Unicode 转义）
> 和 `\'hh`（当前代码页的字节）。如果先 `decode()` 再解析，`\'hh` 已经被解成
> 乱码字符、再也还原不回来。所以实现是：**以 latin-1 把字节 1:1 映成字符**，
> 剥离控制字时把普通字符与 `\'hh` 都攒回字节缓冲，最后按文档声明的
> `\ansicpg`（中文 Word 是 936）统一解码。`\uNNNN` 后面跟的那 `\uc` 个替代字符
> 必须丢掉，否则每个汉字都会多出一个乱码。

### `src/vsum/asr/` 与 `src/vsum/llm/`

| 文件 | 作用 |
| --- | --- |
| `asr/base.py` | 转写引擎基类 |
| `asr/faster_whisper.py` | 本地 Whisper。自己解码 PCM（绕开 PyAV 版本坑）、镜像下载、断点重试 |
| `asr/cloud.py` | 云端 ASR（OpenAI 兼容 `/audio/transcriptions`）。**长音频（>10 分钟）自动切 5 分钟片逐段上传**：单请求传几十 MB 大文件实测易超时（35 分钟/66MB wav 单次约 10 分钟，网络稍差即超时，且已识别部分全部作废）；分片后单片几 MB、失败只重试该片，时间戳按片起点平移。失败时音频保留在缓存，重试无需重新下载 |
| `llm/base.py` | 摘要引擎基类 + **提示词构造**（三种风格、时间戳、元信息模式提示）+ `chat()` 抽象 |
| `llm/openai_compat.py` | OpenAI 兼容接口，支持流式摘要与非流式对话 |
| `llm/extractive.py` | 本地抽取式摘要：零配置兜底，句打分 + 时间线 + 关键词 |

### `src/vsum/obsidian/`

知识库入库。**直接写 `.md` 文件**——不装 Obsidian 插件、不需要 Obsidian 在运行
（官方没有写入 API，社区插件方案要求 Obsidian 保持运行，太脆）。

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 对外只暴露 `status()` / `save()`。**永不抛错打断主流程** |
| `vault.py` | vault 定位、文件名清洗、原子写入、路径逃逸拦截 |
| `curator.py` | AI 筛选器（五维打分 → 三档）+ 无 Key 时的保守本地打分 |
| `note.py` | 笔记正文重写、YAML 头组装、`[[双链]]` 挂载 |
| `frontmatter.py` | 手写最小 YAML 生成（不引第三方库，打包体积敏感） |
| `knowledge.py` | 术语表 + MOC 视频索引的生成与维护 |

**设计要点**：

- **筛选标准是面向检索的，不是面向"好看"**。知识库未来会被 RAG 读，
  而 `top-k` 召回位置有限 —— 一段视频讲得再透，没有专实体就永远召不回来，
  对知识库零贡献。所以「专实体密度」占 20%、「信息增量」占 35%。
- **分级而非全存**。低分不写文件。知识库的成本不是空间，是三个月后没人愿意翻。
- **AI 挂了要能降级**。筛选和重写都走 `LlmEngine.chat()`，
  任何一步失败都退回摘要原文并在笔记里标注降级原因，不丢信息。

#### 踩过的坑

| 坑 | 现象 | 教训 |
| --- | --- | --- |
| `[[双链]]` 污染 YAML 头 | `entities: [LangGraph, [[2026-10-01 xxx]]]` 让整篇笔记解析失败 | 双链替换必须在拼 YAML 头**之前**完成，且 frontmatter 里只放纯文本 |
| 自动探测抢用户填的路径 | 路径填错一个字符，程序悄悄写进另一个库 | `find_vault(hint)` 填了路径就只认它，不存在直接返回 `None` |
| 强制保存后元数据自相矛盾 | `value: 20` 配 `tier: 精选` | 改档位必须同步抬分数，并留痕"用户手动强制入库" |
| 模型返回垃圾标签 | 几百字符的 `xxxxx` 进了 tags 和 entities | 启发式和 AI 双向清洗：滤乱码串（`(.)\1{4,}`）、滤重复、滤口水词 |
| 空串字段弄脏 YAML | `author: ""` | `dump()` 同时跳过 `None` 和 `""`，但**不能**跳 `0` 和 `False` |

### `src/vsum/ui/`

| 文件 | 作用 |
| --- | --- |
| `server.py` | 标准库起的本地 HTTP 服务 + 任务池（提交/轮询/配置/导出/入库） |
| `launcher.py` | 启动器：Edge WebView2 → WinForms → 浏览器的三级降级 |
| `assets/index.html` | 前端页面（ES5 写法，IE 内核也能渲染；自研极简 Markdown 渲染；拖拽上传 + 文件选择） |

#### HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/config` `/api/platforms` `/api/presets` `/api/status` `/api/obsidian` `/api/health` | 查询类 |
| POST | `/api/summarize` | 提交总结任务（`{text}`；**本地文档就是把落盘路径当 text 传**） |
| POST | `/api/upload` | 接收拖拽文件：原始字节 + `X-Filename` 请求头（percent-encoded），落盘后返回绝对路径 |
| POST | `/api/cancel` `/api/config` `/api/obsidian` `/api/export` | 取消 / 存配置 / 入库 / 导出 |

`/api/upload` 的三个设计取舍：

1. **不用 multipart** —— Python 3.13 已移除 `cgi` 模块，标准库不再带 multipart 解析器。
   传原始字节、文件名放请求头，零依赖且中文名不会乱码（前端 `encodeURIComponent`，后端 `unquote`）。
2. **文件名必须洗成 basename** —— 文件名来自浏览器，可以塞进 `..\..\x`
   这类路径，直接拼进目录就是目录穿越。见 `server._safe_name()`。
3. **类型校验放在落盘之后** —— 先存、再用 `sniff_document()` 按内容认一次。
   只按后缀拦的话，WPS 的 `.wps/.et/.dps`、丢了后缀的下载件会在这一关就被拒，
   后端明明能读却用不上。认不出来才删文件并返回 400，提示按**类别**列出
   （而不是把四十多个后缀倒成一长串）。

## 3. 数据流与降级矩阵

| 场景 | 走的链路 | 结果 |
| --- | --- | --- |
| 有官方/AI 字幕 | `adapter.fetch_subtitle()` | `source=subtitle`，最准 |
| 无字幕，能下载音频 | yt-dlp 取流 → ffmpeg 抽 16k wav → 本地 Whisper | `source=asr` |
| 无字幕 + 配了云端 ASR | 同上，但走云 API | `source=asr` |
| 音频也拿不到 | `adapter.meta_only_transcript()` | `source=metadata`，界面明确标注 |
| 大模型可用 | OpenAI 兼容接口，流式输出 | `engine=OpenAI 兼容接口` |
| 大模型不可用/失败 | 本地抽取式 | `engine=本地抽取式摘要（无需 API Key）` |

入库这条链路同样有降级矩阵（**任何一格出问题都不影响总结结果本身**）：

| 场景 | 结果 |
| --- | --- |
| AI 可用 | 五维打分 → 精选 / 待阅 / 丢弃 |
| 没配 API Key | 保守本地打分，分数普遍偏低，可能什么都不写 |
| AI 筛选失败 | 同上，笔记里标注「已降级为本地打分」 |
| AI 重写失败 | 用摘要原文，正文顶部标注降级提示 |
| vault 没找到 / 路径错 | 返回 `ok=False` + 可读原因，不抛异常 |

`asr.engine` 配置决定行为：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 字幕 → 本地 Whisper → 云端 ASR → 元信息 |
| `subtitle` | 字幕 → 元信息（不下载音频，最快） |
| `faster-whisper` / `cloud` | 跳过字幕，直接用指定引擎 |

解析元数据这一步单独有一条降级链（**平台风控是常态，不是异常**）：

| 场景 | 结果 |
| --- | --- |
| 专用适配器成功 | 正常 |
| 专用适配器撞风控页 | `is_captcha_page()` 识别出验证页 → 抛带针对性 hint 的 `FetchError` |
| 专用适配器失败 → 通用兜底成功 | 用通用兜底的结果 |
| 两者都失败 | **回退到专用适配器的错误**（它知道该配哪个站的 Cookie，通用兜底不知道） |
| 底层 yt-dlp 报错 | 经 `_reason_of()` 剥掉 Python 异常包装，`explain_failure()` 翻成「下一步做什么」 |

## 4. 平台风控与错误透传

这一块的坑集中出现过一次用户投诉（「填了 API Key，抖音链接显示无法解析」），值得单独记。

### 4.1 风控页**不是空标题**

抖音/快手/小红书在未登录 IP 或高频访问时返回验证页。这些页面的 `<title>`
**有内容**（抖音是「验证码中间页」），所以 `if not title` 这种判空拦不住 ——
结果适配器把「验证码中间页」当成正常视频标题返回，**不报错**，
界面看着像"解析不出来"，日志里也看不出哪里不对。

对策是 `platforms/base.py` 的 `is_captcha_page(*texts)`：按关键词识别
（验证码 / 安全验证 / captcha / just a moment / 访问过于频繁 …），
各家适配器的 `resolve()` 收尾都要过一下。

**误伤比漏检更糟**：正常视频被当成风控页，用户就永远解析不了这个平台了。
所以判据只用平台风控页特有的措辞，不用泛化词。

### 4.2 yt-dlp 的报错必须透出

yt-dlp 的报错往往精确到能直接照着做 ——
`Fresh cookies (not necessarily logged in) are needed` 直接告诉你「去配 Cookie」。
而 `extract_info()` 原来失败时只写 WARNING 然后返回 None，
**这层信息彻底丢失**，上层只能笼统报「无法解析该链接」。

现在：

- `media/ytdlp.py` 用 `_LAST_ERROR` 记住最近一次的原始原因（按 URL 匹配）
- `_reason_of()` 剥掉 Python 异常包装 —— yt-dlp 的 `DownloadError.exc_info`
  是**三元组**，直接 `str()` 会打出 `(<class '...'>, <traceback object at 0x...>)`
  这种带内存地址的表示，用户完全看不懂
- `explain_failure()` 按关键词把报错翻成「下一步做什么」
- `generic_ytdlp.resolve()` 把原始原因拼进 `FetchError` 的 message

### 4.3 pipeline 不得覆盖专用适配器的准确原因

专用适配器知道「抖音要配 douyin 的 Cookie」，通用兜底不知道。
所以：通用兜底也失败时，`last_err` 要**回退到专用适配器的 `FetchError`**
（且只在该错误带 hint 时回退）；并且 `raise` 时保留原异常的 hint，
不要用一句通用的「海外站点请配置代理」覆盖 —— 抖音是国内站点，那纯误导。

### 4.4 插件注册表为空 ≠ 正常

平台适配器靠 `@register_platform` 装饰器注册。如果调用方忘了 `load_all()`，
`detect()` 会静默返回通用兜底，于是**所有平台链接都走 yt-dlp**，
表现和「平台不支持」一模一样。

`Pipeline.detect()` 现在有自愈：判断依据是**关键平台是否齐全**
（`_platforms_ready()`），而不是「注册表是否为空」——
因为只要有任何适配器模块被 import 过，注册表就非空了。

⚠️ 这个判断必须在 `import` 任何适配器**之前**做：
import 本身会触发注册装饰器，先 import 就永远轮不到自愈。

`extensions.load_builtin()` 末尾还有一道自检，缺哪个平台都会打 WARNING。

## 5. 打包

| 产物 | 类型 | 说明 |
| --- | --- | --- |
| `dist/VideoSummarizer.exe` | 无控制台 | 桌面界面（自包含单文件） |
| `dist/vsum.exe` | 有控制台 | 命令行（自包含单文件） |

**必须分两次构建**（`build.bat` 已封装）。spec 里两个 EXE 若放在同一次运行，
第二个 EXE 会先把第一个生成的 `base_library.zip` 整包删掉——在有批量删除保护
（安全软件 / 企业策略）的机器上会被拦下，结果只产出界面版、没有命令行版。
所以用环境变量 `VSUM_TARGET=gui|cli` 选择目标，并各自指定独立 `--workpath`：

```bat
set VSUM_TARGET=gui && pyinstaller VideoSummarizer.spec --noconfirm --workpath build\gui --distpath dist
set VSUM_TARGET=cli && pyinstaller VideoSummarizer.spec --noconfirm --workpath build\cli --distpath dist
```

> **单文件，且没有 `_internal\`（v16 起）**
>
> spec 里**只写 `EXE`、不写 `COLLECT`**，所以产物就是两个自包含的单文件 exe，
> 发给别人只需拷一个文件。
>
> 早先的 spec 同时写了 `EXE(binaries, datas, ...)` 和 `COLLECT(...)`，把同一套依赖
> 生成了两份：exe 内嵌一份（onefile 行为，启动解压到 `%TEMP%`）+ `_internal\` 一份
> （418-500 MB）。实测 2026-10-09：带 `_internal` 启动 4.73s / 4.64s；把 exe 单独拷到
> 空目录启动 4.49s / 4.52s —— **完全一致**，即 `_internal\` 从未被读取，是纯冗余。
> 因此删掉 `COLLECT`。README 旧版「根目录 exe 缺 `_internal` 会报错」的说法是错的。

要点：

* **ffmpeg 随包**：取 `imageio-ffmpeg` 自带的二进制，用户不用自己装；
* **yt-dlp 全量 extractor**：`collect_submodules("yt_dlp")`，否则打包后只认得少数站点；
* **playwright 是必需依赖，缺了直接中止构建**（2026-10-10 定）。
  抖音 / 快手 / 小红的浏览器兜底全靠它。两条都要做对：
  ① `collect_submodules("playwright")` 进 `hiddenimports`；
  ② `collect_data_files("playwright")`（driver，即 node 内核）**必须进 `datas`** ——
  只收模块不收 driver 的话，构建照样"成功"，等到运行时 `launch()` 才报错。
  另外 `greenlet` / `pyee` 是它的依赖且属**顶层包**，`collect_submodules("playwright")`
  带不出来，要显式加（playwright 的 import 在函数体内，静态分析容易漏）。
* **浏览器内核也打进 exe（v18 / B1 方案，2026-10-10 定）**，同样硬校验。
  `collect_data_files("playwright")` 只带**驱动**（node + 协议实现，约 100 MB），
  浏览器本体在 `%LOCALAPPDATA%\ms-playwright`（271 MB），**不在包里**。
  于是"打包了 playwright"≠"对方机器上有浏览器" ——
  把 exe 发给没装 Edge/Chrome 的人，抖音兜底整个失效且看不出原因。
  所以 `tools/prepare_browser_vendor.py` 生成本机内核的**精简副本**
  （271 → 219 MB，剔掉 220 种 locales / LICENSE / hyphen-data / SwiftShader Vulkan），
  放进 `vendor/pw-browsers/`，spec 再以 `datas` 打进 exe（压缩后约 +100 MB）。
  运行时 `browser_fetch._bundled_shell()` 从 `sys._MEIPASS` 找到它，
  作为「系统 Edge → 系统 Chrome → **内置精简内核** → 本机内核」的第三级。
  内置的是 `chrome-headless-shell`，**只能无头**，故仅在 `headless=True` 时参与。
* **前端页面作为 data 文件**打进去（`vsum/ui/assets/index.html`）；
* `excludes` 掉了 Qt / tkinter / 科学计算栈，减小体积。

> **教训：别把必需能力写成 `find_spec(...) if ... else []` 的"静默可选"。**
>
> spec 里原先有这么一段：`pw_mods = collect_submodules("playwright") if find_spec("playwright") else []`。
> 当时的想法是"可选功能不该拖垮构建"，实际后果是——某次 venv 里没了这个包，
> **之后每一版 exe 都静默少了这块能力**，而用户侧只看到"某个平台解析失败"这一个模糊症状，
> 从表象根本推不到"包里没带"。实测归档条目数：老版 `dist` 有 182 条 playwright，
> `dist_v13` / `dist_v15` / `dist_v16` 全是 0。
>
> 规则：**必需能力一律在构建时硬校验（缺了就 `raise SystemExit`）**，
> 只有真正"没有也能用"的才允许静默跳过（如文档解析库 —— 缺了只影响拖文件）。
> 配套加了 `vsum --check`，让"这份 exe 到底带了什么"可以被一条命令验证。

## 6. 实测记录（2026-10）

| 项 | 结果 |
| --- | --- |
| B站 元数据 / 弹幕 | ✅ 通过 |
| B站 字幕接口 | ⚠️ 无 Cookie 时返回空列表（B站要求登录态），已自动降级到音频转写 |
| B站 音频下载 + ffmpeg 抽音 | ✅ 通过（20 秒音频约 0.4s） |
| 本地 Whisper 转写 | ✅ 通过（tiny 模型，45 秒音频约 1.5-3s，中文可识别） |
| 抖音/快手/小红书 | ⚠️ 只能到元信息级，**需 Cookie**（风控所致；未登录 IP 必被拦） |
| 抖音风控页识别 | ✅ 通过（未登录时返回「验证码中间页」，程序抛可读的 `FetchError` 而非当成功） |
| 平台插件注册自愈 | ✅ 通过（`detect()` 在插件未装载时自动补全，六个内置平台均命中） |
| Web API 全流程 | ✅ 通过 |
| PyInstaller 打包 | ✅ 通过（v17：界面版 206 MB / 命令行版 205 MB；v18 内置内核后约 310 MB） |
| 打包后插件注册 | ✅ `--list` 显示 10 个插件全在 |
| 打包后 ASR | ✅ 通过（small 模型，34.8s 完成一次完整流程） |
| 打包后中文输出 | ✅ 通过（控制台强制切 65001 代码页 + UTF-8 重配置） |
| 浏览器兜底（playwright + 系统 Edge） | ✅ 通过（`--check` 实测启动 Edge 154.0.4258.37；v17 之前此项在各版 exe 中**均为缺失**，属打包静默降级） |
| 精简内核副本可独立运行 | ✅ 通过（219 MB 副本用 `executable_path` 启动，3.25s，Chromium 153.0.8010.12，抖音 `s_v_web_id` **拿到** = 过风控） |
| v18 exe 内置内核 | ✅ 通过（见下方「v18 验证」小节） |

### v18 验证（2026-10-10，内置浏览器内核）

| 项 | 实测结果 |
| --- | --- |
| 精简内核副本独立启动 | ✅ 3.25s，Chromium 153.0.8010.12（219 MB 副本，`executable_path` 指定） |
| 精简内核能否过抖音风控 | ✅ 拿到 `s_v_web_id`（纯 JS 生成的凭证，拿到 = JS 正常执行过） |
| exe 归档里有内核 | ✅ `chrome-headless-shell.exe` / `locales\zh-CN.pak` / `pw-browsers` 均在 |
| **exe 解压出来的那份内核**端到端 | ✅ 解压完成 4.4s → 启动 3.25s → 打开抖音拿到 `s_v_web_id`；目录 11 项齐全 |
| 体积 | 界面版 321,556,226 B / 命令行版 320,478,537 B（v17：206 / 205 MB，增量 +115 MB） |
| 启动耗时（同口径） | `vsum --help` 从进程创建到退出：v17 43.6s → v18 **46.8s**（+3.2s） |

> **更正一处旧数据**：上文「带 `_internal` 启动 4.73s / 单独 exe 4.49s」是
> 2026-10-09 用**另一口径**（界面窗口出现）测的，和这里的 43.6s / 46.8s
> （`subprocess` 计时，含进程退出时清理 `%TEMP%`）**不可直接比较**。
> 判断内置内核的代价，只看**同一口径下的差值**：+3.2s。

已知坑（已在代码里规避）：

1. `av`（faster-whisper 的依赖）在 Python 3.13 上装不上/接口不兼容 → 改为自己用 `wave` 解 PCM 喂模型；
2. HuggingFace 的 xet 存储协议在国内镜像上会 401 → 设置 `HF_HUB_DISABLE_XET=1`；
3. Windows 未开开发者模式时，HF 缓存的 symlink 会变成 0 字节文件 → 改用 `local_dir` 直下；
4. 系统 Python 常缺 tkinter → 界面改用「本地服务 + 桌面内嵌窗口」，不依赖 tkinter/Qt；
5. 打包后只剩 `generic` 一个平台：`pkgutil` 扫不到 PYZ 里的模块、且 `__import__("vsum.xxx")`
   无法被静态分析 → 内置插件改成字面量 `from .platforms import bilibili` + `collect_submodules("vsum")`；
6. 打包后 ASR 报 `silero_vad_v6.onnx 不存在` → `datas` 加 `collect_data_files("faster_whisper")`，
   并增加「VAD 失败则退回无 VAD 重试」的兜底；
7. 中文关键词出现几十字的长串：ASR 文本无标点时整段被当成一个词 →
   改为从字符流统计 2/3/4-gram，再用「首尾虚词字 + 左右邻字多样性」剔掉跨词碎片；
8. 风控页的 `<title>` 不是空的，`if not title` 拦不住 → 改用 `is_captcha_page()` 按关键词识别（见 4.1）；
9. `extract_info()` 吞掉 yt-dlp 的报错 → 加 `_LAST_ERROR` + `explain_failure()` 透传（见 4.2）；
10. `pipeline` 用一句通用 hint 覆盖了适配器的准确原因 → 保留 `FetchError` 原文并优先用专用适配器的（见 4.3）；
11. 忘了 `load_all()` 时 `detect()` 静默退化成通用兜底 → 按「关键平台是否齐全」判断并自愈（见 4.4）。
