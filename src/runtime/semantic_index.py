"""
SemanticIndex — FAISS + nomic-embed-text semantic search for Pulse cold tier.

Wraps FAISS flat-L2 index with Ollama nomic-embed-text embeddings.
Falls back to keyword search if Ollama is unavailable.

Files written to index_dir/:
  faiss.index     — FAISS flat index (float32, 768-dim)
  faiss-meta.jsonl — one metadata record per vector (content, type, ts, source)
  faiss-id.json   — next_id counter

Usage:
    idx = SemanticIndex(Path("~/.pulse/state/cold-tier"))
    idx.add([{"content": "...", "type": "MSG", "ts": "...", "ts_unix": 1234}])
    results = idx.search("weather bot status", top_k=5)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("pulse.runtime.semantic_index")

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768
BATCH_SIZE = 32  # embed N entries at a time


def _embed(texts: list[str]) -> Optional[list[list[float]]]:
    """Get embeddings from Ollama nomic-embed-text. Returns None on failure."""
    try:
        results = []
        for text in texts:
            body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/embeddings",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.load(r)
            vec = d.get("embedding")
            if not vec or len(vec) != EMBED_DIM:
                return None
            results.append(vec)
        return results
    except Exception as e:
        logger.debug("Embed failed: %s", e)
        return None


class SemanticIndex:
    """FAISS semantic index with Ollama embeddings + keyword fallback."""

    def __init__(self, index_dir: Path) -> None:
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._faiss_path = self.index_dir / "faiss.index"
        self._meta_path = self.index_dir / "faiss-meta.jsonl"
        self._id_path = self.index_dir / "faiss-id.json"
        self._lock = threading.RLock()
        self._index = None
        self._meta: list[dict] = []
        self._next_id: int = 0
        self._faiss_available = False
        self._embed_available = False
        self._load()

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load FAISS index + metadata from disk."""
        try:
            import faiss as _faiss  # noqa: F401
            self._faiss_available = True
        except ImportError:
            logger.info("FAISS not available — keyword fallback only")
            return

        # Test embed availability (quick, cached)
        test = _embed(["test"])
        self._embed_available = test is not None

        if not self._embed_available:
            logger.info("nomic-embed-text unavailable — keyword fallback only")
            return

        import faiss
        # Load or create index
        if self._faiss_path.exists():
            try:
                self._index = faiss.read_index(str(self._faiss_path))
                logger.info("Loaded FAISS index (%d vectors)", self._index.ntotal)
            except Exception as e:
                logger.warning("FAISS index corrupt, rebuilding: %s", e)
                self._index = None

        if self._index is None:
            self._index = faiss.IndexFlatL2(EMBED_DIM)

        # Load metadata
        if self._meta_path.exists():
            try:
                self._meta = [
                    json.loads(l)
                    for l in self._meta_path.read_text().splitlines()
                    if l.strip()
                ]
            except Exception:
                self._meta = []

        # Load next_id
        if self._id_path.exists():
            try:
                self._next_id = json.loads(self._id_path.read_text()).get("next_id", 0)
            except Exception:
                self._next_id = len(self._meta)

    def _save_locked(self) -> None:
        """Persist index + metadata. Caller must hold self._lock."""
        if self._index is None:
            return
        try:
            import faiss
            faiss.write_index(self._index, str(self._faiss_path))
            tmp = self._meta_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                for m in self._meta:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, self._meta_path)
            self._id_path.write_text(json.dumps({"next_id": self._next_id}))
        except Exception as e:
            logger.warning("FAISS save failed: %s", e)

    # ------------------------------------------------------------------
    # Add
    # ------------------------------------------------------------------

    def add(self, entries: list[dict]) -> int:
        """Embed and index entries. Returns number successfully added."""
        if not entries:
            return 0

        if not self._faiss_available or not self._embed_available or self._index is None:
            return 0  # silently skip — keyword search will handle it

        import numpy as np

        # Process in batches
        added = 0
        for i in range(0, len(entries), BATCH_SIZE):
            batch = entries[i: i + BATCH_SIZE]
            texts = [str(e.get("content", ""))[:512] for e in batch]

            vecs = _embed(texts)
            if not vecs:
                logger.debug("Embedding batch failed, skipping %d entries", len(batch))
                continue

            with self._lock:
                import faiss  # re-import inside lock is fine (module cached)
                mat = np.array(vecs, dtype=np.float32)
                # Normalise for cosine similarity (L2 on normalised = cosine)
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms = np.where(norms == 0, 1.0, norms)
                mat = mat / norms

                self._index.add(mat)
                for e in batch:
                    self._meta.append({
                        "id": self._next_id,
                        "content": str(e.get("content", ""))[:500],
                        "type": e.get("type", ""),
                        "source": e.get("source", ""),
                        "ts": e.get("ts", ""),
                        "ts_unix": float(e.get("ts_unix", time.time())),
                    })
                    self._next_id += 1
                added += len(batch)

        if added > 0:
            with self._lock:
                self._save_locked()
            logger.info("SemanticIndex: added %d vectors (total=%d)", added, self._next_id)

        return added

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Semantic search. Falls back to keyword if FAISS/embed unavailable."""
        if not query.strip():
            return []

        if self._faiss_available and self._embed_available and self._index is not None and self._index.ntotal > 0:
            return self._semantic_search(query, top_k)
        else:
            return []  # caller falls back to keyword

    def _semantic_search(self, query: str, top_k: int) -> list[dict]:
        """Pure FAISS semantic search."""
        import numpy as np

        vecs = _embed([query])
        if not vecs:
            return []

        vec = np.array([vecs[0]], dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm

        with self._lock:
            k = min(top_k, self._index.ntotal)
            if k == 0:
                return []
            distances, indices = self._index.search(vec, k)

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(self._meta):
                continue
            meta = dict(self._meta[idx])
            # Convert L2 distance on normalised vectors → cosine similarity
            score = max(0.0, 1.0 - float(dist) / 2.0)
            meta["score"] = round(score, 4)
            results.append(meta)

        return sorted(results, key=lambda x: x["score"], reverse=True)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def count(self) -> int:
        if self._index is None:
            return 0
        return self._index.ntotal

    def status(self) -> dict:
        return {
            "faiss_available": self._faiss_available,
            "embed_available": self._embed_available,
            "vectors": self.count(),
            "model": EMBED_MODEL if self._embed_available else "keyword-fallback",
        }
