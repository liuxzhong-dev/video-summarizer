# VideoSummarizer · 视频 / 文章链接一键总结

把**国内主流平台的视频链接**（或手机 App 复制出来的整段分享文案）丢进去，
自动拿到字幕 / 语音 / 元信息，再交给大模型，输出一份结构化的中文总结。

**同时支持非视频内容**：微信公众号、知乎、技术博客、小红书图文，
以及直接粘一段正文、或把**本地文件拖进窗口**（PDF / Word / Excel / PPT /
EPUB / RTF / OpenDocument / 字幕 / 网页存档 / 各类纯文本，共 40 多种后缀）。

交付物是一个**真正的 Windows 可执行文件**（`dist/VideoSummarizer/VideoSummarizer.exe`），
双击即用，不需要装 Python。

---

## 1. 三分钟上手

| 步骤 | 操作 |
| --- | --- |
| 1 | 双击 **`dist\VideoSummarizer.exe`**（首次会弹防火墙提示，允许即可） |
| 2 | 把抖音 / B站 / 快手 / 小红书 的**分享文案或链接**粘进输入框，**或把 PDF / Word / Excel / PPT / EPUB / 字幕 等文件直接拖进窗口**，点「开始总结」 |
| 3 | 想让总结更好？点「设置 → 大模型」填一个 API Key（DeepSeek / Kimi / 通义都行） |

> 不填 API Key 也能跑：程序会自动退回到**本地抽取式摘要**（离线、免费，质量低于大模型）。
> 视频没有字幕时，会自动下载音频做**本地语音识别**（首次需下载模型，之后离线可用）。

> 📦 `dist\VideoSummarizer.exe` 是**自包含的单文件**，不需要任何同级目录。
> 发给别人时**只拷这一个 exe** 就能双击运行（想带上自己的设置，再把 `config.json` 一起拷）。
> 首次运行会在 exe 旁边自动生成一份 `config.json`。

### 配置就是一个文件，随时能改

所有设置（API Key、Cookie、代理、Obsidian 库路径、**关注方向**…）都在 exe 同目录的
**`config.json`** 里，纯文本，**记事本直接改**，改完重启程序生效。
设置界面只是它的图形外壳 —— 两者改的是同一个文件，不存在「写死在程序里」的项。

程序找不到配置文件时会**自动生成一份**默认的，所以第一次运行就能拿到这个文件，
不用先点一遍保存。界面上「设置」弹窗顶部会显示这个文件的**完整路径**。

### 看不懂界面上的英文？三种办法

界面上所有英文词都有中文释义，按你顺手的程度挑一种：

| 想知道 | 怎么用 |
| --- | --- |
| 某个下拉选项是什么意思 | 下拉框里已经写成 `tiny — 极简` 这样，**展开就能看到**；下方常驻一行完整说明（含体积/耗时/准确度） |
| 某个字段（如 `Base URL`、`API Key`）是什么意思 | 鼠标停在字段右边的蓝色 `?` 上，会弹出气泡详解 |
| 想一次看全 | 点右上角**「术语表」**，Whisper / ASR / VAD / ffmpeg / Cookie / 风控 … 全都有中文解释 |

术语表和释义都写死在 `src/vsum/ui/assets/index.html` 的 `TERMS` 词典里，
想补充自己的说明直接改那个对象即可，不用动其他代码。

命令行用户可以用同目录的 `vsum.exe`：

```bat
vsum.exe "https://www.bilibili.com/video/BV1GJ411x7h7"
vsum.exe "粘贴整段抖音分享文案" --asr subtitle --out ./out
vsum.exe --list          :: 查看已注册插件与平台支持情况
vsum.exe --check         :: 自检：各项能力是否就位（含实测启动一次浏览器）
```

一次**真实运行**的完整输出（11 分钟 B站视频 → 本地 small 模型转写 3020 字 → 时间线 + 关键词，
耗时 154 秒，未接大模型）见 [`docs/example-output.md`](docs/example-output.md)。

---

## 2. 平台支持情况（实测）

⚠️ 说明：各家平台风控策略经常变，下表是**本项目代码实际做到的程度**，不是宣传口径。

### 视频平台

