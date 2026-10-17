"""
Module tích hợp Qwen3-Embedding & Kev-0.8B (Decision & Reranker)
cho dự án Personal Task Board thông qua chuẩn OpenAI API.
"""

import json
import logging
import os
import socket
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional
import numpy as np
from openai import OpenAI

logger = logging.getLogger("ptb.ai_service")


class Qwen3EmbeddingService:
    """Service tạo vector embedding 1024 chiều bằng Qwen3-Embedding-0.6B qua OpenAI API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8082/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("EMBEDDING_API_KEY", "local")
        self.model = model or os.getenv("EMBEDDING_MODEL", "qwen3-embedding-0.6b")
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        self._last_probe_time = 0.0
        self._last_available = False
        self.last_call_status: str = "initialized"
        self.last_usage_tokens: Optional[int] = None

    def _is_available(self) -> bool:
        """Fast check whether embedding endpoint is reachable with 30s cache."""
        now = time.time()
        if hasattr(self, "_last_probe_time") and (now - self._last_probe_time) < 30.0:
            return getattr(self, "_last_available", False)
        self._last_probe_time = now
        try:
            parsed = urllib.parse.urlparse(self.base_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 8082
            with socket.create_connection((host, port), timeout=0.1):
                self._last_available = True
                return True
        except (OSError, socket.error) as exc:
            logger.debug("Local Qwen3 endpoint %s:%s unavailable: %s", host, port, exc)
            if host in ("127.0.0.1", "localhost") and (os.getenv("IN_DOCKER") or os.path.exists("/.dockerenv")):
                try:
                    with socket.create_connection(("host.docker.internal", port), timeout=0.1):
                        self.base_url = self.base_url.replace(host, "host.docker.internal")
                        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
                        self._last_available = True
                        return True
                except (OSError, socket.error) as docker_exc:
                    logger.debug("Docker host fallback for Qwen3 failed: %s", docker_exc)
            self._last_available = False
            return False

    def embed_text(self, text: str) -> List[float]:
        """Tạo embedding cho một đoạn văn bản đơn lẻ."""
        if not self._is_available():
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
            if os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes"):
                raise RuntimeError(f"Embedding service at {self.base_url} is unavailable.")
            return [0.0] * 1024
        try:
            resp = self.client.embeddings.create(model=self.model, input=text)
            self.last_usage_tokens = getattr(getattr(resp, "usage", None), "total_tokens", None)
            self.last_call_status = "success"
            return resp.data[0].embedding
        except Exception as exc:
            self.last_call_status = "failure"
            logger.warning("Embedding service error during embed_text: %s", exc)
            if os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes"):
                raise RuntimeError(f"Embedding service at {self.base_url} failed: {exc}") from exc
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
            return [0.0] * 1024

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Tạo embedding theo lô (batch) cho danh sách câu."""
        if not texts:
            return []
        if not self._is_available():
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
            if os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes"):
                raise RuntimeError(f"Embedding service at {self.base_url} is unavailable.")
            return [[0.0] * 1024 for _ in texts]
        try:
            resp = self.client.embeddings.create(model=self.model, input=texts)
            self.last_usage_tokens = getattr(getattr(resp, "usage", None), "total_tokens", None)
            self.last_call_status = "success"
            return [item.embedding for item in resp.data]
        except Exception as exc:
            self.last_call_status = "failure"
            logger.warning("Embedding service error during embed_batch: %s", exc)
            if os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes"):
                raise RuntimeError(f"Embedding service at {self.base_url} failed: {exc}") from exc
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
            return [[0.0] * 1024 for _ in texts]

    def compute_similarity(self, query_vec: List[float], doc_vecs: List[List[float]]) -> List[float]:
        """Tính cosine similarity giữa query và tập documents."""
        q = np.array(query_vec)
        d = np.array(doc_vecs)
        norm_q = np.linalg.norm(q)
        norm_d = np.linalg.norm(d, axis=1)
        sims = (d @ q) / (norm_d * norm_q + 1e-9)
        return sims.tolist()


