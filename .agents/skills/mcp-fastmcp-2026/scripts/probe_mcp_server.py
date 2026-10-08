#!/usr/bin/env python3
"""Wire-level probe: does a Streamable HTTP MCP server really speak 2026-07-28?

Usage:
    python probe_mcp_server.py http://127.0.0.1:8000/mcp
    python probe_mcp_server.py URL --header "Authorization: Bearer TOKEN"
    python probe_mcp_server.py URL --call my_tool --args '{"x": 1}'   # optional, runs a real tool
    python probe_mcp_server.py URL --json

Stdlib only. Sends raw JSON-RPC over HTTP, exactly as a modern client would, and checks
the behaviours the 2026-07-28 spec requires of servers. It never calls tools unless you
pass --call (tools may have side effects).

Result levels:  PASS | FAIL (spec violation) | WARN (probably wrong / worth a look) | INFO
Exit code: 1 if any FAIL, else 0.

For stdio servers use `fastmcp inspect server.py` or an in-process fastmcp.Client test
instead; this tool is HTTP only.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

VERSION = "2026-07-28"
META_BASE = {
    "io.modelcontextprotocol/protocolVersion": VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "mcp-probe", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
RESULTS: list[dict] = []


def record(level: str, name: str, detail: str = ""):
    RESULTS.append({"level": level, "check": name, "detail": detail})


def snip(raw: bytes, n: int = 100) -> str:
    return " ".join(raw.decode("utf-8", "replace").split())[:n]


class Resp:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw
        self.body = self._parse()

    def _parse(self):
        ctype = (self.headers.get("content-type") or "").lower()
        text = self.raw.decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            last = None
            for block in text.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in block.split("\n") if l.startswith("data:"))
                if data:
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                        last = obj
            return last
        try:
            return json.loads(text) if text.strip() else None
        except ValueError:
            return None

    @property
    def result(self):
        return (self.body or {}).get("result") if isinstance(self.body, dict) else None

    @property
    def error(self):
        return (self.body or {}).get("error") if isinstance(self.body, dict) else None


def post(url, payload, extra_headers, timeout, *, std_headers=True, method_hdr=None, name_hdr=None,
         version_hdr=VERSION, raw_headers=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if std_headers:
        if version_hdr is not None:
            headers["MCP-Protocol-Version"] = version_hdr
        headers["Mcp-Method"] = method_hdr if method_hdr is not None else payload.get("method", "")
        if name_hdr is not None:
            headers["Mcp-Name"] = name_hdr
    headers.update(extra_headers)
    if raw_headers:
        headers.update(raw_headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Resp(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


def rpc(method, params=None, rid=1, version=VERSION):
    meta = dict(META_BASE)
    meta["io.modelcontextprotocol/protocolVersion"] = version
    p = dict(params or {})
    p["_meta"] = meta
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": p}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--header", action="append", default=[], help='extra header, e.g. "Authorization: Bearer X"')
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--call", help="optionally call this tool (side effects possible!)")
    ap.add_argument("--args", default="{}", help="JSON arguments for --call")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    extra = {}
    for h in a.header:
        k, _, v = h.partition(":")
        extra[k.strip()] = v.strip()
    url, t = a.url, a.timeout

    def modern(method, params=None, rid=1, **kw):
        name = kw.pop("name_hdr", None)
        if name is None and method in ("tools/call", "prompts/get"):
            name = (params or {}).get("name")
        if name is None and method == "resources/read":
            name = (params or {}).get("uri")
        return post(url, rpc(method, params, rid), extra, t, name_hdr=name, **kw)

    session_headers_seen = []

    def note_session(label, r):
        if "mcp-session-id" in r.headers:
            session_headers_seen.append(label)

    # 1. server/discover -------------------------------------------------------------
    try:
        r = modern("server/discover", rid="d1")
    except Exception as e:  # network error
        print(f"FAIL: cannot reach {url}: {e}", file=sys.stderr)
        return 2
    note_session("server/discover", r)
    caps = {}
    if r.status == 200 and r.result:
        res = r.result
        record("PASS", "server/discover answers", f"HTTP 200")
        sv = res.get("supportedVersions")
        if isinstance(sv, list) and VERSION in sv:
            record("PASS", "advertises 2026-07-28", f"supportedVersions={sv}")
        else:
            record("FAIL", "advertises 2026-07-28", f"supportedVersions={sv!r}")
        if res.get("resultType") == "complete":
            record("PASS", "resultType present", "complete")
        else:
            record("FAIL", "resultType present", f"got {res.get('resultType')!r}")
        info = (res.get("_meta") or {}).get("io.modelcontextprotocol/serverInfo")
        record("PASS" if info else "WARN", "serverInfo in _meta", json.dumps(info) if info else "missing (SHOULD)")
        caps = res.get("capabilities") or {}
        exts = sorted((caps.get("extensions") or {}).keys())
        record("INFO", "capabilities", f"{sorted(k for k in caps if k != 'extensions')} extensions={exts}")
        if "ttlMs" in res and res.get("cacheScope") in ("public", "private"):
            record("PASS", "discover is cacheable", f"ttlMs={res['ttlMs']} cacheScope={res['cacheScope']}")
        else:
            record("WARN", "discover is cacheable", "ttlMs/cacheScope missing or invalid")
    else:
        record("FAIL", "server/discover answers",
               f"HTTP {r.status}; error={r.error}. Server may be legacy-only (initialize-based) or the URL/auth is wrong.")

    # 2. list endpoints --------------------------------------------------------------
    def check_list(method, key, advertised):
        if not advertised:
            record("INFO", f"{method}", "capability not advertised; skipped")
            return None
        r1 = modern(method, rid=f"{method}-1")
        note_session(method, r1)
        if r1.status != 200 or not r1.result:
            record("FAIL", f"{method} works", f"HTTP {r1.status} error={r1.error}")
            return None
        res = r1.result
        record("PASS", f"{method} works", f"{len(res.get(key, []))} item(s)")
        ok_ttl = isinstance(res.get("ttlMs"), int) and res["ttlMs"] >= 0
        ok_scope = res.get("cacheScope") in ("public", "private")
        record("PASS" if ok_ttl and ok_scope else "FAIL", f"{method} has ttlMs + cacheScope (required)",
               f"ttlMs={res.get('ttlMs')!r} cacheScope={res.get('cacheScope')!r}")
        record("PASS" if res.get("resultType") == "complete" else "FAIL", f"{method} resultType",
               repr(res.get("resultType")))
        if res.get("ttlMs") == 0:
            record("INFO", f"{method} caching", "ttlMs=0 (no client caching). Set cache_ttl if the catalog is stable.")
        r2 = modern(method, rid=f"{method}-2")
        if r2.result:
            n1 = [x.get("name") or x.get("uri") or x.get("uriTemplate") for x in res.get(key, [])]
            n2 = [x.get("name") or x.get("uri") or x.get("uriTemplate") for x in r2.result.get(key, [])]
            record("PASS" if n1 == n2 else "FAIL", f"{method} deterministic order (SHOULD)",
                   "stable across two calls" if n1 == n2 else "order changed between calls")
        return res

    tools = check_list("tools/list", "tools", "tools" in caps or not caps)
    check_list("prompts/list", "prompts", "prompts" in caps)
    check_list("resources/list", "resources", "resources" in caps)
    if "resources" in caps:
        check_list("resources/templates/list", "resourceTemplates", True)

    if tools:
        names = [x.get("name") for x in tools.get("tools", [])]
        record("PASS" if len(names) == len(set(names)) else "FAIL", "tool names unique", f"{len(names)} tools")
        bad = [x.get("name") for x in tools.get("tools", [])
               if not isinstance(x.get("inputSchema"), dict) or "type" not in x["inputSchema"]
               and not any(k in x["inputSchema"] for k in ("$ref", "oneOf", "anyOf", "allOf"))]
        record("PASS" if not bad else "WARN", "tools have a usable inputSchema", f"problem tools: {bad}" if bad else "ok")

    # 3. version negotiation ---------------------------------------------------------
    r = post(url, rpc("tools/list", version="1900-01-01"), extra, t, version_hdr="1900-01-01")
    err = r.error or {}
    sup = (err.get("data") or {}).get("supported")
    if r.status == 400 and err.get("code") == -32022 and isinstance(sup, list) and sup:
        record("PASS", "unsupported version -> -32022 + supported[]", f"HTTP 400 supported={sup}")
    else:
        record("FAIL", "unsupported version -> -32022 + supported[]",
               f"HTTP {r.status} error={err or snip(r.raw)}")

    # 4. header validation -----------------------------------------------------------
    r = post(url, rpc("tools/list"), extra, t, method_hdr="tools/call")
    err = r.error or {}
    if r.status == 400 and err.get("code") == -32020:
        record("PASS", "Mcp-Method/body mismatch -> 400 -32020", "rejected")
    else:
        record("FAIL", "Mcp-Method/body mismatch -> 400 -32020",
               f"HTTP {r.status} error={err or snip(r.raw)} (header/body validation is a security MUST)")

    r = post(url, rpc("tools/list"), extra, t, version_hdr="2025-11-25")
    err = r.error or {}
    if r.status == 400:
        record("PASS", "MCP-Protocol-Version header/body mismatch rejected", f"-> {err.get('code')}")
    else:
        record("FAIL", "MCP-Protocol-Version header/body mismatch rejected", f"HTTP {r.status}")

    r = post(url, rpc("tools/list"), extra, t, std_headers=False)
    if 400 <= r.status < 500:
        record("PASS", "request without Mcp-* headers is rejected", f"HTTP {r.status}"
               + (" (dual-era server routed it to the legacy path - expected)" if "mcp-session-id" in r.headers else ""))
    elif r.status >= 500:
        record("FAIL", "request without Mcp-* headers is rejected", f"HTTP {r.status}: server error, not a clean 4xx rejection")
    else:
        record("FAIL", "request without Mcp-* headers is rejected", f"HTTP {r.status}: accepted a request lacking required Mcp-* headers")

    # 5. statelessness ---------------------------------------------------------------
    if session_headers_seen:
        record("FAIL", "no Mcp-Session-Id on modern responses", f"minted on: {sorted(set(session_headers_seen))}")
    else:
        record("PASS", "no Mcp-Session-Id on modern responses", "none minted")

    # 6. legacy / transport hygiene (informational) ----------------------------------
    init = {"jsonrpc": "2.0", "id": "i1", "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "legacy-probe", "version": "1"}}}
    r = post(url, init, extra, t, std_headers=False)
    if r.status == 200:
        pv = ((r.body or {}).get("result") or {}).get("protocolVersion")
        record("INFO", "legacy initialize", f"accepted (dual-era server); negotiated {pv}")
    else:
        record("INFO", "legacy initialize", f"HTTP {r.status}: server is modern-only; legacy clients will fail")

    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream", **extra}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=min(t, 5)) as g:
                code = g.status
        except urllib.error.HTTPError as e:
            code = e.code
        record("INFO", "GET on MCP endpoint", f"HTTP {code} (modern-only servers should answer 405; dual-era may differ)")
    except Exception as e:
        record("INFO", "GET on MCP endpoint", f"no response ({type(e).__name__})")

    r = post(url, rpc("tools/list"), {**extra, "Origin": "http://evil.example"}, t)
    if r.status == 403:
        record("PASS", "foreign Origin rejected (DNS-rebinding defense)", "HTTP 403")
    else:
        record("WARN", "foreign Origin rejected (DNS-rebinding defense)",
               f"HTTP {r.status}: spec says servers MUST validate Origin. Fine only if a trusted gateway does it; "
               "else enable it (FastMCP: http_host_origin_protection) and bind to 127.0.0.1 locally.")

    # 7. optional tool call ----------------------------------------------------------
    if a.call:
        try:
            targs = json.loads(a.args)
        except ValueError:
            print("--args must be valid JSON", file=sys.stderr)
            return 2
        r = modern("tools/call", {"name": a.call, "arguments": targs}, rid="call-1")
        note_session("tools/call", r)
        if r.status == 200 and r.result:
            rt = r.result.get("resultType")
            if rt == "complete":
                record("PASS", f"tools/call {a.call}", f"complete; isError={r.result.get('isError')}")
            elif rt == "input_required":
                keys = list((r.result.get("inputRequests") or {}).keys())
                record("PASS", f"tools/call {a.call}", f"input_required (MRTR) inputRequests={keys} "
                       f"requestState={'yes' if r.result.get('requestState') else 'no'}")
            else:
                record("FAIL", f"tools/call {a.call}", f"unexpected resultType {rt!r}")
        else:
            record("FAIL", f"tools/call {a.call}", f"HTTP {r.status} error={r.error}")

    # report -------------------------------------------------------------------------
    fails = sum(1 for x in RESULTS if x["level"] == "FAIL")
    warns = sum(1 for x in RESULTS if x["level"] == "WARN")
    if a.json:
        print(json.dumps({"url": url, "fails": fails, "warns": warns, "results": RESULTS}, indent=2))
    else:
        icon = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "INFO": "info"}
        print(f"Probe of {url} against MCP {VERSION}\n")
        for x in RESULTS:
            print(f"[{icon[x['level']]}] {x['check']}" + (f"\n        {x['detail']}" if x["detail"] else ""))
        print(f"\n{fails} failure(s), {warns} warning(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