| 平台 | 等级 | 能拿到什么 | 限制与前提 |
| --- | --- | --- | --- |
| **哔哩哔哩** | ★★★ 完整 | 标题/UP主/简介/时长 + 官方或 AI 字幕 + 弹幕 + 音频 | **字幕接口需要登录态**：不填 Cookie 时通常拿不到字幕，会自动改用「下载音频 → 语音识别」，这条路径已实测跑通 |
| **抖音** | ★★ 部分 | **完整音频 → 转写 → 摘要**（实测 70 秒 / 8000 字） | HTTP 路线会被风控拦截（判据含浏览器 JS 生成的 `s_v_web_id`，Cookie 改不动）。程序**自动改用真实浏览器抓流**，见下 |
| **快手** | ★★ 部分 | 同抖音，走浏览器兜底 | 同上 |
| **小红书** | ★★ 部分 | 同抖音，走浏览器兜底 | 同上；图文笔记另需登录才能拿正文 |
| **微信视频号** | ★ 元信息 | 分享文案里的标题 | 官方无公开解析接口（分享链接只能在微信内打开） |
| **通用（yt-dlp）** | ★★ 部分 | 上千个站点：YouTube / 微博 / 西瓜 / 知乎 / 腾讯视频 / 优酷 / Vimeo / TikTok … | 有字幕时直接用字幕；海外站点需配置代理 |

### 非视频内容（本版新增）

| 输入方式 | 等级 | 说明 |
| --- | --- | --- |
| **拖拽文件**（PDF / Word / Excel / PPT / EPUB / RTF / 字幕 / 文本…） | ★★★ | 直接把文件拖到窗口任意位置，或点「选择文件」 |
| **网页文章**（公众号 / 知乎 / 博客园 / 技术博客…） | ★★★ | 自研正文抽取（零新依赖的简化版 Readability），实测抽出 6927 字正文 |
| **纯文本直贴** | ★★★ | 不少于 60 字即按文章处理，实测 1.8 秒出摘要 |
| **本地文档路径** | ★★★ | 在输入框里填 `D:\notes\某篇.pdf` 同样有效 |

支持的文件格式与各自的边界：

| 格式 | 读取方式 | 能拿到什么 | 限制 |
| --- | --- | --- | --- |
| `.pdf` | pypdf | 文字层全文（多页拼接） | **扫描件 / 图片版 PDF 读不了**（没有文字层），会明确提示需要 OCR，不会硬编一份总结 |
| `.docx` | python-docx | 段落 + 表格（按文档流顺序，表格不会被挤到末尾） | 加密文档不支持 |
| `.doc` | — | — | Word 97-2003 旧格式读不了，提示「另存为 .docx」 |
| `.xlsx` | openpyxl | 每个工作表逐行读出（单元格用 `\|` 分隔），**取公式的计算结果** | 图表里的文字读不到 |
| `.xls` | xlrd | 同上（旧版二进制格式，xlrd 2.x 专治它） | — |
| `.pptx` | python-pptx | 每页文字 + 表格 + **演讲备注**，并按页标注 | 纯图片幻灯片读不到文字；`.ppt` 需先另存为 `.pptx` |
| `.epub` | 标准库（zipfile） | 按书籍阅读顺序（spine）拼接各章节 | 纯图片漫画读不到文字 |
| `.odt .ods .odp` | 标准库（zip + XML） | OpenDocument 三件套，段落 / 表格 / 分页 | 内容全在图片里时读不到 |
| `.rtf` | 标准库（自研剥离器） | 正文，中文按 `\uNNNN` 转义还原；字体表 / 图片等控制信息自动丢掉 | — |
| `.srt .vtt .ass .ssa .sbv` | 标准库（清洗） | 字幕正文，**时间轴与序号自动洗掉**，相邻重复行合并 | — |
| `.html .htm .mhtml` | 自研抽取 | 正文（抽不到时退回整页清洗）；`.mhtml` 是浏览器「另存为单个文件」的存档 | — |
| `.txt .md .csv .tsv .json .xml .yaml .ini .toml .log .tex .sql .rst` | 直接读 | 全文（自动识别 UTF-8 / GBK / Big5 编码） | — |
| `.wps .et .dps` | WPS 三大件 | 新版 WPS 存出来的内核就是 docx / xlsx / pptx，**按内容识别后正常读** | 老版二进制内核读不了，提示「另存为」 |

> **后缀只是线索，内容才是准绳。** 程序会读文件头（magic bytes）判断真实格式，
> 所以「把 .docx 改名成 .wps」「浏览器下载丢了后缀」「后缀被改错」都能正确读出来；
> 老版 OLE2 二进制（`.doc` / `.ppt` / 老 WPS）没有可靠的纯 Python 提取路径，
> 会明确提示另存为新格式 —— 而不是甩一句「解析失败」。
> `.xls` 是个例外：它虽然也是 OLE2，但 `xlrd` 能读，所以按内部流名（`Workbook`）
> 认出来之后照读不误。

