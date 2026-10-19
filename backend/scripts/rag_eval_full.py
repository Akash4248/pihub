#!/usr/bin/env python3
"""Comprehensive RAG evaluation harness for the content-pipeline /rag/search endpoint.

Ground truth is derived from the indexed corpus metadata (Qdrant payload:
chapter/subject/grade/keywords/concepts). Relevance is defined objectively:

  * chapter queries   -> every chunk belonging to the target chapter is relevant
  * concept queries   -> every chunk whose keywords/concepts/text contains the term
  * subject queries   -> every chunk in that grade+subject containing the term
  * multilingual      -> every chunk containing the (English) target term
  * negative queries  -> no relevant chunk (used to measure over-confident answers)

Reports Recall/Precision/HitRate/nDCG @ {1,3,5,10}, MRR@10, MAP@10, latency
percentiles, throughput, and a per-query-type breakdown.

Usage:
    python scripts/rag_eval_full.py \
        --qdrant http://127.0.0.1:6333 --rag http://127.0.0.1:8011 \
        --out /tmp/opencode/rag_eval_full.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import time
import urllib.request
from collections import Counter, defaultdict

K_VALUES = [1, 3, 5, 10]

HINDI_TERMS = {
    "water": "पानी",
    "air": "हवा",
    "plant": "पौधा",
    "photosynthesis": "प्रकाश संश्लेषण",
    "number": "संख्या",
    "addition": "जोड़",
    "subtraction": "घटाव",
    "fraction": "भिन्न",
    "soil": "मिट्टी",
    "light": "प्रकाश",
    "energy": "ऊर्जा",
    "cell": "कोशिका",
    "force": "बल",
    "motion": "गति",
    "animal": "जानवर",
    "food": "भोजन",
    "money": "पैसा",
    "magnet": "चुंबक",
}

NEGATIVE_QUERIES = [
    "who won the 2024 cricket world cup final",
    "how to install kubernetes on ubuntu 24.04",
    "best stock picks for next quarter",
    "latest iphone pro max price in usa",
    "how to write a react hook for websockets",
    "what is the capital of france",
    "recipe for paneer butter masala",
    "how to change a car tyre",
    "python asyncio semaphore example",
    "bitcoin price prediction 2030",
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were",
    "you", "your", "we", "our", "they", "it", "this", "that", "these", "those",
    "how", "what", "which", "when", "where", "who", "why", "there", "here",
    "can", "will", "would", "should", "could", "do", "does", "did", "be", "been",
    "have", "has", "had", "not", "no", "yes", "on", "at", "by", "for", "with",
    "from", "as", "but", "if", "then", "than", "so", "up", "down", "out", "about",
    "into", "over", "after", "one", "two", "three", "many", "much", "more", "most",
    "some", "any", "all", "each", "every", "other", "such", "only", "own", "same",
    "also", "just", "now", "let", "us", "see", "look", "using", "used", "use",
    "given", "find", "write", "make", "take", "like", "number", "numbers",
}


AUTH_HEADERS: dict[str, str] = {}


def post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **AUTH_HEADERS}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=AUTH_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def scroll_all(qdrant: str, collection: str) -> list[dict]:
    points: list[dict] = []
    offset = None
    while True:
        body = {"limit": 1000, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        res = post_json(f"{qdrant}/collections/{collection}/points/scroll", body, timeout=120)
        result = res["result"]
        points.extend(result["points"])
        offset = result.get("next_page_offset")
        if not offset:
            break
    return points


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def tokenize(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z]{3,}", (s or "").lower()) if t not in STOPWORDS]


def dcg(gains: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(rel: set[str], ranked: list[str], k: int) -> float:
    gains = [1 if rid in rel else 0 for rid in ranked[:k]]
    ideal = sorted([1] * min(len(rel), k) + [0] * max(0, k - len(rel)), reverse=True)[:k]
    idcg = dcg(ideal)
    return (dcg(gains) / idcg) if idcg > 0 else 0.0


def average_precision(rel: set[str], ranked: list[str], k: int) -> float:
    if not rel:
        return 0.0
    hits, score = 0, 0.0
    for i, rid in enumerate(ranked[:k]):
        if rid in rel:
            hits += 1
            score += hits / (i + 1)
    return score / min(len(rel), k)


def reciprocal_rank(rel: set[str], ranked: list[str], k: int) -> float:
    for i, rid in enumerate(ranked[:k]):
        if rid in rel:
            return 1.0 / (i + 1)
    return 0.0


def build_queries(corpus: list[dict], rng: random.Random, max_per_type: int) -> list[dict]:
    by_chapter: dict[tuple, list[dict]] = defaultdict(list)
    by_id = {}
    for rec in corpus:
        by_chapter[(rec["grade"], rec["subject"], rec["chapter"])].append(rec)
        by_id[rec["id"]] = rec

    # term -> set of ids (keywords + concepts + text)
    term_ids: dict[str, set[str]] = defaultdict(set)
    for rec in corpus:
        terms = set(rec["keywords"]) | set(rec["concepts"])
        for t in terms:
            if len(t) >= 4 and t not in STOPWORDS:
                term_ids[t].add(rec["id"])

    queries: list[dict] = []

    # A) chapter / topic queries
    chapter_keys = [k for k, v in by_chapter.items() if len(v) >= 4]
    rng.shuffle(chapter_keys)
    for grade, subject, chapter in chapter_keys[:max_per_type]:
        rel = {r["id"] for r in by_chapter[(grade, subject, chapter)]}
        queries.append({
            "qtype": "chapter",
            "query": re.sub(r"\b(english|hindi)\b", "", chapter).strip(),
            "relevant": rel,
            "chapter": chapter, "subject": subject, "grade": grade,
        })

    # B/C/D) concept, definition-phrased, subject-scoped
    concepts = [(t, ids) for t, ids in term_ids.items() if 1 <= len(ids) <= 25]
    concepts.sort(key=lambda x: -len(x[1]))
    rng.shuffle(concepts)
    used = 0
    for term, ids in concepts:
        if used >= max_per_type:
            break
        sample = by_id[next(iter(ids))]
        grade, subject = sample["grade"], sample["subject"]
        queries.append({"qtype": "concept", "query": term, "relevant": set(ids),
                        "term": term, "subject": subject, "grade": grade})
        tmpl = rng.choice(["what is {}", "explain {}", "define {} in simple words"])
        queries.append({"qtype": "definition", "query": tmpl.format(term), "relevant": set(ids),
                        "term": term, "subject": subject, "grade": grade})
        if used < max_per_type:
            queries.append({"qtype": "subject_scoped",
                            "query": f"class {grade} {subject} {term}", "relevant": set(ids),
                            "term": term, "subject": subject, "grade": grade})
        used += 1

    # E) multilingual (Hindi -> English corpus)
    for en, hi in HINDI_TERMS.items():
        ids = term_ids.get(en, set())
        if ids:
            queries.append({"qtype": "multilingual", "query": hi, "relevant": set(ids),
                            "term": en, "subject": None, "grade": None})

    # F) negatives
    for q in NEGATIVE_QUERIES:
        queries.append({"qtype": "negative", "query": q, "relevant": set(),
                        "term": None, "subject": None, "grade": None})

    return queries


def run_query(rag: str, query: str, limit: int, filters: dict | None = None) -> tuple[list[str], list[float], float]:
    payload = {"query": query, "limit": limit}
    if filters:
        payload["filters"] = filters
    t0 = time.perf_counter()
    results = []
    for attempt in range(2):
        try:
            data = post_json(f"{rag}/rag/search", payload, timeout=60)
            results = data.get("results", [])
            break
        except Exception:
            if attempt == 1:
                results = []
    elapsed = (time.perf_counter() - t0) * 1000.0
    ids = [r.get("id") for r in results]
    scores = [r.get("score", 0.0) for r in results]
    return ids, scores, elapsed


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1))))
    return values[idx]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qdrant", default="http://127.0.0.1:6333")
    ap.add_argument("--collection", default="educational_chunks")
    ap.add_argument("--rag", default="http://127.0.0.1:8011")
    ap.add_argument("--max-per-type", type=int, default=40)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--throughput-n", type=int, default=150)
    ap.add_argument("--throughput-workers", type=int, default=8)
    ap.add_argument("--out", default="/tmp/opencode/rag_eval_full.json")
    ap.add_argument("--internal-key", default=os.environ.get("RAG_INTERNAL_KEY", ""),
                    help="X-Internal-Service-Key for the RAG service (auth-enabled deployments)")
    args = ap.parse_args()

    if args.internal_key:
        AUTH_HEADERS["X-Internal-Service-Key"] = args.internal_key
        print("[info] using internal service key for RAG requests")

    rng = random.Random(args.seed)
    print(f"[info] scrolling corpus from {args.qdrant} ...")
    raw = scroll_all(args.qdrant, args.collection)
    corpus = []
    for p in raw:
        pl = p.get("payload") or {}
        corpus.append({
            "id": p["id"],
            "text": pl.get("text", ""),
            "chapter": pl.get("chapter", ""),
            "subject": pl.get("subject", ""),
            "grade": pl.get("grade", ""),
            "language": pl.get("language", ""),
            "keywords": [norm(k) for k in (pl.get("keywords") or [])],
            "concepts": [norm(c) for c in (pl.get("concepts") or [])],
        })
    print(f"[info] corpus size = {len(corpus)} chunks")

    queries = build_queries(corpus, rng, args.max_per_type)
    counts = Counter(q["qtype"] for q in queries)
    print(f"[info] queries = {len(queries)} {dict(counts)}")

    per_query = []
    latencies = []
    for i, q in enumerate(queries, 1):
        ids, scores, ms = run_query(args.rag, q["query"], args.limit)
        latencies.append(ms)
        rel = q["relevant"]
        row = {
            "qtype": q["qtype"], "query": q["query"],
            "n_relevant": len(rel), "returned": len(ids),
            "top_score": round(scores[0], 4) if scores else 0.0,
            "latency_ms": round(ms, 1),
        }
        if rel:
            for k in K_VALUES:
                topk = ids[:k]
                hit = len(rel & set(topk))
                row[f"hit@{k}"] = 1 if hit else 0
                row[f"precision@{k}"] = hit / k
                row[f"recall@{k}"] = hit / len(rel)
                row[f"ndcg@{k}"] = round(ndcg_at_k(rel, ids, k), 4)
            row["mrr@10"] = round(reciprocal_rank(rel, ids, args.limit), 4)
            row["map@10"] = round(average_precision(rel, ids, args.limit), 4)
        per_query.append(row)
        if i % 25 == 0:
            print(f"  ... {i}/{len(queries)}")

    # aggregate
    def agg(rows):
        out = {}
        for k in K_VALUES:
            for m in ("hit", "precision", "recall", "ndcg"):
                key = f"{m}@{k}"
                vals = [r[key] for r in rows if key in r]
                out[key] = round(statistics.mean(vals), 4) if vals else None
        for key in ("mrr@10", "map@10"):
            vals = [r[key] for r in rows if key in r]
            out[key] = round(statistics.mean(vals), 4) if vals else None
        out["n_queries"] = len(rows)
        return out

    positive = [r for r in per_query if r["n_relevant"] > 0]
    negatives = [r for r in per_query if r["qtype"] == "negative"]

    report = {
        "corpus_chunks": len(corpus),
        "queries_total": len(queries),
        "queries_scored": len(positive),
        "negative_queries": len(negatives),
        "overall": agg(positive),
        "by_type": {t: agg([r for r in positive if r["qtype"] == t]) for t in sorted(set(r["qtype"] for r in positive))},
        "negative_analysis": {
            "avg_top_score": round(statistics.mean([r["top_score"] for r in negatives]), 4) if negatives else None,
            "max_top_score": round(max([r["top_score"] for r in negatives]), 4) if negatives else None,
            "frac_top_score_below_0.5": round(
                statistics.mean([1 if r["top_score"] < 0.5 else 0 for r in negatives]), 4) if negatives else None,
        },
        "latency_ms": {
            "n": len(latencies),
            "mean": round(statistics.mean(latencies), 1),
            "p50": round(percentile(latencies, 50), 1),
            "p90": round(percentile(latencies, 90), 1),
            "p95": round(percentile(latencies, 95), 1),
            "p99": round(percentile(latencies, 99), 1),
            "min": round(min(latencies), 1),
            "max": round(max(latencies), 1),
        },
    }

    # throughput test (sequential burst)
    print("[info] throughput test (sequential) ...")
    burst = [rng.choice(positive)["query"] for _ in range(args.throughput_n)]
    t0 = time.perf_counter()
    for q in burst:
        run_query(args.rag, q, 5)
    seq_elapsed = time.perf_counter() - t0
    report["throughput_sequential_qps"] = round(args.throughput_n / seq_elapsed, 2)

    # throughput test (concurrent)
    print(f"[info] throughput test (concurrent, {args.throughput_workers} workers) ...")
    import threading
    lock = threading.Lock()
    done = 0

    def worker():
        nonlocal done
        for _ in range(args.throughput_n // args.throughput_workers):
            run_query(args.rag, rng.choice(positive)["query"], 5)
            with lock:
                done += 1

    threads = [threading.Thread(target=worker) for _ in range(args.throughput_workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conc_elapsed = time.perf_counter() - t0
    report["throughput_concurrent_qps"] = round(done / conc_elapsed, 2)
    report["throughput_concurrent_workers"] = args.throughput_workers

    # server-side stage metrics
    try:
        report["server_metrics"] = get_json(f"{args.rag}/rag/metrics")
    except Exception as exc:
        report["server_metrics"] = {"error": str(exc)}

    with open(args.out, "w") as f:
        json.dump({"report": report, "per_query": per_query}, f, indent=2, ensure_ascii=False)

    print("\n===== RAG EVALUATION SUMMARY =====")
    print(json.dumps(report["overall"], indent=2))
    print("\nby type:")
    for t, m in report["by_type"].items():
        print(f"  {t:14s} n={m['n_queries']:3d}  "
              f"nDCG@5={m['ndcg@5']}  recall@5={m['recall@5']}  P@5={m['precision@5']}  MRR={m['mrr@10']}")
    print("\nlatency_ms:", report["latency_ms"])
    print("throughput seq/concurrent QPS:", report["throughput_sequential_qps"], "/", report["throughput_concurrent_qps"])
    print("negatives:", report["negative_analysis"])
    print(f"\n[written] {args.out}")


if __name__ == "__main__":
    main()
