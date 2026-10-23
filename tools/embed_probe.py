"""阶段 0 探针：验证硅基流动 BAAI/bge-m3 的 embedding 接口。

只回答三个问题：
  1. 接口能不能通（Key / 地址 / 网络）
  2. 向量维度是多少（决定后面向量库的建表参数）
  3. 中文语义区分度够不够（相关文本的相似度是否明显高于无关文本）

不写入任何项目产物，随时可删。
运行：.venv/Scripts/python.exe tools/embed_probe.py
"""

import json
import math
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# 复用已有的云端配置（ASR 走的就是硅基流动，Key 和地址都一样）
base = cfg["asr"]["cloud_base_url"].rstrip("/")
key = cfg["asr"]["cloud_api_key"]
model = "BAAI/bge-m3"

# 三句话：前两句语义相关（都在讲 RAG 的实现细节），第三句故意无关
docs = [
    "RAG 的切块策略决定了检索上限，按固定字符数硬切会把一句话从中间切断。",
    "向量库检索用余弦相似度衡量两段文本的语义接近程度。",
    "今天下午我打算去楼下买一杯冰美式。",
]

resp = requests.post(
    f"{base}/embeddings",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    json={"model": model, "input": docs, "encoding_format": "float"},
    timeout=60,
)
print("HTTP 状态码:", resp.status_code)
if resp.status_code != 200:
    print("接口失败，原文:", resp.text[:500])
    sys.exit(1)

vecs = [d["embedding"] for d in resp.json()["data"]]
print("向量维度:", len(vecs[0]), "| 返回条数:", len(vecs))


def cos(a, b):
    """余弦相似度：只看方向不看长度，值域 -1~1，越大越接近。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


rel = cos(vecs[0], vecs[1])
irr = cos(vecs[0], vecs[2])
print(f"相关对（切块策略 vs 向量检索）: {rel:.4f}")
print(f"无关对（切块策略 vs 买咖啡）  : {irr:.4f}")
print("区分度:", "通过" if rel > irr else "异常——相关句反而更远，需要换模型")
