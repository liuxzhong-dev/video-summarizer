"""本地 Web 服务：用标准库起一个 HTTP 服务，给前端提供 API。

为什么是 Web 而不是原生 GUI：

* 不依赖 tkinter / Qt，打包出来的 exe 小得多，也不会因为缺运行时而白屏；
* 界面用 HTML/CSS 写，改起来比布局代码快十倍；
* 同一套服务既能塞进桌面窗口（pywebview），也能直接用浏览器打开，
  远端/无显卡环境照样可用。

任务模型是「提交 → 轮询」：前端拿到 ``task_id`` 后定期问进度，
顺便把已经生成的摘要片段取走，看起来就是流式输出。
"""

from __future__ import annotations

import json
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

from ..core.config import Config, LLM_PRESETS
from ..core.context import Context
from ..core.errors import VSumError
from ..core.exporters import export, to_markdown, to_plain_text
from ..core.pipeline import Pipeline
from ..extensions import default_plugin_dir, load_all
from ..obsidian import status as vault_status

#: 单个上传文件的大小上限。拖进来的 PDF/PPT 通常几 MB，
#: 给到 60 MB 足够；再大就说明用户可能拖错了东西（比如整个安装包），
#: 而且正文最终只会喂给模型 24000 字，没必要把内存撑爆。
MAX_UPLOAD_BYTES = 60 * 1024 * 1024

#: 上传的临时文件保留多久。任务读它只需要几秒，留 48 小时是为了
#: 用户「总结完再点一次重新总结」时不至于文件已消失。
UPLOAD_TTL_SECONDS = 48 * 3600


def _format_hint() -> str:
    """拒收时告诉用户「到底能拖什么」。

    分类列出，而不是把四十多个后缀一股脑倒出来 —— 后者在界面上长到看不清。
    """
    return ("支持这些格式：\n"
            "文档   PDF / Word(.docx/.doc) / Excel(.xlsx/.xls) / PPT(.pptx) / "
            "EPUB / RTF / OpenDocument(.odt/.ods/.odp)\n"
            "网页   .html / .htm / .mhtml　字幕 .srt / .vtt / .ass\n"
            "文本   txt / md / csv / tsv / json / xml / yaml / ini / log / tex / sql 等")