> **为什么拖文件是「上传」而不是「传路径」**：浏览器出于安全**不会**把被拖进来文件的
> 真实路径告诉网页（`file.path` 是 Electron 之类的私有扩展，标准浏览器没有）。
> 所以程序把文件字节传到本地服务、由服务落盘，再把落盘路径交给解析器。
> 好处是桌面窗口和浏览器打开时行为完全一致。上传的临时文件放在
> `%TEMP%\vsum_cache\uploads\`，48 小时后自动清理。

**已知限制**：知乎 / 掘金 / CSDN 大量使用 JS 渲染，正文不在 HTML 里，抽不到。
遇到抽不到时会明确说明原因，不会静默输出空摘要。

### 强风控平台怎么解决的（抖音/快手/小红书）

这些平台的风控凭证由**浏览器 JS 动态生成**（实测：服务器既不在响应里下发，
页面 HTML 里也没有那段配置），所以 `requests` 无论怎么伪造 Headers 都过不去 ——
实测把 Cookie 里的字段删掉、只留三个关键字段，yt-dlp 三种改法**全都失败**。

程序的解法是**启动真实的浏览器**（Playwright 驱动），
让页面正常执行 JS、正常请求视频，然后**只取 URL**，
用 `requests` + `Referer` 下载 —— 与你手动打开视频播放是一回事，只是自动化了。

**代价明说**：这一条约 **70 秒**（浏览器启动 + 等播放器拉流），
运行时可能弹出浏览器窗口（无头模式抓不到时才会切有头）。
不想要可以在「设置 → 语音识别」里关掉浏览器兜底。

#### 浏览器从哪来（v18 起内核也打进 exe 了）

按这个顺序找，**哪个能用就用哪个**：

| 顺序 | 通道 | 说明 |
| --- | --- | --- |
| 1 | 系统 Edge | Windows 自带，启动最快，实测抓流最全 |
| 2 | 系统 Chrome | 装了 Chrome 的机器 |
| 3 | **exe 内置精简内核** | v18 新增。没有 Edge/Chrome 时靠它兜住 |
| 4 | 本机 `ms-playwright` 内核 | 源码运行主要走这条 |

> **v18 之前这里有个真缺口**：exe 里打包的只是 Playwright **驱动**
> （node + 协议实现，约 100 MB），浏览器本体在
> `%LOCALAPPDATA%\ms-playwright`（271 MB），**从来没进过包**。
> 于是把 exe 发给没装 Edge/Chrome 的人，抖音解析直接失败、看不出原因。
> v18 把精简版 `chrome-headless-shell` 一并打进 exe：
> 剔掉 220 种语言包、许可证、西文断字词典和 SwiftShader Vulkan 图形栈，
> 271 MB → 219 MB，压缩后给 exe 增加约 100 MB。
> 实测该精简版能正常启动、且照样拿到 `s_v_web_id`（即照样过抖音风控）。

**代价也明说**：exe 从 206 MB 涨到约 **321 MB**。单文件每次启动本来就要把整个
归档解压到 `%TEMP%`，多带一个内核后**同一口径下慢约 3 秒**
（实测 `vsum --help`：v17 43.6s → v18 46.8s；其中内核解压完成约 4.4s）。
换来的是"发给谁都能用"。
内置的是无头内核，没有窗口 —— 所以「切有头重试」只在有系统浏览器时才有。

> **这条兜底现在是"必需依赖"**：打包时缺 playwright **或缺内置内核**
> 都会直接中止构建，不允许再出现"某版 exe 静默少了兜底"的情况
> （踩过这个坑，见下方"排障"）。
> 想确认手上这份 exe 带没带，跑 `vsum.exe --check` 一看便知。

**合规边界**：不绕验证码、不解密 DRM、不批量抓取付费内容。

判定规则：链接匹配不到专属适配器时，**自动落到通用 yt-dlp 适配器**，
所以「不支持的平台」往往也能出结果。

---

## 3. 工作流程

```
分享文案 ──▶ 抠出链接 ──▶ 匹配平台插件 ──▶ 解析元数据
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              官方/AI 字幕          下载音频（ffmpeg）
                    │                   │
                    │           本地 Whisper / 云端 ASR
                    └─────────┬─────────┘
                              ▼
                   大模型总结（失败则本地抽取式兜底）
                              ▼
                    Markdown / 纯文本 / SRT / JSON
