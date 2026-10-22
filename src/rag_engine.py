"""RAG 检索增强引擎（默认关闭，config.yaml 中 rag.enabled=true 开启）。

技术选型：
- ChromaDB 本地持久化（纯文件，无服务端）
- BAAI/bge-small-zh-v1.5 本地嵌入模型（中文优化，数据不出境）
- langchain Markdown 标题语义切块 + 超长块递归切分

工程注意（来自实战踩坑）：
1. 全进程只创建一个 PersistentClient，避免 Chroma 客户端锁冲突
2. 增量更新前先按 metadata.source 删除该文件的旧 chunk，防止陈旧内容污染召回
3. metadata 全部规范化为 str/int/float（部分 Chroma 版本不接受 bool）
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from src.config_loader import AppConfig

COLLECTION_NAME = "personal_knowledge"
_engine: "RAGEngine | None" = None  # 单例，防止重复创建客户端导致锁冲突


def _require_rag_deps():
    """惰性导入 RAG 重依赖：未开启 RAG 时不需要安装它们。"""
    try:
        import chromadb  # noqa: F401
        from chromadb.utils import embedding_functions  # noqa: F401
        from langchain_text_splitters import (  # noqa: F401
            MarkdownHeaderTextSplitter,
            RecursiveCharacterTextSplitter,
        )
    except ImportError as e:
        raise RuntimeError(
            "RAG 依赖未安装，请在项目虚拟环境中执行：\n"
            "  pip install chromadb langchain langchain-text-splitters sentence-transformers"
        ) from e


class RAGEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.rag_cfg = config.rag
        self.conversations_dir = config.dir_path("conversations")
        self.knowledge_dir = config.dir_path("knowledge")
        self.db_dir = config.dir_path("chroma_db")
        self.db_dir.mkdir(parents=True, exist_ok=True)

        # 国内 HF 镜像，加速嵌入模型下载（必须在导入 chromadb 嵌入函数前设置）
        if self.rag_cfg.hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", self.rag_cfg.hf_endpoint)
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

        import chromadb
        from chromadb.utils import embedding_functions

        self._client = chromadb.PersistentClient(path=str(self.db_dir))
        self._embedding = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=self.rag_cfg.embedding_model, device="cpu"
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._embedding,
            metadata={"hnsw:space": "cosine"},
        )

    # ---------- 切块 ----------
    def _split_markdown(self, text: str) -> list[tuple[str, dict]]:
        """按 Markdown 标题语义切块；超长块再按字符长度递归切分。

        返回 [(chunk_text, header_metadata), ...]
        """
        from langchain_text_splitters import (
            MarkdownHeaderTextSplitter,
            RecursiveCharacterTextSplitter,
        )

        header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "h1"),
                ("##", "h2"),
                ("###", "h3"),
            ],
            strip_headers=False,
        )
        try:
            header_docs = header_splitter.split_text(text)
        except Exception:
            header_docs = []
        if not header_docs:  # 没有任何标题的纯文本，整体作为一个文档
            from langchain_core.documents import Document

            header_docs = [Document(page_content=text, metadata={})]

        recursive = RecursiveCharacterTextSplitter(
            chunk_size=self.rag_cfg.chunk_size,
            chunk_overlap=self.rag_cfg.chunk_overlap,
        )
        chunks = recursive.split_documents(header_docs)
        return [(c.page_content.strip(), c.metadata or {}) for c in chunks if c.page_content.strip()]

    @staticmethod
    def _chunk_id(source: str, index: int) -> str:
        return hashlib.md5(f"{source}::{index}".encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_meta(meta: dict) -> dict:
        """Chroma metadata 只接受 str/int/float/None，bool 转 0/1。"""
        out: dict = {}
        for k, v in meta.items():
            if isinstance(v, bool):
                out[k] = int(v)
            elif v is None:
                continue
            elif isinstance(v, (str, int, float)):
                out[k] = v
            else:
                out[k] = str(v)
        return out

    # ---------- 入库 ----------
    def _delete_source(self, source: str) -> None:
        """删除某个文件在向量库中的全部旧 chunk（增量更新的关键步骤）。"""
        existing = self._collection.get(where={"source": source})
        if existing and existing.get("ids"):
            self._collection.delete(ids=existing["ids"])

    def incremental_update(self, file_path: str | Path) -> int:
        """单个 MD 文件的增量同步（新增/修改/删除），返回写入的 chunk 数。

        文件已删除时：清除该 source 的全部旧 chunk，返回 0。
        """
        path = Path(file_path).resolve()
        if not path.exists():
            # 文件被删除：清除其在向量库中的残留 chunk
            self._delete_source(str(path))
            return 0
        if path.suffix.lower() != ".md":
            return 0

        source = str(path)
        doc_type = (
            "conversation" if self.conversations_dir in path.parents else "knowledge"
        )
        text = path.read_text(encoding="utf-8")
        chunks = self._split_markdown(text)

        # 先清旧再写新，避免文件内容变化后旧 chunk 残留污染召回
        self._delete_source(source)
        if not chunks:
            return 0

        ids, documents, metas = [], [], []
        for i, (content, header_meta) in enumerate(chunks):
            ids.append(self._chunk_id(source, i))
            documents.append(content)
            meta = self._normalize_meta(
                {
                    "source": source,
                    "type": doc_type,
                    "mtime": int(path.stat().st_mtime),
                    "title": header_meta.get("h1") or path.stem,
                    **header_meta,
                }
            )
            metas.append(meta)
        self._collection.add(ids=ids, documents=documents, metadatas=metas)
        return len(chunks)

    def build_vector_db(self) -> dict[str, int]:
        """全量扫描 conversations/ 和 knowledge/ 下所有 MD，重建向量库。

        返回 {文件路径: chunk 数}。
        """
        md_files: list[Path] = []
        for d in (self.knowledge_dir, self.conversations_dir):
            if d.exists():
                md_files.extend(sorted(d.rglob("*.md")))

        if not md_files:
            print("  [RAG] conversations/ 和 knowledge/ 下没有找到任何 .md 文件")
            return {}

        stats: dict[str, int] = {}
        for path in md_files:
            n = self.incremental_update(path)
            stats[str(path)] = n
            print(f"  [RAG] 已入库 {path.name}（{n} 个片段）")
        return stats

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        """根据问题检索最相关的文本片段。"""
        top_k = top_k or self.rag_cfg.top_k
        total = self._collection.count()
        if total == 0:
            return []
        result = self._collection.query(
            query_texts=[query],
            n_results=min(top_k, total),
            include=["documents", "metadatas", "distances"],
        )
        items: list[dict] = []
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]
        for content, meta, dist in zip(docs, metas, dists):
            items.append(
                {
                    "content": content,
                    "source": meta.get("source", ""),
                    "title": meta.get("title", ""),
                    "distance": round(float(dist), 4),
                }
            )
        return items

    def format_context(self, query: str, top_k: int | None = None) -> str:
        """检索并组装成可直接注入 system prompt 的上下文段落。"""
        hits = self.search(query, top_k)
        if not hits:
            return ""
        parts = ["以下是从你的本地知识库和历史对话中检索到的相关资料，请优先参考："]
        for i, h in enumerate(hits, 1):
            parts.append(f"\n[资料{i}] 来源：{Path(h['source']).name}\n{h['content']}")
        return "\n".join(parts)


def get_rag_engine(config: AppConfig, force: bool = False) -> RAGEngine | None:
    """获取 RAG 引擎单例。

    - 配置 rag.enabled=true 时默认可用
    - force=True 时（CLI /rag on、请求级 rag:true）即使配置关闭也临时开启
    - 依赖缺失时返回 None（RAG 优雅降级为关闭）
    """
    global _engine
    if not (config.rag.enabled or force):
        return None
    if _engine is None:
        _require_rag_deps()
        _engine = RAGEngine(config)
    return _engine
