# -*- coding: utf-8 -*-
"""冒烟测试：RAG 检索链路。**全部离线，不发任何网络请求。**

覆盖四类容易在改动中悄悄退化、且症状不明显的行为：

1. 切块 —— LLM 救不回来切碎的语义，块边界错了很难从「答得不好」倒推回来；
2. 向量库读写 —— 落盘后重新加载必须与内存一致，维度不符必须**报错而不是凑合**；
3. 检索 —— 排序方向、关键词兜底、以及「向量挂了还能不能搜到东西」；
4. 提示词 —— 防幻觉的四条约束是硬要求，被改掉就等于系统开始编答案。

假向量引擎的做法：不联网，用字符散列造确定性向量。它没有语义，
所以**语义排序的用例改用受控向量直接验证**，不用它假装语义正确。
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")))

from vsum.core.config import Config                                  # noqa: E402
from vsum.core.context import Context                                # noqa: E402
from vsum.core.errors import RagError                                # noqa: E402
from vsum.core.models import Segment, Transcript, VideoMeta           # noqa: E402
from vsum.rag.ask import build_prompt                                # noqa: E402
from vsum.rag.base import Chunk, Hit                                 # noqa: E402
from vsum.rag.chunker import (chunk_document, chunk_transcript,      # noqa: E402
                              strip_frontmatter)
from vsum.rag.ingest import ingest_chunks                            # noqa: E402
from vsum.rag.retriever import (Retriever, keyword_score,            # noqa: E402
                                terms_of)
from vsum.rag.store import LocalVectorStore                          # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


TMP = tempfile.mkdtemp(prefix="vsum_rag_test_")


def new_ctx() -> Context:
    return Context(Config({"rag": {"store_path": os.path.join(TMP, "store")}}))


class FakeEmbedder:
    """确定性假向量：同样文本一定得到同样向量。**没有语义**，只用于流程验证。"""

    key, name, note = "fake", "假向量引擎", "仅测试用"
    dim = 16

    def __init__(self, ctx=None, dim=None):
        self.ctx = ctx
        self.calls = 0
        # 维度必须能与被测的库对齐，否则只会测出「维度不符」那条分支
        if dim:
            self.dim = dim

    def available(self):
        return True, ""

    def embed(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for i, ch in enumerate(t):
                v[ord(ch) % self.dim] += 1.0
                v[(i * 7 + ord(ch)) % self.dim] += 0.3
            out.append(v)
        return out


class BrokenEmbedder(FakeEmbedder):
    """模拟向量服务挂掉 —— 用来验证「入库不该被它阻断」。"""

    def embed(self, texts):
        raise RagError("模拟：向量服务不可用")


# ------------------------------------------------------------ 1. frontmatter
print("\n=== 1. frontmatter 剥离 ===")
fm_text = ("---\n"
           "type: rule\n"
           "tags: [RAG, 切块]\n"
           "created: 2026-10-16\n"
           "---\n"
           "\n"
           "# 标题\n"
           "正文第一段。\n")
body, fm = strip_frontmatter(fm_text)
check("剥掉 frontmatter", "type: rule" not in body, f"实际={body[:40]!r}")
check("正文保留", "正文第一段" in body)
check("解析出 tags 列表", fm.get("tags") == ["RAG", "切块"], f"实际={fm.get('tags')!r}")

# 负向：正文中间的水平线不能被当成 frontmatter 吞掉
no_fm = "第一段\n\n---\n\n第二段\n"
body2, fm2 = strip_frontmatter(no_fm)
check("中间的分割线不误吞", "第一段" in body2 and "第二段" in body2, f"实际={body2!r}")
check("没有 frontmatter 时元数据为空", fm2 == {})

# 负向：文件开头没有 --- 时不许自作聪明
body3, _ = strip_frontmatter("RAG 是什么\n\nRAG 是检索增强生成。")
check("无 frontmatter 时原文不动", body3.startswith("RAG 是什么"))


# ------------------------------------------------------------ 2. 字幕切块
print("\n=== 2. 字幕按时间戳切块 ===")
segs = [Segment(start=i * 5.0, end=i * 5.0 + 4.0, text=f"这是第{i}句话的内容。")
        for i in range(40)]
tr = Transcript(source="subtitle", text=" ".join(s.text for s in segs),
                segments=segs, engine="test")
chunks = chunk_transcript(tr, source_id="bilibili/BV1test", title="测试视频",
                          max_chars=100, overlap=0)
check("切出了多个块", len(chunks) > 1, f"实际={len(chunks)}")
check("每块都有时间轴", all(c.has_time for c in chunks))
check("块序号连续", [c.index for c in chunks] == list(range(len(chunks))),
      f"实际={[c.index for c in chunks][:8]}")
check("起始时间递增", all(chunks[i].start <= chunks[i + 1].start
                          for i in range(len(chunks) - 1)))
check("没有空块", all(c.text.strip() for c in chunks))
check("块长度在上限附近（不是一句一块）",
      all(len(c.text) <= 130 for c in chunks),
      f"最长={max(len(c.text) for c in chunks)}")
check("出处标签带标题", "测试视频" in chunks[0].cite, f"实际={chunks[0].cite!r}")

# 负向：没有时间轴的来源绝不能被当成有时间轴
art = Transcript(source="article", text="第一段。\n\n第二段。\n\n第三段。",
                 segments=[], engine="")
art_chunks = chunk_transcript(art, source_id="doc/x", title="一篇笔记")
check("无时间轴来源不伪造时间", all(not c.has_time for c in art_chunks))
check("无时间轴来源仍能切出内容", len(art_chunks) >= 1 and art_chunks[0].text)


# ------------------------------------------------------------ 3. 文档切块
print("\n=== 3. Markdown 按标题成节 ===")
md = ("# 顶层\n"
      "引言段落。\n"
      "\n"
      "## 架构 > 打包\n"
      "打包相关说明一。\n"
      "\n"
      "打包相关说明二。\n"
      "\n"
      "### 内置内核\n"
      "内置内核的细节说明。\n"
      "\n"
      "## 另一节\n"
      "另一节的内容。\n")
docs = chunk_document(md, source_id="doc/a.md", title="测试笔记", max_chars=400)
check("切出多个块", len(docs) >= 3, f"实际={len(docs)}")
check("标题路径写进块首", any("架构 > 打包" in c.text for c in docs),
      f"实际={[c.text[:20] for c in docs]}")
check("块首含完整层级", any("顶层 > 架构 > 打包 > 内置内核" in c.text for c in docs),
      f"实际={[c.text[:40] for c in docs]}")
check("文档块不带时间轴", all(not c.has_time for c in docs))
check("source_id 写入", all(c.source_id == "doc/a.md" for c in docs))

# 负向：超长单段必须被切开，且不能切出空块
long_para = "这是一句很长的测试文本。" * 60
big = chunk_document(long_para, source_id="doc/b.md", title="长文",
                     max_chars=200, overlap=20)
check("超长段落被切开", len(big) > 1, f"实际={len(big)}")
check("切完没有空块", all(c.text.strip() for c in big))
check("切开后每块仍不超上限太多",
      all(len(c.text) <= 260 for c in big), f"最长={max(len(c.text) for c in big)}")


# ------------------------------------------------------------ 4. 向量库读写
print("\n=== 4. 向量库落盘与重载 ===")
sdir = os.path.join(TMP, "store_roundtrip")
ctx = new_ctx()
st = LocalVectorStore(ctx, sdir)
st.clear()
st.add([Chunk(text="块一", source_id="s1", index=0),
        Chunk(text="块二", source_id="s1", index=1)],
       [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
check("写入 2 块", st.count() == 2, f"实际={st.count()}")
check("维度记录下来", st.dim == 4, f"实际={st.dim}")
check("索引文件已落盘", os.path.isfile(os.path.join(sdir, "index.json")))
check("向量文件已落盘", os.path.isfile(os.path.join(sdir, "vectors.f32")))

st2 = LocalVectorStore(ctx, sdir)
check("重载后条数一致", st2.count() == 2, f"实际={st2.count()}")
check("重载后维度一致", st2.dim == 4)
check("重载后文本一致",
      [c.text for c in st2.all_chunks()] == ["块一", "块二"],
      f"实际={[c.text for c in st2.all_chunks()]}")

# 排序：查询向量贴近哪个，哪个就该排第一
hits = st2.search([0.95, 0.05, 0.0, 0.0], top_k=2)
check("检索返回 2 条", len(hits) == 2)
check("更贴近的排在前面", hits[0].chunk.text == "块一", f"实际={hits[0].chunk.text}")
check("分数降序", hits[0].score >= hits[1].score)
check("via 标为 vector", hits[0].via == "vector")
check("归一化后相似度接近 1", abs(hits[0].score - 1.0) < 0.05,
      f"实际={hits[0].score:.4f}")

# 同来源同序号重复入库 → 必须替换而不是追加（否则同一段内容占两个名额）
st2.add([Chunk(text="块一改", source_id="s1", index=0)], [[1.0, 0.0, 0.0, 0.0]])
check("同源同序号被替换而非追加", st2.count() == 2, f"实际={st2.count()}")
check("替换后内容已更新",
      any(c.text == "块一改" for c in st2.all_chunks()),
      f"实际={[c.text for c in st2.all_chunks()]}")

# 负向：维度不一致必须抛错。静默兼容会让检索结果全是噪声，
# 而且症状是「答得莫名其妙」，根本推不到「库是坏的」。
try:
    st2.add([Chunk(text="维度不符", source_id="s9", index=0)], [[1.0, 0.0, 0.0]])
    check("维度不符时抛错", False, "居然接受了")
except RagError as e:
    check("维度不符时抛错", True)
    check("错误信息点明维度", "维度" in str(e), f"实际={e}")

# 负向：块数与向量数不一致必须抛错（错配会把 A 的向量配给 B 的文本）
st3 = LocalVectorStore(ctx, os.path.join(TMP, "store_mismatch"))
st3.clear()
try:
    st3.add([Chunk(text="a", source_id="s", index=0)], [[1.0], [2.0]])
    check("块数与向量数不符时抛错", False, "居然接受了")
except RagError:
    check("块数与向量数不符时抛错", True)

# 只存文本（无向量）也必须能落盘、能读回 —— 这是「向量服务挂了」时的形态
st4 = LocalVectorStore(ctx, os.path.join(TMP, "store_textonly"))
st4.clear()
st4.add([Chunk(text="只有文本", source_id="s", index=0)], None)
st4b = LocalVectorStore(ctx, os.path.join(TMP, "store_textonly"))
check("纯文本库能落盘并读回", st4b.count() == 1 and st4b.all_chunks()[0].text == "只有文本")
check("纯文本库 dim 为 0", st4b.dim == 0)
check("纯文本库向量检索返回空", st4b.search([1.0, 0.0], top_k=3) == [])


# ------------------------------------------------------------ 5. 检索
print("\n=== 5. 检索：向量优先 + 关键词兜底 ===")
rdir = os.path.join(TMP, "store_retr")
ctx = new_ctx()
st5 = LocalVectorStore(ctx, rdir)
st5.clear()
st5.add([Chunk(text="RAG 的切块策略决定检索上限", source_id="doc/a", index=0),
         Chunk(text="向量库用余弦相似度衡量语义接近程度", source_id="doc/a", index=1),
         Chunk(text="今天下午去楼下买一杯冰美式", source_id="doc/b", index=0)],
        [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 0.0, 1.0]])

fake = FakeEmbedder(ctx, dim=3)
ret = Retriever(ctx, store=st5, embedder=fake)
hits, mode = ret.search("切块", top_k=2)
check("有向量时走向量检索", mode == "vector", f"实际={mode}")
check("能召回", len(hits) >= 1)
check("向量命中标记为 vector", hits[0].via == "vector")
check("正常时没有向量错误", ret.last_error == "", f"实际={ret.last_error!r}")

# 维度不符（换了 embedding 模型但没重建库）不能被静默吞掉：
# 表现是「检索退化成关键词」，而根因在配置，必须留下可读原因。
ret_bad = Retriever(ctx, store=st5, embedder=FakeEmbedder(ctx, dim=8))
bad_hits, bad_mode = ret_bad.search("切块", top_k=2)
check("维度不符时不崩（仍能返回结果）", bad_mode in ("keyword", "mixed"),
      f"实际={bad_mode}")
check("维度不符时留下可读原因", "维度" in ret_bad.last_error,
      f"实际={ret_bad.last_error!r}")

# 关键词这一路：把 embedder 换成不可用的，仍要能搜到东西
class DeadEmbedder(FakeEmbedder):
    def available(self):
        return False, "模拟未配置 Key"


ret2 = Retriever(ctx, store=st5, embedder=DeadEmbedder(ctx))
hits2, mode2 = ret2.search("切块策略", top_k=2)
check("向量不可用时退化为关键词", mode2 == "keyword", f"实际={mode2}")
check("关键词路径仍能召回", len(hits2) >= 1, f"实际={len(hits2)}")
check("命中的是相关那条",
      any("切块" in h.chunk.text for h in hits2),
      f"实际={[h.chunk.text[:12] for h in hits2]}")
check("via 标为 keyword", hits2[0].via == "keyword")

# 完全没有向量的库：关键词是唯一出路
st6 = LocalVectorStore(ctx, os.path.join(TMP, "store_kw"))
st6.clear()
st6.add([Chunk(text="PyInstaller 打包后插件会丢失", source_id="doc/c", index=0),
         Chunk(text="与打包无关的一句话", source_id="doc/c", index=1)], None)
ret3 = Retriever(ctx, store=st6, embedder=DeadEmbedder(ctx))
hits3, mode3 = ret3.search("PyInstaller 插件", top_k=2)
check("纯文本库走关键词", mode3 == "keyword")
check("纯文本库能召回正确块",
      hits3 and "PyInstaller" in hits3[0].chunk.text,
      f"实际={[h.chunk.text[:16] for h in hits3]}")

# 空查询不该炸，也不该返回垃圾
empty_hits, empty_mode = ret3.search("   ", top_k=3)
check("空查询返回空结果", empty_hits == [] and empty_mode == "none",
      f"实际={empty_mode}/{len(empty_hits)}")

# 关键词打分：中英混合都要能算，且相关 > 无关
q = terms_of("RAG 切块策略")
check("中文切成 2-gram", "切块" in q and "块策" in q, f"实际={sorted(q)[:6]}")
check("英文按词保留", "rag" in q)
check("相关句得分高于无关句",
      keyword_score(q, terms_of("RAG 的切块策略决定检索上限"))
      > keyword_score(q, terms_of("今天下午买一杯冰美式")))
check("无交集得 0 分", keyword_score(q, terms_of("完全不相干的一句话内容")) == 0.0)
check("空输入得 0 分", keyword_score(set(), q) == 0.0 and keyword_score(q, set()) == 0.0)

check("不存在的来源目录也能构造（不抛错）",
      LocalVectorStore(ctx, os.path.join(TMP, "no_such_dir")).count() == 0)


# ------------------------------------------------------------ 6. 入库降级
print("\n=== 6. 入库：向量挂了也不能阻断 ===")
ctx7 = new_ctx()
st7 = LocalVectorStore(ctx7, os.path.join(TMP, "store_ingest"))
st7.clear()

info = ingest_chunks(ctx7, [Chunk(text="正常入库的内容", source_id="s", index=0)],
                     store=st7, embedder=FakeEmbedder(ctx7))
check("正常入库成功", info["ok"] and info["chunks"] == 1, f"实际={info}")
check("正常入库带向量", info["vectors"] is True)

info2 = ingest_chunks(ctx7, [Chunk(text="服务挂了也要进库", source_id="s2", index=0)],
                      store=st7, embedder=BrokenEmbedder(ctx7))
check("向量失败仍判定入库成功", info2["ok"] is True, f"实际={info2}")
check("向量失败时标记未生成", info2["vectors"] is False)
check("向量失败时给出提示", "向量" in (info2.get("message") or ""),
      f"实际={info2.get('message')!r}")
check("文本确实进了库", any(c.text == "服务挂了也要进库"
                            for c in st7.all_chunks()))
check("空内容不入库", ingest_chunks(ctx7, [], store=st7)["ok"] is False)


# ------------------------------------------------------------ 7. 提示词
print("\n=== 7. 提示词必须约束住幻觉 ===")
prompt_hits = [Hit(chunk=Chunk(text="切块上限设为 800 字符。", source_id="doc/a",
                               title="切块笔记", start=192.0, end=200.0), score=0.8)]
system, user = build_prompt("切块上限是多少？", prompt_hits)
check("声明只能依据片段", "只能依据" in system, f"实际={system[:60]!r}")
check("要求标注编号", "[编号]" in system)
check("允许回答「没有相关内容」", "没有相关内容" in system)
check("要求指出矛盾", "冲突" in system or "矛盾" in system)
check("问题进了 user", "切块上限是多少？" in user)
check("片段正文进了 user", "切块上限设为 800 字符。" in user)
check("出处标题进了 user", "切块笔记" in user)
check("时间戳被格式化成 mm:ss", "03:12" in user, f"实际={user[-120:]!r}")
check("片段有编号", "[1]" in user)

# 负向：没有命中时不能编，要明确告知「没有片段」
_, user_none = build_prompt("随便问问", [])
check("无命中时明确写没有片段", "没有检索到任何片段" in user_none, f"实际={user_none!r}")


# ------------------------------------------------------------ 8. 配置与回落
print("\n=== 8. 配置默认值与 Key 回落 ===")
from vsum.rag.retriever import build_embedder, build_store            # noqa: E402

cfg8 = Config({"asr": {"cloud_base_url": "https://api.siliconflow.cn/v1",
                       "cloud_api_key": "sk-test-1234"}})
emb8 = build_embedder(Context(cfg8))
ok8, why8 = emb8.available()
check("rag 未配置 Key 时回落到 asr 的 Key", ok8, f"实际={why8!r}")
base8, key8, model8 = emb8._endpoint()
check("回落拿到 base_url", base8.endswith("siliconflow.cn/v1"), f"实际={base8}")
check("回落拿到 api_key", key8 == "sk-test-1234")
check("默认模型是 bge-m3", model8 == "BAAI/bge-m3", f"实际={model8}")

cfg9 = Config({"rag": {"base_url": "https://mine.example/v1", "api_key": "sk-mine"},
               "asr": {"cloud_base_url": "https://api.siliconflow.cn/v1",
                       "cloud_api_key": "sk-other"}})
b9, k9, _ = build_embedder(Context(cfg9))._endpoint()
check("显式配置覆盖回落", b9 == "https://mine.example/v1" and k9 == "sk-mine",
      f"实际={b9}/{k9}")

cfg10 = Config({"rag": {"store": "不存在的实现"}})
check("未知 store 配置退回默认实现",
      build_store(Context(cfg10)).key == "local-json")

check("默认配置里有 rag 段", isinstance(Config().get("rag"), dict))
check("auto_ingest 默认关闭", Config().get("rag.auto_ingest") is False)


# ------------------------------------------------------------ 9. 出处可回溯
print("\n=== 9. 出处能回到原片的那一秒 ===")
c = Chunk(text="内容", source_id="bilibili/BV1x", title="某视频", start=192.0, end=200.0)
check("cite 含标题与时间", "某视频" in c.cite and "03:12" in c.cite, f"实际={c.cite!r}")
c2 = Chunk(text="内容", source_id="doc/a.md", title="笔记", start=-1, end=-1)
check("无时间轴时 cite 不带时间", c2.cite == "《笔记》", f"实际={c2.cite!r}")
check("to_dict/from_dict 往返一致",
      Chunk.from_dict(c.to_dict()).cite == c.cite)
check("Hit.to_dict 含分数与来路",
      set(Hit(chunk=c, score=0.5, via="vector").to_dict()) == {"chunk", "score", "via"})


# ------------------------------------------------------------ 清理与汇总
shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 56)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAIL:
    print("有失败项")
    sys.exit(1)
print("全部通过")
