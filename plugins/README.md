# plugins/ —— 外部插件目录

把任意 `.py` 文件丢进这个目录，程序启动时会自动 `import` 它。
只要文件里用了注册装饰器，新功能就会被接进流水线，**主流程代码一行都不用改**。

## 三类扩展点

| 想加什么 | 装饰器 | 继承的基类 | 关键接口 |
| --- | --- | --- | --- |
| 新平台 | `@register_platform` | `vsum.platforms.base.PlatformAdapter` | `resolve(url) -> VideoMeta` |
| 新识别引擎 | `@register_asr` | `vsum.asr.base.AsrEngine` | `transcribe(path, language, on_progress) -> Transcript` |
| 新摘要引擎 | `@register_llm` | `vsum.llm.base.LlmEngine` | `summarize(meta, transcript, on_token) -> str` |

`PlatformAdapter` 还有三个**可选**接口，实现了就能让该平台能力更完整：

| 方法 | 作用 | 不实现会怎样 |
| --- | --- | --- |
| `fetch_subtitle(meta)` | 返回官方/AI 字幕 | 自动走「下载音频 → 语音识别」 |
| `fetch_audio_source(meta)` | 返回音频直链 | 自动交给 yt-dlp |
| `supplement_text(meta)` | 返回弹幕/评论等补充语料 | 跳过 |

## 直接抄

见同目录的 `example_zhihu.py`，里面还有一个 ASR / LLM 引擎的模板骨架。

## 调试

```bat
vsum.exe --list          :: 列出已注册插件，确认你的插件被加载了
```

加载失败会在日志里留一条 WARNING（不会拖垮主程序），用 `python run_cli.py -v` 可以看到原因。
