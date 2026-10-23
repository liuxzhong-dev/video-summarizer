"""统一异常体系。

所有插件（平台适配器 / ASR / LLM）都抛出这里的异常，
流水线据此决定是降级、重试还是直接报错给用户。
"""


class VSumError(Exception):
    """所有本项目异常的基类。"""

    #: 是否已经是可读的用户提示（True 时不再追加堆栈）
    friendly = False

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


class UnsupportedURLError(VSumError):
    """无法识别 / 暂不支持的链接。"""

    friendly = True


class NetworkError(VSumError):
    """网络层失败（超时、DNS、被风控等）。"""

    friendly = True


class FetchError(VSumError):
    """拿到了页面/接口，但解析不出需要的数据（常因平台风控或需登录）。"""

    friendly = True


class LoginRequiredError(FetchError):
    """平台要求登录 / Cookie 才能访问。"""

    friendly = True


class MediaError(VSumError):
    """下载或提取音频失败（ffmpeg 缺失、无直链等）。"""

    friendly = True


class TranscribeError(VSumError):
    """语音转写失败。"""

    friendly = True


class LLMError(VSumError):
    """调用大模型失败（未配置、额度、超时等）。"""

    friendly = True


class DependencyMissing(VSumError):
    """可选依赖未安装（如 faster-whisper）。"""

    friendly = True


class RagError(VSumError):
    """RAG 链路失败（向量化接口不可用、向量库读写异常等）。

    单独一类是因为它的处置方式和其他环节不同：向量化失败**不该中断整条链路**，
    上层可以选择「只存文本、退化成关键词检索」继续跑完。
    """

    friendly = True
