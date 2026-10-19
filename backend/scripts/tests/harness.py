#!/usr/bin/env python3
"""PIHUB backend test harness (standard library only).

Exercises a running FastAPI service with smoke, auth, load and stress tests and
prints/writes a JSON report. Uses http.client + threads so it runs anywhere
without pip installs.

Subcommands
-----------
  smoke   --base URL [--health /health]
  auth    --base URL [--key KEY] [--endpoint PATH]
  load    --base URL [--path /health] [--method GET] --concurrency N --duration S
  stress  --base URL [--path /health] [--levels 1,5,10,25,50] --duration S
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
from urllib.parse import urlsplit

PUBLIC_PREFIXES = (
    "/health", "/healthz", "/ready", "/readyz", "/livez", "/ping",
    "/metrics", "/docs", "/redoc", "/openapi.json", "/favicon.ico",
)


def host_port(base: str) -> tuple[str, int]:
    p = urlsplit(base)
    return p.hostname or "127.0.0.1", (p.port or (443 if p.scheme == "https" else 80))


def request(method, host, port, path, headers=None, timeout=15):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        t0 = time.perf_counter()
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        dt = (time.perf_counter() - t0) * 1000.0
        return resp.status, data, dt
    finally:
        conn.close()


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(lat, statuses, elapsed, concurrency):
    ok = sum(1 for s in statuses if 200 <= s < 400)
    err = len(statuses) - ok
    return {
        "requests": len(statuses),
        "success": ok,
        "errors": err,
        "error_rate": round(err / len(statuses), 4) if statuses else 0.0,
        "rps": round(len(statuses) / elapsed, 2) if elapsed > 0 else 0.0,
        "concurrency": concurrency,
        "elapsed_s": round(elapsed, 3),
        "latency_ms": {
            "min": round(min(lat), 2) if lat else 0.0,
            "mean": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "p50": round(pct(lat, 50), 2),
            "p90": round(pct(lat, 90), 2),
            "p95": round(pct(lat, 95), 2),
            "p99": round(pct(lat, 99), 2),
            "max": round(max(lat), 2) if lat else 0.0,
        },
        "status_counts": {str(s): statuses.count(s) for s in sorted(set(statuses))},
    }


def run_load(host, port, path, method, headers, concurrency, duration, timeout=15):
    lat: list[float] = []
    statuses: list[int] = []
    lock = threading.Lock()
    stop_at = time.perf_counter() + duration

    def worker():
        local_lat: list[float] = []
        local_st: list[int] = []
        while time.perf_counter() < stop_at:
            try:
                st, _, dt = request(method, host, port, path, headers, timeout)
            except Exception:
                st, dt = -1, 0.0
            local_st.append(st)
            local_lat.append(dt)
        with lock:
            lat.extend(local_lat)
            statuses.extend(local_st)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    return summarize(lat, statuses, elapsed, concurrency)


def get_openapi(host, port):
    try:
        st, data, _ = request("GET", host, port, "/openapi.json")
        if st == 200:
            return json.loads(data)
    except Exception:
        pass
    return {}


def is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES)


def cmd_smoke(args):
    host, port = host_port(args.base)
    out = {"service": args.name, "base": args.base, "checks": {}}
    st, data, dt = request("GET", host, port, args.health)
    body = {}
    try:
        body = json.loads(data)
    except Exception:
        body = {"raw": data[:200].decode("utf-8", "replace")}
    out["checks"]["health"] = {"status": st, "latency_ms": round(dt, 2), "body": body}
    spec = get_openapi(host, port)
    routes = sorted(spec.get("paths", {}).keys()) if spec else []
    out["openapi"] = {"reachable": bool(spec), "route_count": len(routes), "routes": routes[:60]}
    out["ok"] = st == 200
    return out


def cmd_auth(args):
    host, port = host_port(args.base)
    key = args.key or ""
    method = args.method.upper()
    spec = get_openapi(host, port)
    protected = None
    if args.endpoint:
        protected = args.endpoint
    else:
        for path, methods in (spec.get("paths") or {}).items():
            if not is_public(path) and method.lower() in methods:
                protected = path
                break
    out = {"service": args.name, "base": args.base, "method": method,
           "protected_endpoint": protected, "checks": {}}
    if not protected:
        out["ok"] = False
        out["checks"]["error"] = f"no protected {method} endpoint found in openapi"
        return out
    base_headers = {"Content-Type": "application/json"} if method != "GET" else {}
    st_no, _, _ = request(method, host, port, protected, dict(base_headers))
    out["checks"]["without_credentials"] = {"status": st_no, "rejected": st_no == 401}
    st_key = None
    if key:
        st_key, _, _ = request(method, host, port, protected, {**base_headers, "X-Internal-Service-Key": key})
        out["checks"]["with_internal_key"] = {"status": st_key, "accepted": st_key != 401}
    st_bad, _, _ = request(method, host, port, protected, {**base_headers, "X-Internal-Service-Key": "wrong-key"})
    out["checks"]["with_wrong_key"] = {"status": st_bad, "rejected": st_bad == 401}
    out["ok"] = out["checks"]["without_credentials"]["rejected"] and out["checks"]["with_wrong_key"]["rejected"]
    if st_key is not None:
        out["ok"] = out["ok"] and out["checks"]["with_internal_key"]["accepted"]
    return out


def cmd_load(args):
    host, port = host_port(args.base)
    headers = {"X-Internal-Service-Key": args.key} if args.key else {}
    res = run_load(host, port, args.path, args.method, headers, args.concurrency, args.duration)
    res.update({"service": args.name, "base": args.base, "path": args.path, "kind": "load"})
    return res


def cmd_stress(args):
    host, port = host_port(args.base)
    headers = {"X-Internal-Service-Key": args.key} if args.key else {}
    levels = [int(x) for x in args.levels.split(",")]
    results = []
    breaking = None
    for c in levels:
        r = run_load(host, port, args.path, args.method, headers, c, args.duration)
        r["concurrency"] = c
        results.append(r)
        if breaking is None and (r["error_rate"] > 0.01 or r["latency_ms"]["p95"] > args.p95_limit):
            breaking = c
    return {
        "service": args.name, "base": args.base, "path": args.path, "kind": "stress",
        "levels": results, "breaking_point_concurrency": breaking,
        "p95_limit_ms": args.p95_limit,
    }


def main():
    ap = argparse.ArgumentParser(description="PIHUB backend test harness")
    ap.add_argument("--key", default=None, help="internal service key")
    ap.add_argument("--name", default="service", help="service label")
    ap.add_argument("--out", default=None, help="write JSON report here")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("smoke"); p.add_argument("--base", required=True); p.add_argument("--health", default="/health")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("auth"); p.add_argument("--base", required=True); p.add_argument("--endpoint", default=None)
    p.add_argument("--method", default="GET")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("load"); p.add_argument("--base", required=True); p.add_argument("--path", default="/health")
    p.add_argument("--method", default="GET"); p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--duration", type=float, default=10)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("stress"); p.add_argument("--base", required=True); p.add_argument("--path", default="/health")
    p.add_argument("--method", default="GET"); p.add_argument("--levels", default="1,5,10,25,50")
    p.add_argument("--duration", type=float, default=5); p.add_argument("--p95-limit", type=float, default=2000)
    p.set_defaults(func=cmd_stress)

    args = ap.parse_args()
    result = args.func(args)
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"wrote {args.out}")
    print(text)


if __name__ == "__main__":
    main()