```

每一步都可以单独失败并降级，**最终一定产出结果**（除非链接本身无效）。界面上会明确标出
这次总结用的是「字幕 / 语音识别 / 仅元信息」，不会拿元信息冒充全文。

---

## 4. 配置

配置文件是 JSON：优先读写**程序同目录的 `config.json`**（便携模式），否则 `%APPDATA%\VideoSummarizer\config.json`。

### 4.1 大模型（决定总结质量）

| 预设 | Base URL | 模型 |
| --- | --- | --- |
| `deepseek`（默认） | `https://api.deepseek.com/v1` | `deepseek-chat` |
| `moonshot` | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| `siliconflow` | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| `ollama` | `http://localhost:11434/v1` | `qwen2.5:7b` |

任何 OpenAI 兼容接口（含自建 one-api / new-api）都能直接填。

### 4.2 语音识别

| 方式 | 说明 |
| --- | --- |
| 本地 Whisper（默认） | 离线可用。首次下载模型：`tiny` 75MB / `small` 460MB（推荐）/ `medium` 1.5GB。默认走 `hf-mirror.com` 镜像 |
| 云端 ASR | 填 Base URL + Key + 模型。推荐硅基流动 `FunAudioLLM/SenseVoiceSmall`（有免费额度）或 Groq `whisper-large-v3-turbo`。**长音频（>10 分钟）自动切成 5 分钟一段逐段识别**，不再单次上传大文件（v19 实测修复：35 分钟视频单请求上传易超时，超时后整次识别作废、只剩标题简介总结） |
| 仅字幕 | 最快。拿不到字幕就直接基于标题简介总结 |

识别失败时音频文件会保留在缓存目录（界面的总结备注里会给出完整路径），
**直接重试即可，不用重新下载视频**。

### 4.3 平台 Cookie（遇风控时填）

F12 → Network → 随便点一个请求 → Request Headers → 复制 `cookie` 的值，粘到「设置 → 平台 Cookie」。
B站 Cookie（含 `SESSDATA`）能直接解锁**字幕接口**，收益最明显。

### 4.4 Obsidian 知识库（AI 筛选后入库）

把总结结果沉淀成**结构化笔记**写进你的 Obsidian 库。核心不是"自动保存"，而是**AI 先判断这段内容值不值得进库**——知识库最大的成本不是空间，是三个月后没人愿意翻。

**前提**：本功能写 `.md` 文件，**不需要装任何 Obsidian 插件**，Obsidian 也不用开着。

#### 怎么用

1. 设置 → Obsidian 知识库 → 「库路径」填你的库根目录（含 `.obsidian` 那层）。**留空会自动探测**，一般不用填。
2. **重点：填「关注方向」**。这是筛选质量的唯一关键 —— 不填，AI 只能用泛泛的标准打分。
   - 例：`LLM推理优化, 向量检索, 求职面试` —— 逗号分隔
3. 打开「自动入库」，或总结完手动点「存入 Obsidian 知识库」。

#### AI 怎么判（五个维度）

| 维度 | 权重 | 问什么 |
| --- | --- | --- |
| 信息增量 | 35% | 是不是你已经知道的东西？换个说法讲一遍 = 0 分 |
| 专实体密度 | 20% | 有多少能当检索锚点的专有名词/术语？没有锚点，RAG 召不回来 |
| 可执行性 | 20% | 看完能做什么？纯观点、纯背景介绍 → 低分 |
| 上下文自足 | 15% | 半年后只看这段能看懂吗？依赖"上期说过"→ 低分 |
| 时效风险 | 10% | 会不会很快过期？价格、版本号这类 → 降分 |

**关键区别**：这套标准是**面向检索**的，不是"面向好看"的。一段视频讲得再好但没有专实体，检索系统永远召不回它，对知识库就是零贡献。

#### 分级入库

| 档位 | 分数 | 去哪 |
| --- | --- | --- |
| 精选 | ≥ 80（可调） | `20-精选/` —— 可复用的知识 |
| 待阅 | 40–80（可调） | `30-待定/` —— 先进来，以后再看 |
| 丢弃 | < 40 | **不写文件** |

低分不是失败，是**在保护你的知识库**。确实想存就点「强制存入精选」。

#### 写出来长什么样