def upload_dir(cfg: Config) -> str:
    """拖拽上传文件的落盘目录（与 media.cache_dir 同源，便于一起清理）。"""
    base = cfg.get("media.cache_dir") or os.path.join(
        tempfile.gettempdir(), "vsum_cache")
    path = os.path.join(base, "uploads")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    """把用户文件名洗成安全的 basename。

    必须做这一步：文件名来自浏览器，**可以包含** ``..\\..\\x`` 这类路径，
    直接拼进目录就是目录穿越。中文名要保留（要展示给用户），
    所以只剔掉路径分隔符与控制字符。
    """
    name = unquote(name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    if not name:
        name = "dropped"
    # 给中文名留足空间，同时别让文件系统超长
    root, ext = os.path.splitext(name)
    return (root[:80] + ext[:16]) or "dropped"


def _prune_uploads(folder: str) -> None:
    """清掉过期上传。**只删自己这个目录里的**，绝不外溢。

    上传文件是「用完即弃」的中间产物，不清理的话每拖一次就留一份，
    用户翻到临时目录时会莫名其妙。

    注意上传内容是「子目录 + 原始文件名」的结构，所以要连目录一起删，
    只删文件会把空壳目录留一堆。
    """
    import shutil

    now = time.time()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for fn in names:
        p = os.path.join(folder, fn)
        try:
            age = now - os.path.getmtime(p)
            if age <= UPLOAD_TTL_SECONDS:
                continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                os.remove(p)
        except OSError:
            continue




class Task:
    """一个总结任务的运行时状态。"""

    def __init__(self, task_id: str, raw: str):
        self.id = task_id
        self.raw = raw
        self.state = "running"          # running / done / error
        self.percent = 0
        self.message = "排队中…"
        self.text = ""                  # 流式累积的摘要正文
        self.error = ""
        self.hint = ""
        self.result = None
        self.created = time.time()
        self._q: "queue.Queue[str]" = queue.Queue()
        self.cancel = threading.Event()

    def drain(self) -> None:
        while True:
            try:
                self.text += self._q.get_nowait()
            except queue.Empty:
                return

    def to_dict(self) -> dict:
        self.drain()
        d: dict[str, Any] = {
            "id": self.id, "state": self.state, "percent": self.percent,
            "message": self.message, "text": self.text,
            "error": self.error, "hint": self.hint,
        }
        if self.result is not None:
            d["result"] = self.result.to_dict()
            d["markdown"] = to_markdown(self.result)
            d["plain"] = to_plain_text(self.result)
        return d


class TaskManager:
    """任务池。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()

    def submit(self, raw: str) -> Task:
        tid = uuid.uuid4().hex[:12]
        task = Task(tid, raw)
        with self.lock:
            self.tasks[tid] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

    def get(self, tid: str) -> Optional[Task]:
        with self.lock:
            return self.tasks.get(tid)

    # ---------------- 执行 ----------------
    def _run(self, task: Task) -> None:
        def progress(msg, pct):
            task.message = msg
            if pct and pct >= 0:
                task.percent = pct

        def token(chunk):
            task._q.put(chunk)

        ctx = Context(self.cfg, progress=progress)
        try:
            result = Pipeline(ctx).run(task.raw, on_token=token,
                                       should_cancel=task.cancel.is_set)
            task.result = result
            if not task.text:                       # 非流式时补上正文
                task.text = result.markdown
            task.state = "done"
            task.percent = 100
            task.message = "完成"
        except VSumError as exc:
            task.state = "error"
            task.error = str(exc)
            task.hint = getattr(exc, "hint", "") or ""
        except Exception as exc:
            import traceback
            task.state = "error"
            task.error = f"{type(exc).__name__}: {exc}"
            task.hint = traceback.format_exc(limit=3)
        finally:
            ctx.close()


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理。"""

    server_version = "VideoSummarizer/1.0"
    manager: TaskManager
    cfg: Config
    static_dir: str
    index_html: str

    # ---------------- 基础 ----------------
    def log_message(self, fmt, *args):     # 安静一点，别刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/api/config":
            self._json(self._config_view())
        elif path == "/api/platforms":
            self._json({"platforms": Pipeline.list_platforms()})
        elif path == "/api/presets":
            self._json({"presets": LLM_PRESETS})
        elif path == "/api/status":
            self._status()
        elif path == "/api/obsidian":
            self._json(vault_status(Context(self.cfg)))
        elif path == "/api/health":
            self._json({"ok": True, "version": "1.0.0"})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/upload":
            # 上传是原始字节流，不是 JSON —— 不能先走 self._body()
            self._do_upload()
            return
        body = self._body()
        if path == "/api/summarize":
            raw = (body.get("text") or "").strip()
            if not raw:
                self._json({"error": "请输入链接或分享文案"}, 400)
                return
            task = self.manager.submit(raw)
            self._json({"task_id": task.id})
        elif path == "/api/cancel":
            t = self.manager.get(body.get("task_id", ""))
            if t:
                t.cancel.set()
            self._json({"ok": True})
        elif path == "/api/obsidian":
            self._save_to_vault(body)
        elif path == "/api/config":
            self._save_config(body)
        elif path == "/api/export":
            self._do_export(body)
        else:
            self._json({"error": "unknown endpoint"}, 404)

    # ---------------- 具体实现 ----------------
    def _serve_index(self) -> None:
        try:
            with open(self.index_html, "rb") as f:
                data = f.read()
        except OSError:
            data = b"<h1>index.html missing</h1>"
        self._send(200, data, "text/html; charset=utf-8")

    def _do_upload(self) -> None:
        """接收前端拖进来 / 选择的文件，落盘后返回可读路径。

        为什么不让前端直接把文件读成文本发过来：
        浏览器读不了 PDF/Word 的二进制（要再引入 pdf.js 这类大库），
        而后端本来就有解析器。让后端存原文件、前端只传「路径」两个字，
        是最省事也最不容易出编码问题的做法。

        为什么不用 multipart：Python 3.13 已移除 ``cgi``，
        标准库不再带 multipart 解析器。传原始字节 + 文件名放请求头，
        零依赖且不会有中文文件名乱码。
        """
        from ..platforms.article import SUPPORTED_DOC_EXT, sniff_document

        raw_name = _safe_name(self.headers.get("X-Filename") or "")
        ext = os.path.splitext(raw_name)[1].lower()

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json({"error": "空文件"}, 400)
            return
        if length > MAX_UPLOAD_BYTES:
            self._json({
                "error": f"文件太大（{length / 1048576:.1f} MB，上限 "
                         f"{MAX_UPLOAD_BYTES // 1048576} MB）",
                "hint": "先把 PDF 拆成小份，或只把需要的章节复制成文本粘贴进来",
            }, 413)
            return

        # 边读边落盘，避免把 60 MB 整个塞在内存里。
        # 每次上传单独放一个子目录、里面保留**原始文件名** ——
        # 这样下游拿到的 basename 就是用户认识的名字（会显示在结果里），
        # 而不是 `20261009-143012-a1b2c3-报告.pdf` 这种糊在文件名上的时间戳。
        folder = upload_dir(self.cfg)
        subdir = os.path.join(folder, uuid.uuid4().hex[:12])
        try:
            os.makedirs(subdir, exist_ok=True)
        except OSError as exc:
            self._json({"error": f"创建上传目录失败：{exc}"}, 500)
            return
        target = os.path.join(subdir, raw_name)
        got = 0
        try:
            with open(target, "wb") as f:
                while got < length:
                    chunk = self.rfile.read(min(65536, length - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
        except OSError as exc:
            self._json({"error": f"保存文件失败：{exc}"}, 500)
            return
        if got != length:
            try:
                os.remove(target)
            except OSError:
                pass
            self._json({"error": "文件传输不完整，请重试"}, 400)
            return

        # 类型校验放在**落盘之后**：后缀不认识时还有一次机会 ——
        # WPS 存出来的 .wps/.et/.dps、浏览器丢了后缀的下载件，
        # 光看名字拒掉太武断，按内容再认一次才准。
        if ext and ext not in SUPPORTED_DOC_EXT and not sniff_document(target):
            try:
                os.remove(target)
            except OSError:
                pass
            self._json({
                "error": f"不支持的文件类型：{ext}",
                "hint": _format_hint(),
            }, 400)
            return

        _prune_uploads(folder)
        self._json({"ok": True, "path": os.path.abspath(target),
                    "name": raw_name, "size": got, "kind": ext.lstrip(".")})

    def _status(self) -> None:
        q = urlparse(self.path).query
        tid = ""
        for kv in q.split("&"):
            if kv.startswith("task_id="):
                tid = kv[8:]
        task = self.manager.get(tid)
        if not task:
            self._json({"error": "task not found"}, 404)
            return
        self._json(task.to_dict())

    def _config_view(self) -> dict:
        return {
            "config": self.cfg.as_dict(),
            # 配置文件在磁盘上的真实位置。必须在设置页显示出来 ——
            # 用户想手工改「关注方向」时，得先知道该打开哪个文件；
            # 也要让「首次运行自动生成的默认配置」这件事可见。
            "config_path": self.cfg.path or "",
            "config_created": bool(getattr(self.cfg, "created", False)),
            "asr_installed": self._asr_installed(),
            "llm_ready": self._llm_ready(),
            "plugin_dir": default_plugin_dir(),
            "obsidian": vault_status(Context(self.cfg)),
        }

    @staticmethod
    def _asr_installed() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def _llm_ready(self) -> tuple:
        from ..llm.openai_compat import OpenAICompatEngine
        ctx = Context(self.cfg)
        try:
            return OpenAICompatEngine(ctx).available()
        finally:
            ctx.close()

    def _save_config(self, body: dict) -> None:
        data = body.get("config") or {}
        for k, v in data.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    self.cfg.set(f"{k}.{k2}", v2)
            else:
                self.cfg.set(k, v)
        self.cfg.save()
        self._json({"ok": True, "path": self.cfg.path})

    def _save_to_vault(self, body: dict) -> None:
        """手动把某个任务的总结写入 Obsidian。"""
        task = self.manager.get(body.get("task_id", ""))
        if not task or task.result is None:
            self._json({"error": "任务未完成"}, 400)
            return
        from ..obsidian import save as vault_save

        try:
            info = vault_save(Context(self.cfg), task.result,
                              force=bool(body.get("force")))
        except Exception as exc:
            self._json({"error": f"入库失败：{exc}"}, 500)
            return
        self._json(info)

    def _do_export(self, body: dict) -> None:
        task = self.manager.get(body.get("task_id", ""))
        outdir = body.get("dir") or ""
        if not task or task.result is None:
            self._json({"error": "任务未完成"}, 400)
            return
        if not outdir:
            outdir = os.path.join(os.path.expanduser("~"), "VideoSummarizer")
        try:
            files = export(task.result, outdir, ("md", "txt", "json", "srt"))
            self._json({"ok": True, "files": files})
        except Exception as exc:
            self._json({"error": str(exc)}, 500)


def start_server(cfg: Optional[Config] = None, host: str = "127.0.0.1",
                 port: int = 0, index_html: str = "") -> tuple[ThreadingHTTPServer, int]:
    """启动服务，返回 ``(server, port)``。port=0 时自动挑一个空闲端口。"""
    load_all()
    cfg = cfg or Config.load()
    handler = type("BoundApiHandler", (ApiHandler,), {
        "manager": TaskManager(cfg),
        "cfg": cfg,
        "index_html": index_html or _default_index(),
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _default_index() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "assets", "index.html")
