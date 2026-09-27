"""Keyword (BM25) retrieval over the approved knowledge chunks, filtered by audience.

BM25 is deterministic, needs no embedding service, and is plenty for a few hundred policy
documents. If evals show it missing paraphrased or Malayalam questions, add an embedding
retriever behind the same `search` interface and merge the two result lists.
"""

import math
import re
from collections import Counter

from app.knowledge.loader import Chunk

# Which knowledge audiences each user role may read.
VISIBLE_TO = {
    "patient": {"patient"},
    "staff": {"patient", "staff"},
    "clinician": {"patient", "staff", "clinician"},
}

_TOKEN = re.compile(r"[^\s.,;:!?()\[\]{}\"'/\\|<>*#`~_=+-]+")
_STOP = set(
    "a an the is are was were be to of in on at for and or with by from what when where how who "
    "which can i my me we our you your do does did please tell about it this that there any".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in (m.group(0).lower() for m in _TOKEN.finditer(text)) if t not in _STOP]


class Retriever:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(f"{c.doc_title} {c.section} {c.text}")) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if chunks else 1.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, role: str, top_k: int = 5, min_score: float = 0.0) -> list[tuple[Chunk, float]]:
        allowed = VISIBLE_TO.get(role, {"patient"})
        terms = tokenize(query)
        scored = []
        for i, chunk in enumerate(self.chunks):
            if not (chunk.audience & allowed):
                continue
            tf, length = self._tf[i], self._len[i]
            score = 0.0
            for t in terms:
                f = tf.get(t)
                if f:
                    score += self._idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self._avg))
            if score >= min_score and score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
