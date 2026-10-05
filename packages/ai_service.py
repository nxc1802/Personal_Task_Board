"""
Module tích hợp Qwen3-Embedding & Kev-0.8B (Decision & Reranker)
cho dự án Personal Task Board thông qua chuẩn OpenAI API.
"""

import os
import numpy as np
import urllib.request
import json
from typing import List, Dict, Any, Optional
from openai import OpenAI

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

    def embed_text(self, text: str) -> List[float]:
        """Tạo embedding cho một đoạn văn bản đơn lẻ."""
        resp = self.client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Tạo embedding theo lô (batch) cho danh sách câu."""
        if not texts:
            return []
        resp = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in resp.data]

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

    def rerank(self, query: str, documents: List[str], top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Rerank danh sách documents theo query sử dụng thuật toán Choice Probabilities của Kev."""
        if not documents:
            return []
        if len(documents) == 1:
            return [{"index": 0, "document": documents[0], "score": 1.0}]

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
                return ranked
        except Exception as e:
            # Fallback nếu gặp lỗi mạng
            return [{"index": i, "document": doc, "score": 1.0 / (i + 1)} for i, doc in enumerate(documents)]

    def classify_task_urgency(self, task_text: str) -> Dict[str, Any]:
        """Đánh giá mức độ ưu tiên & tính khả thi của task bằng Kev System 1."""
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

            return {
                "is_task_probability": is_task_prob,
                "urgency_score": urgency_score,
                "priority_level": priority,
            }