```markdown
---
type: video-note
category: 视频笔记
title: LangGraph 多智能体编排实战
platform: B站
created: 2026-10-05
value: 86
tier: 精选
tags: [视频总结, LangGraph, AI Agent, B站, 精选]
entities: [LangGraph, StateGraph, 提示词使用指南]
---

（开头是 3–5 句自足摘要，然后是核心结论 / 关键数据 / 可执行操作 / 关键概念）
```

- 文件名带日期前缀（`2026-10-05 标题.md`），和你库里现有命名一致
- `entities` / `topics` 只在**库里确实有对应笔记**时才加 `[[双链]]`，不会指向空页
- 精选笔记会顺带维护根目录的`术语表`和 `00-MOC/视频笔记索引.md`

#### 命令行

```bash
vsum.exe --obsidian "https://..."          # 总结完顺手入库
vsum.exe --obsidian --focus "LLM,检索"     # 临时指定关注方向
vsum.exe --vault-status                    # 只看库在哪、有多少篇、筛选能不能用
vsum.exe --force-save                      # 忽略评分强制入库
```

#### 没配大模型会怎样

不会失败，改用**保守的本地打分**（基于摘要里有没有专实体、有没有数字和步骤）。分数普遍偏低，可能什么都不写 —— 这是刻意的，宁可漏不可乱。

---

### 4.5 语义检索问答（RAG）

前面那些能力做的是「把一条链接变成一份总结」——每条视频各跑各的，跑完就散在硬盘上。
这一节做的是**把所有看过的内容攒成一个能提问的库**：语料切块、向量化后存起来，
提问时先检索出最相关的几段，再让大模型基于这几段作答，并标明出处。

区别不在「多一个功能」，在于**从一次性工具变成可累积的知识库**。

#### 怎么用

```bash
# 建库：三类语料都能灌
vsum.exe --rag-ingest "D:\KnowledgeBase"          # 一个目录的 Markdown 笔记
vsum.exe --rag-ingest "D:\某文件.pdf"              # 单个文件（PDF/Word/Excel/PPT 均可）
vsum.exe --rag-ingest "https://www.bilibili.com/video/BV1xx411c7mD"   # 视频链接

# 提问与查看
vsum.exe --rag-ask "打包之后插件为什么会丢"
vsum.exe --rag-ask "..." --rag-topk 8             # 临时多取几段
vsum.exe --rag-list                               # 库里有哪些来源、共多少片段
vsum.exe --rag-clear                              # 清空重建
```

#### 语料从哪来

「字幕库」不是程序自带的，是**一条条攒出来的**。两条路：

| 语料 | 做法 | 适合 |
| --- | --- | --- |
| Markdown 笔记 | `--rag-ingest <目录>` | 已有 Obsidian 库或文档目录，一次几十上百篇 |
| 视频字幕 | `--rag-ingest <链接>` | 把看过的视频攒成库，B站/抖音/YouTube 等都能进 |

视频这条路会走完整的抓取链路（有字幕用字幕，没有就下载音频做识别），因此慢一些。

#### 一次问答里发生了什么

```
问题 → 向量化 → 在库里找最相关的几段 → 拼进提示词 → 大模型作答（带编号引用）
```

答案里的 `[1] [2]` 对应下方的「引用片段」，能看出它来自哪篇、哪一段；视频还会带上
时间点。这是**防幻觉的关键**：模型只能依据给定的片段回答，片段里找不到就必须说
「资料库中没有相关内容」，不允许用常识补全。检索到的片段如果互相矛盾，也要求它指出来。

#### 配置

```jsonc
"rag": {
  "enabled": true,
  "embedder": "siliconflow",       // 向量化引擎（插件 key）
  "store": "local-json",           // 向量库实现（插件 key）
  "base_url": "",                  // 留空 = 复用 asr.cloud_base_url
  "api_key": "",                   // 留空 = 复用 asr.cloud_api_key
  "embed_model": "BAAI/bge-m3",    // 1024 维；换模型必须重建库
  "store_path": "",                // 留空 = config.json 同目录的 rag_store
  "top_k": 5,
  "min_score": 0.0,
  "subtitle_chars": 600,           // 字幕切块上限（按说话节奏切）
  "doc_chars": 800,                // 文档切块上限（段落是完整语义单元，给大些）
  "auto_ingest": false             // 总结完自动入库
}
```

**两个 Key 默认复用**：向量化走硅基流动，和云端语音识别是同一家，所以
`base_url` / `api_key` 留空时会自动取 `asr.cloud_*`，不必把同一个 Key 抄两遍。
想让 embedding 走别家（OpenAI、本地模型）再填上即可。