class KevDecisionReranker:
    """Service Reranker & Decision sử dụng Kev-0.8B chạy qua /v1/systemone của llama.cpp."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        # Lấy host:port từ DECISION_BASE_URL (ví dụ http://127.0.0.1:8081)
        raw_url = (base_url or os.getenv("DECISION_BASE_URL", "http://127.0.0.1:8081/v1")).rstrip("/")
        if raw_url.endswith("/v1"):
            self.root_url = raw_url[:-3]
        else:
            self.root_url = raw_url
        self.systemone_url = f"{self.root_url}/v1/systemone"
        self.api_key = api_key or os.getenv("DECISION_API_KEY", "local")
        self.model = model or os.getenv("DECISION_MODEL", "kev-0.8b")
        self._last_probe_time = 0.0
        self._last_available = False
        self.last_call_status: str = "initialized"
        self.last_usage_tokens: Optional[int] = None

    def _is_available(self) -> bool:
        """Fast check whether Kev service endpoint is reachable with 30s cache."""
        now = time.time()
        if hasattr(self, "_last_probe_time") and (now - self._last_probe_time) < 30.0:
            return getattr(self, "_last_available", False)
        self._last_probe_time = now
        try:
            parsed = urllib.parse.urlparse(self.root_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 8081
            with socket.create_connection((host, port), timeout=0.1):
                self._last_available = True
                return True
        except (OSError, socket.error) as exc:
            logger.debug("Local Kev endpoint %s:%s unavailable: %s", host, port, exc)
            # If host is localhost/127.0.0.1 and running inside Docker, try host.docker.internal
            if host in ("127.0.0.1", "localhost") and (os.getenv("IN_DOCKER") or os.path.exists("/.dockerenv")):
                try:
                    with socket.create_connection(("host.docker.internal", port), timeout=0.1):
                        self.root_url = self.root_url.replace(host, "host.docker.internal")
                        self.systemone_url = f"{self.root_url}/v1/systemone"
                        self._last_available = True
                        return True
                except (OSError, socket.error) as docker_exc:
                    logger.debug("Docker host fallback for Kev failed: %s", docker_exc)
            self._last_available = False
            return False

    def rerank(self, query: str, documents: List[str], top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Rerank danh sách documents theo query sử dụng thuật toán Choice Probabilities của Kev."""
        if not documents:
            return []
        if len(documents) == 1:
            self.last_call_status = "success"
            return [{"index": 0, "document": documents[0], "score": 1.0}]
        if not self._is_available():
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
            docs = documents[:top_k] if top_k else documents
            return [{"index": i, "document": d, "score": 0.5} for i, d in enumerate(docs)]

        criteria = {}
        idx_map = {}
        for i, doc in enumerate(documents):
            key = f"doc_{i}"
            criteria[key] = doc[:200]
            idx_map[key] = (i, doc)

        payload = {
            "state": f"Query: {query}",
            "questions": {
                "relevance": {
                    "type": "choice",
                    "instructions": "Which document is most relevant and directly answers the query?",
                    "criteria": criteria,
                }
            },
        }

        req = urllib.request.Request(
            self.systemone_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
                usage = result.get("usage", {})
                self.last_usage_tokens = usage.get("total_tokens") or usage.get("prompt_tokens")
                probs = result["answers"]["relevance"]["probabilities"]
                
                ranked = []
                for key, prob in probs.items():
                    if key in idx_map:
                        orig_idx, doc_text = idx_map[key]
                        ranked.append({
                            "index": orig_idx,
                            "document": doc_text,
                            "score": float(prob),
                        })
                ranked.sort(key=lambda x: x["score"], reverse=True)
                if top_k is not None:
                    ranked = ranked[:top_k]
                self.last_call_status = "success"
                return ranked
        except Exception as e:
            logger.warning("Kev rerank error: %s", e)
            if os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes"):
                self.last_call_status = "failure"
                raise RuntimeError(f"Kev Decision Reranker is unavailable: {e}") from e
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
            docs = documents[:top_k] if top_k else documents
            return [{"index": i, "document": d, "score": 0.5} for i, d in enumerate(docs)]

    def classify_task_urgency(self, task_text: str) -> Dict[str, Any]:
        """Đánh giá mức độ ưu tiên & tính khả thi của task bằng Kev System 1."""
        if not self._is_available():
            self.last_call_status = "fallback"
            return {
                "is_task_probability": 0.5,
                "urgency_score": 1.0,
                "priority_level": "Medium",
            }
        payload = {
            "state": f"Task: {task_text}",
            "questions": {
                "is_actionable": {
                    "type": "noul",
                    "instructions": "Is this a concrete actionable work task or commitment?",
                },
                "urgency": {
                    "type": "score",
                    "instructions": "How urgent is this task?",
                    "criteria": ["Thấp (can wait)", "Bình thường (this week)", "Ưu tiên (today)", "Khẩn cấp (immediate)"],
                }
            }
        }
        req = urllib.request.Request(
            self.systemone_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                res = json.loads(response.read().decode("utf-8"))
                answers = res["answers"]
                is_task_prob = answers["is_actionable"]["noul"]
                urgency_score = answers["urgency"]["score"] # 0 - 3
                
                # Map sang priority level
                if urgency_score >= 2.0:
                    priority = "High"
                elif urgency_score >= 1.0:
                    priority = "Medium"
                else:
                    priority = "Low"

                self.last_call_status = "success"
                return {
                    "is_task_probability": is_task_prob,
                    "urgency_score": urgency_score,
                    "priority_level": priority,
                }
        except Exception as e:
            logger.warning("Kev classification error: %s", e)
            if os.getenv("PTB_REQUIRE_LOCAL_AI", "false").lower() in ("true", "1", "yes"):
                self.last_call_status = "failure"
                raise RuntimeError(f"Kev Decision Classifier is unavailable: {e}") from e
            self.last_call_status = "fallback"
            return {
                "is_task_probability": 0.5,
                "urgency_score": 1.0,
                "priority_level": "Medium",
            }
