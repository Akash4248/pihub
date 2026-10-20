import json, time, re, urllib.request, statistics, collections

CP = "http://127.0.0.1:8001"
QDRANT = "http://127.0.0.1:6333"
STOP = {"and","the","of","in","a","to","for","part","class","introduction","contents"}

def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))

def get(url):
    return json.load(urllib.request.urlopen(url, timeout=60))

# ---- build ground truth from the indexed corpus ----
off = None
chapters = {}
while True:
    body = {"limit":1000,"with_payload":["chapter","textbook_name","subject","grade"],"with_vector":False}
    if off: body["offset"] = off
    r = post(f"{QDRANT}/collections/educational_chunks/points/scroll", body)["result"]
    for p in r["points"]:
        pl = p["payload"]
        ch = (pl.get("chapter") or "").strip()
        if ch and ch not in STOP and len(ch) > 3:
            chapters.setdefault(ch, pl)
    off = r.get("next_page_offset")
    if not off: break

print(f"[info] distinct chapters in index: {len(chapters)}")

def clean(ch):
    return re.sub(r"\s+", " ", ch.replace("_", " ")).strip()

# deterministic sample of 50 chapters
sample = sorted(chapters.items())[:50] if len(chapters) <= 60 else [sorted(chapters.items())[i] for i in range(0, len(chapters), max(1,len(chapters)//50))][:50]
tests = []
for ch, pl in sample:
    q = f"What is {clean(ch)}?"
    tests.append({"query": q, "expected": ch, "subject": pl.get("subject"), "grade": pl.get("grade")})

def relevant(payload, expected):
    hay = f"{payload.get('chapter','')} {payload.get('textbook_name','')} {payload.get('topic','')}".lower().replace("_"," ")
    exp = expected.lower().replace("_"," ")
    if exp and exp in hay:
        return True
    et = {t for t in re.findall(r"[a-z]+", exp) if t not in STOP and len(t) > 2}
    if not et: return False
    ht = set(re.findall(r"[a-z]+", hay))
    return len(et & ht) / len(et) >= 0.5

def dcg(rels):
    return sum(r / (1 if i == 0 else (i+1).bit_length() and __import__("math").log2(i+2)) for i, r in enumerate(rels))

import math
def ndcg(rels):
    d = sum(r / math.log2(i+2) for i, r in enumerate(rels))
    ideal = sorted(rels, reverse=True)
    idl = sum(r / math.log2(i+2) for i, r in enumerate(ideal))
    return d/idl if idl else 0.0

K = 5
latencies = []
hits = recalls = precs = mrrs = ndcgs = 0.0
top1 = 0
n = 0
rows = []
for t in tests:
    try:
        t0 = time.perf_counter()
        res = post(f"{CP}/rag/search", {"query": t["query"], "limit": K})
        dt = (time.perf_counter() - t0) * 1000
    except Exception as e:
        rows.append((t["query"], "ERR", str(e))); continue
    latencies.append(dt)
    results = res.get("results", [])
    rels = [1 if relevant(r.get("metadata", {}), t["expected"]) else 0 for r in results]
    n += 1
    hit = 1 if any(rels) else 0
    hits += hit
    top1 += 1 if rels and rels[0] else 0
    recalls += (sum(rels) > 0)  # proxy recall (>=1 relevant found)
    precs += (sum(rels) / len(rels)) if rels else 0
    if any(rels):
        mrrs += 1 / (rels.index(1) + 1)
    ndcgs += ndcg(rels)
    rows.append((t["query"], len(results), top1 and "", f"rel={sum(rels)}/{len(rels)} rels", f"{dt:.0f}ms"))

def pct(xs, p):
    xs = sorted(xs)
    if not xs: return 0
    k = int(round((p/100)*(len(xs)-1)))
    return xs[k]

print("\n===== RAG EVALUATION (content-pipeline /rag/search, live) =====")
print(f"queries={n}  k={K}  collection=educational_chunks (2642 vectors, 384-d bge-small)")
print(f"HitRate@{K}     = {hits/n*100:.1f}%")
print(f"Precision@{K}   = {precs/n*100:.1f}%")
print(f"MRR             = {mrrs/n:.3f}")
print(f"nDCG@{K}        = {ndcgs/n:.3f}")
print(f"Top-1 accuracy  = {top1/n*100:.1f}%")
print(f"Latency  avg={statistics.mean(latencies):.1f}ms  p50={pct(latencies,50):.1f}ms  p95={pct(latencies,95):.1f}ms  min={min(latencies):.1f}  max={max(latencies):.1f}")
print("\nPer-query (first 20):")
for r in rows[:20]:
    print("  ", r)