`min_score` 别凭感觉设 0.7 / 0.8 —— bge-m3 在中文上的绝对值偏低（实测相关句约
0.52、无关句约 0.40），阈值要用自己的语料校准；默认 0.0 表示不过滤。

#### 两处降级：没配 Key 也不是不能用

| 情况 | 行为 |
| --- | --- |
| 向量化失败 / 未配 Key | 语料照常入库，只是**只存文本**；检索退化为关键词匹配，能用但精度下降。补好 Key 后重跑一次同样的入库命令即可补上向量 |
| 大模型不可用 | 不做生成，直接把最相关的原文片段列出来。宁可少给答案，不给假答案 |

两种情况都会在结果下方写明原因 —— 不然「搜不准」很容易被误判成语料质量差，
而真正的原因往往只是 Key 没配或库与模型维度对不上。

#### 换成别的向量库

`embedder` 与 `store` 都是插件（`@register_embedder` / `@register_store`）。
默认实现刻意选了零依赖的本地库：几千条语料下暴力检索只要毫秒级，不值得为它
拖进 onnxruntime 之类的重依赖。等语料涨到几万条以上，再按同一套接口换
FAISS / Chroma 即可，上层检索与问答代码不用动。

---

## 5. 目录结构

```
video-summarizer/
├── README.md                  ← 本文件
├── requirements.txt
├── build.bat                  ← 一键打包
├── VideoSummarizer.spec       ← PyInstaller 配置
├── run_ui.py                  ← 界面入口（源码运行）
├── run_cli.py                 ← 命令行入口（源码运行）
├── config.json                ← 运行后自动生成
├── src/vsum/
│   ├── core/                  配置/上下文/流水线/数据模型/HTTP/ffmpeg/导出
│   ├── platforms/             平台适配器（bilibili / douyin / kuaishou / article …）
│   ├── asr/                   语音转写（faster-whisper 本地 / 云端）
│   ├── llm/                   摘要引擎（OpenAI 兼容 / 本地抽取式）
│   ├── media/                 yt-dlp 封装 + 浏览器兜底（browser_fetch）
│   ├── obsidian/              知识库入库（定位 vault / AI 筛选 / 笔记重写 / 术语表）
│   ├── rag/                   语义检索（切块 / 向量化 / 向量库 / 检索 / 问答）
│   ├── ui/                    本地服务 + 桌面窗口 + 前端页面
│   └── extensions.py          插件装载
├── plugins/                   ← 用户自建插件目录（.py 直接丢进来）
├── rag_store/                 ← 向量库（建库后生成；index.json + vectors.f32）
├── tools/prepare_browser_vendor.py  ← 生成内置浏览器内核（精简 271→219 MB）
├── vendor/pw-browsers/        ← 上一步的产物，打包时打进 exe（不入库）
├── tests/                     冒烟测试（B站 / ASR / Web API / Obsidian /
│                              风控守卫 / 文档格式 / RAG 检索）
└── dist_v18/                  ← 打包产物（v16/v17 及更早见 dist_v16/dist_v17）
    ├── VideoSummarizer.exe    ← 界面版（单文件，约 310 MB，含内置内核）
    ├── vsum.exe               ← 命令行版（单文件）
    └── config.json            ← 设置与关注方向，可直接编辑
```

详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 6. 从源码运行

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python run_ui.py            :: 界面
.venv\Scripts\python run_cli.py "链接"     :: 命令行
.venv\Scripts\python tests/smoke_bilibili.py
.venv\Scripts\python tests/smoke_asr.py
.venv\Scripts\python tests/smoke_webapi.py
.venv\Scripts\python tests/smoke_obsidian.py
.venv\Scripts\python tests/smoke_platform_guard.py   :: 风控页识别 / 错误透传
.venv\Scripts\python tests/smoke_doc_formats.py      :: 拖拽文档解析（全离线）
```

> `smoke_doc_formats.py` 会**当场生成**真 PDF / docx / pptx / epub 来测，
> 不依赖任何外部素材，也不联网。

> **必须照 `requirements.txt` 装，别挑着装。**
> 尤其 `pywebview` —— 缺了它界面版 exe 会退化成浏览器，而且服务会跟着进程退出
> （浏览器刷新就是 `ERR_CONNECTION_REFUSED`）。`VideoSummarizer.spec` 里已经加了检查，
> 缺依赖会直接中止打包而不是产出一个坏 exe。

打包：双击 `build.bat`。它会先清掉上一轮的 `build/` 和 `dist/`，再**分两次**调用
PyInstaller（界面版 / 命令行版各一次，各自独立 workpath）——别合并成一次，
也不要加 `--clean`，原因见 `VideoSummarizer.spec` 顶部注释。手动打包等价于：

```bat
set VSUM_TARGET=gui && pyinstaller VideoSummarizer.spec --noconfirm --workpath build\gui --distpath dist
set VSUM_TARGET=cli && pyinstaller VideoSummarizer.spec --noconfirm --workpath build\cli --distpath dist
```

产物是**两个自包含的单文件 exe**，不会再生成 `_internal\` 目录。

> **历史坑（已修）**：v15 及更早的 spec 里 `EXE()` 传了 binaries/datas，**同时又写了
> `COLLECT()`**，于是同一套依赖被生成了两份 —— exe 里一份、`_internal\` 里一份
> （后者 418-500 MB）。实测带 `_internal` 启动 4.73s / 4.64s，把 exe 单独拷到空目录启动
> 4.49s / 4.52s，**完全一样**：exe 每次启动都会往 `%TEMP%` 解压，`_internal\` 从头到尾
> 没被读过。所以那份目录是纯浪费，v16 起直接删掉 `COLLECT`，只出单文件。
> 相应地，README 旧版那句「根目录的 exe 缺 `_internal` 会报错」是**错的** ——
> 单文件本来就不依赖它。

---

## 7. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 双击 exe 没反应 | 程序会依次尝试 Edge WebView2 窗口 → WinForms 窗口 → 系统浏览器。若三者都被拦截，手动访问控制台打印的地址；或改用 `run_ui.py --browser` |
| 浏览器打开但**刷新就显示连接被拒** | 服务已退出。关掉所有窗口后重开；仍复现说明窗口后端都不可用，用 `VideoSummarizer.exe --browser` 启动（浏览器模式会保持进程存活） |
| 浏览器里看到 `127.0.0.1` 但没有端口号 | 地址不完整，正常情况下一定带端口（如 `127.0.0.1:54195`）。出现这种说明程序异常退出，请从命令行启动看报错 |
| B 站提示「没有字幕」 | 正常。字幕接口需要登录态，程序会自动下载音频做语音识别；想要字幕就去设置里填 B站 Cookie |
| 语音识别很慢 | 首次要下模型；CPU 上 `small` 模型约 1-2 倍速（10 分钟视频约 3-6 分钟）。着急就换 `tiny`，或改用云端 ASR |
| 抖音/快手只出了标题 | 被风控了。填 Cookie 后重试，或把视频文字内容粘进去用纯文本总结 |
| 抖音提示「要求验证后才能访问」 | **不是程序坏了，是抖音的风控**。未登录 IP 访问 `douyin.com/video/xxx` 会被拦到验证码中间页。程序现在能识别这种页面并明确告诉你原因（以前会拿「验证码中间页」当视频标题，界面看着像"解析不出来"）。解法：设置 → 平台 Cookie → 粘贴 douyin.com 的 Cookie |
| 不确定这份 exe 到底带了哪些能力 | 跑 `vsum.exe --check`。它会逐项列出解析 / 界面 / 浏览器兜底 / 文档 / 音频 / 大模型的状态，并且**真的启动一次 Edge** 验证兜底可用（只 import 成功不算数）。关键项缺失时退出码为 1 |
| 提示「此版本未内置浏览器兜底（Playwright）」 | **这是打包失误，不是你操作的问题**。老版本 exe 曾静默漏打 playwright（v17 起已在构建阶段强制校验，不会再发生）。应急办法：填该平台 Cookie 走 HTTP 路线，或把视频标题/简介粘进输入框走纯文本总结。注意：在 exe 环境下自己 `pip install playwright` **没有用**，冻结程序不会去读系统 Python |
| 报错「无法解析该链接：**Unable to handle request: N unexpected error(s)**」 | **请求根本没发出去，与链接、Cookie 无关**；yt-dlp 的几个网络处理器都异常退出了，几乎都是**这台机器的网络环境**问题。按顺序查：① 这台机器能否正常打开该网站；② 是否开了代理 / VPN / 加速器 —— **填错的代理会让所有请求都失败**，去「设置 → 媒体 → HTTP 代理」清空后重试；③ 防火墙 / 安全软件是否拦了本程序；④ 系统时间与根证书是否正常。v16 起这条报错会自动附上本次的 **DNS/TLS 自检结果**，照着看即可 |
| 换了台电脑、改了个设置就报上面这条 | 关注方向、API Key 这类设置**改动不会影响网络请求**，两者只是时间上挨着。换机器后真正变的是网络环境（代理、DNS、安全软件）。把 exe 单独拷到新机也能跑（自包含单文件），但 `config.json` 里的**代理**字段是**跟机器绑定**的：从旧机器带过来的话，先确认那台机器上代理确实可用 |
| 拖文件进去没反应 | 个别老版本 WebView2 运行时会拦掉拖放。点旁边的**「选择文件」**按钮，效果完全一样 |
| 拖 PDF 进去说「没有可提取的文字」 | 这是**扫描件/图片版 PDF**（整页都是图，没有文字层），需要 OCR。可以先用微信/QQ 的「提取文字」转成文本，再粘进输入框用纯文本总结 |
| 拖 PDF/Word/PPT 提示要装库 | 你在用源码运行且没装可选依赖。执行 `.venv\Scripts\pip install pypdf python-docx python-pptx openpyxl xlrd`；**发布版 exe 已内置，不会遇到这个问题** |
| 拖 .doc / .ppt / 老版 .wps 被拒 | 这是 Office 97-2003 那代二进制格式。用 Word/WPS 打开后「另存为」`.docx` / `.pptx` / `.xlsx` 再拖 |
| 拖 `.wps` / `.et` / `.dps`（WPS 三大件） | 新版 WPS 存出来的内核就是 docx / xlsx / pptx，**直接拖就能读**；只有老版二进制内核才需要另存为 |
| 后缀写错了 / 下载的文件没有后缀 | 不用改。程序会读文件头判断真实格式，后缀只是线索 |
| Excel 里的公式变成数字了 | 这是有意的：取公式的**计算结果**（`=SUM(A1:A9)` → 实际数值），否则总结里全是表达式 |
| 字幕文件拖进去满屏时间轴 | 已自动洗掉。`.srt` / `.vtt` / `.ass` 的序号、时间轴、样式标签都会被剔除，只留台词 |
| 拖进去的表格内容顺序乱了 | `.docx` 已按文档流顺序读（表格在它原本的位置）；`.pptx` 只能按「一页内的形状顺序」，复杂排版可能与视觉顺序有出入 |
| 文档总结里为什么没有时间戳 | **故意的**。时间戳只对视频有意义；文档的段落时间是按字数估的，标出来全是 `[00:00]`，属于误导。文档的「分段细讲」按内容顺序列点，不带时间戳 |
| 报错里出现 `Fresh cookies are needed` | yt-dlp 的原话，意思同上：这个平台必须带 Cookie。这是**平台硬性要求**，不是本项目的 bug |
| 短链报 `Unsupported URL` | 短链没展开。把浏览器地址栏里的**完整网址**（含 `/video/数字`）复制过来，或先在浏览器打开一次短链再复制地址 |
| 提示未找到 ffmpeg | 程序内置了一份；若被杀软删了，去设置里手工指定 `ffmpeg.exe` 路径 |
| 模型下载失败 | 换网络/代理重试，或把设置里的镜像改成 `https://huggingface.co`，或手动下载模型后填「模型目录」 |
| YouTube 等打不开 | 设置 → 媒体 → HTTP 代理，填 `http://127.0.0.1:7890` 之类的地址 |
| 入库什么都没写 | 没配 API Key 时用保守的本地打分，分数普遍偏低。填「关注方向」+ 配大模型，或点「强制存入精选」 |
| 明明配了库路径，却说找不到 | 路径必须是**库根目录**（含 `.obsidian` 那层）。填错时程序**不会**去猜别的库——宁可报错也不写错地方 |

---

## 8. 二次开发

新增一个平台 / 识别引擎 / 摘要引擎，**不用改主流程**，只需三步：

1. 在 `src/vsum/platforms/`（或 `asr/` `llm/`）下新建模块，继承对应基类；
2. 用 `@register_platform` / `@register_asr` / `@register_llm` 装饰；
3. 放进 `plugins/` 目录即可被自动加载（内置模块在 `extensions.py` 里自动扫描）。

示例见 [`plugins/example_zhihu.py`](plugins/example_zhihu.py)，完整说明见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 9. 免责声明

本项目仅用于**个人学习与技术研究**。请遵守各平台的用户协议与 robots 规则，
不要用于批量抓取、商业转售或侵犯他人版权/隐私的场景。下载的内容请在合理期限内删除。
