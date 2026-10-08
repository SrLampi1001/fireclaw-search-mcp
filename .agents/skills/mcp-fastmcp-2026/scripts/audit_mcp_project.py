#!/usr/bin/env python3
"""Static audit: find code that targets old MCP revisions or pre-4 FastMCP.

Usage:
    python audit_mcp_project.py [PATH] [--json] [--fail-on high|medium|never]

Stdlib only. Scans *.py plus dependency manifests (pyproject.toml, requirements*.txt,
setup.cfg, Pipfile). It is a *heuristic* line scanner: every finding names the rule so you
can judge false positives. A clean run does NOT prove the server is correct -- follow it
with the wire probe (probe_mcp_server.py) and tests.

Severity:
    HIGH   breaks (ImportError/AttributeError/startup failure) on FastMCP 4 / SDK v2
    MEDIUM works only in the legacy era, silently misbehaves, or is Deprecated
    INFO   improvement or thing to double-check

Exit code: 1 if any finding at/above --fail-on (default: high), else 0.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "site-packages",
             "build", "dist", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "INFO": 2}


@dataclass
class Finding:
    severity: str
    rule: str
    file: str
    line: int
    text: str
    message: str
    fix: str


# (rule id, severity, regex, message, fix)
LINE_RULES = [
    ("R001", "HIGH", r"\bmcp\.server\.fastmcp\b",
     "Imports FastMCP 1.0 bundled in MCP Python SDK v1; module does not exist in `mcp` v2.",
     "Switch to `from fastmcp import FastMCP` (standalone, >=4) or `mcp.server.mcpserver.MCPServer`."),
    ("R002", "HIGH", r"\bctx\.sample(_step)?\s*\(|\bsampling_handler\s*=|\.list_roots\s*\(",
     "Sampling/roots server API was removed in FastMCP 4 (and both features are Deprecated in the spec).",
     "Call an LLM directly from the server, or return an InputRequiredResult carrying the request; pass paths as tool args."),
    ("R003", "HIGH", r"\bfrom\s+mcp\.shared\.exceptions\s+import\s+.*\bMcpError\b",
     "SDK v2 renamed McpError -> MCPError(code, message, data=None).",
     "`from mcp.shared.exceptions import MCPError`."),
    ("R004", "MEDIUM", r"\bctx\.elicit\s*\(",
     "ctx.elicit() is handshake-era only; it fails on modern (2026-07-28) connections.",
     "Return `InputRequiredResult` (MRTR) and read `ctx.input_responses`; see references/fastmcp-4.md section 5."),
    ("R005", "MEDIUM", r"""transport\s*=\s*["']sse["']|\bSSETransport\b|\bsse_app\s*\(|\bSseServerTransport\b|\bsse_client\b""",
     "Uses the deprecated HTTP+SSE transport (2024-11-05).",
     "Use Streamable HTTP: `mcp.run(transport=\"http\")` / StreamableHttpTransport."),
    ("R006", "MEDIUM", r"""[Mm]cp-[Ss]ession-[Ii]d|\bmcp_session_id\b""",
     "Depends on Mcp-Session-Id; protocol sessions were removed in 2026-07-28.",
     "Use explicit handles, UserSession/SessionId, or an external store. Don't route on session ids."),
    ("R007", "MEDIUM", r"""\bon_initialize\b|notifications/initialized|method\s*==\s*["']initialize["']""",
     "Relies on the initialize handshake, which does not exist in the modern era.",
     "Move per-connection setup to lifespan/per-request logic; use server/discover for capability info."),
    ("R008", "MEDIUM", r"""\.import_server\s*\(|\.as_proxy\s*\(|add_tool_transformation|from\s+fastmcp\.tools\.tool\s+import|from\s+fastmcp\.resources\.resource\s+import|from\s+fastmcp\.server\.proxy\s+import|from\s+fastmcp\.server\.openapi\s+import|\bFastMCPOpenAPI\b|fastmcp\.experimental\.server\.openapi|\bCachableToolResult\b""",
     "FastMCP 2/3 API removed or moved in 4.",
     "See the 3->4 table in references/fastmcp-4.md section 10 (mount(), create_proxy(), add_transform(), providers.*)."),
    ("R009", "MEDIUM", r"\bctx\.(set|get)_state\s*\(",
     "ctx state does not persist across requests on modern connections.",
     "Use UserSession / SessionId (with a shared session_state_store when replicated) or an explicit handle argument."),
    ("R010", "MEDIUM", r"resources/(un)?subscribe|\bsubscribe_resource\b|\bunsubscribe_resource\b",
     "resources/subscribe & unsubscribe were replaced by subscriptions/listen.",
     "Clients opt in with subscriptions/listen; servers just emit change notifications."),
    ("R011", "MEDIUM", r"""["'](2024-11-05|2025-03-26|2025-06-18|2025-11-25)["']""",
     "Hard-coded legacy MCP protocol version string.",
     "Target 2026-07-28; let the framework negotiate. If this is a deliberate legacy-compat list, ignore."),
    ("R012", "MEDIUM", r"""\bClient\(\s*["'][^"']+\.py["']""",
     "Bare-string stdio target is deprecated (removal planned in FastMCP 5).",
     "Pass `pathlib.Path('server.py')`."),
    ("R013", "MEDIUM", r"\bsse_read_timeout\b",
     "StreamableHttpTransport dropped sse_read_timeout in FastMCP 4.",
     "Pass `timeout=` to Client."),
    ("R014", "MEDIUM", r"\bexcept\s*\(?\s*httpx\.",
     "FastMCP 4 uses httpx2 internally; `except httpx.X` around FastMCP calls may silently stop matching.",
     "Catch fastmcp.exceptions.ToolError / httpx2 errors for FastMCP client calls."),
    ("R015", "MEDIUM", r"\bFastMCP\([^)]*\b(host|port)\s*=",
     "Transport settings (host/port) belong on run(), not the FastMCP constructor (v3+).",
     "`mcp.run(transport='http', host=..., port=...)`."),
    ("R016", "MEDIUM", r"\bFastMCP\([^)]*\bdependencies\s*=",
     "FastMCP(dependencies=...) was removed in v3.",
     "Use fastmcp.json configuration."),
    ("R017", "INFO", r"\.(inputSchema|outputSchema|structuredContent|isError|nextCursor|protocolVersion|serverInfo|clientInfo|listChanged|mimeType)\b",
     "camelCase attribute read on a protocol model; SDK v2 uses snake_case (bridged with a warning).",
     "Use input_schema / output_schema / structured_content / is_error / next_cursor ... ; set FASTMCP_MCP_CAMELCASE_COMPAT=false to surface all of them."),
    ("R018", "INFO", r"\bctx\.(debug|info|warning|error)\s*\(",
     "Protocol logging is Deprecated and only delivered when the request opts in via _meta logLevel.",
     "Log to stderr (stdio) / use OpenTelemetry. Keep ctx logging only as a courtesy for clients that opt in."),
    ("R019", "INFO", r"\bfrom\s+mcp\.server(\.lowlevel)?(\.server)?\s+import\s+Server\b|@\w+\.(list_tools|call_tool)\s*\(",
     "Low-level SDK server. Behavior depends on the installed `mcp` major version.",
     "Prefer FastMCP 4 unless you need protocol-level control; confirm `mcp>=2` if staying low-level."),
]

DEP_FILES = ("pyproject.toml", "setup.cfg", "Pipfile", "requirements.txt")
VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and (p.suffix == ".py" or p.name in DEP_FILES or re.match(r"requirements.*\.txt$", p.name)):
            yield p


def read_lines(p: Path):
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def major_of(spec: str):
    m = VERSION_RE.search(spec)
    return int(m.group(1)) if m else None


def audit(root: Path):
    findings: list[Finding] = []
    flavors: dict[str, int] = {"standalone fastmcp (from fastmcp import ...)": 0,
                               "SDK-bundled FastMCP 1.0 (mcp.server.fastmcp)": 0,
                               "SDK v2 MCPServer (mcp.server.mcpserver)": 0,
                               "SDK low-level Server": 0}
    compiled = [(rid, sev, re.compile(rx), msg, fix) for rid, sev, rx, msg, fix in LINE_RULES]
    tasks_ext_anywhere = False
    uses_fastmcp_ctor = False
    cache_ttl_anywhere = False
    task_true_files: list[tuple[Path, int, str]] = []
    files = list(iter_files(root))

    for f in files:
        lines = read_lines(f)
        text = "\n".join(lines)
        rel = str(f.relative_to(root)) if root.is_dir() else f.name
        is_py = f.suffix == ".py"

        if is_py:
            if re.search(r"^\s*(from|import)\s+fastmcp\b", text, re.M):
                flavors["standalone fastmcp (from fastmcp import ...)"] += 1
            if re.search(r"\bmcp\.server\.fastmcp\b", text):
                flavors["SDK-bundled FastMCP 1.0 (mcp.server.fastmcp)"] += 1
            if re.search(r"\bmcp\.server\.mcpserver\b", text):
                flavors["SDK v2 MCPServer (mcp.server.mcpserver)"] += 1
            if re.search(r"\bfrom\s+mcp\.server(\.lowlevel)?(\.server)?\s+import\s+Server\b", text):
                flavors["SDK low-level Server"] += 1
            if "TasksExtension" in text:
                tasks_ext_anywhere = True
            if re.search(r"\bFastMCP\s*\(", text):
                uses_fastmcp_ctor = True
            if "cache_ttl" in text:
                cache_ttl_anywhere = True

            for i, line in enumerate(lines, 1):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if re.search(r"\btask\s*=\s*True\b", s):
                    task_true_files.append((f, i, s))
                for rid, sev, rx, msg, fix in compiled:
                    if rx.search(line):
                        findings.append(Finding(sev, rid, rel, i, s[:160], msg, fix))
        else:
            # dependency manifests
            for i, line in enumerate(lines, 1):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.search(r"""(?<![\w-])fastmcp(?:-slim)?(\[[^\]]*\])?\s*([=<>~!]+\s*[\w.*]+(?:\s*,\s*[=<>~!]+\s*[\w.*]+)*)?""", s, re.I)
                if m and "fastmcp" in s.lower() and not re.search(r"fastmcp[\w-]*-", s.lower().replace("fastmcp-slim", "")):
                    spec = m.group(2) or ""
                    mj = major_of(spec) if spec else None
                    if not spec:
                        findings.append(Finding("INFO", "R020", rel, i, s[:160],
                                                "fastmcp is unpinned; a fresh install gets the latest 4.x but results drift.",
                                                "Pin apps to the tested version (fastmcp==4.0.x); floor libraries (>=4.0.0)."))
                    elif mj is not None and mj < 4 and "<" not in spec.split(",")[0]:
                        findings.append(Finding("MEDIUM", "R021", rel, i, s[:160],
                                                f"fastmcp pinned to major {mj}; it cannot speak MCP 2026-07-28.",
                                                "Upgrade to fastmcp>=4,<5 (work through 2->3 then 3->4 guides if coming from v2)."))
                m2 = re.search(r"""(?<![\w.-])mcp(?:\[[^\]]*\])?\s*([=<>~!]+\s*[\w.*]+(?:\s*,\s*[=<>~!]+\s*[\w.*]+)*)""", s)
                if m2 and not s.lower().startswith(("name", "description")):
                    spec = m2.group(1)
                    if re.search(r"<\s*2\b|==\s*1\.|~=\s*1\.|<=\s*1\.", spec):
                        findings.append(Finding("MEDIUM", "R022", rel, i, s[:160],
                                                "MCP Python SDK pinned to v1; v1 cannot speak 2026-07-28 (and conflicts with fastmcp>=4).",
                                                "Move to mcp>=2,<3 (or let fastmcp 4 pull it)."))

    for f, i, s in task_true_files:
        rel = str(f.relative_to(root)) if root.is_dir() else f.name
        sev = "MEDIUM" if tasks_ext_anywhere else "HIGH"
        msg = ("task=True found but no TasksExtension anywhere in the project: startup fails in FastMCP 4."
               if sev == "HIGH" else
               "task=True found; confirm TasksExtension is registered on this server (add_extension).")
        findings.append(Finding(sev, "R023", rel, i, s[:160], msg,
                                "pip install 'fastmcp[tasks]'; `from fastmcp_tasks import TasksExtension`; `mcp.add_extension(TasksExtension())`."))

    if uses_fastmcp_ctor and not cache_ttl_anywhere:
        findings.append(Finding("INFO", "R024", "(project)", 0, "FastMCP(...) without cache_ttl",
                                "No cache_ttl set anywhere: list results carry ttlMs=0, cacheScope=private (correct, but uncached).",
                                "Set FastMCP(cache_ttl=..., cache_scope='public'|'private') for catalogs that rarely change."))

    findings.sort(key=lambda x: (SEV_ORDER[x.severity], x.file, x.line))
    return findings, flavors, len(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".", help="project directory or file (default: .)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fail-on", choices=["high", "medium", "never"], default="high")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2
    findings, flavors, nfiles = audit(root)

    if args.json:
        print(json.dumps({"scanned_files": nfiles,
                          "stack": {k: v for k, v in flavors.items() if v},
                          "findings": [asdict(f) for f in findings]}, indent=2))
    else:
        print(f"Scanned {nfiles} file(s) under {root}\n")
        print("Detected stack:")
        detected = [k for k, v in flavors.items() if v]
        for k in detected or ["(no MCP framework imports found)"]:
            print(f"  - {k}")
        if len(detected) > 1:
            print("  ! Multiple flavors in one project: decide which is authoritative before editing.")
        print()
        if not findings:
            print("No findings. (Heuristic scan; still run the wire probe and tests.)")
        counts = {"HIGH": 0, "MEDIUM": 0, "INFO": 0}
        for f in findings:
            counts[f.severity] += 1
            print(f"[{f.severity}] {f.rule} {f.file}:{f.line}\n    {f.text}\n    why: {f.message}\n    fix: {f.fix}\n")
        print(f"Summary: {counts['HIGH']} high, {counts['MEDIUM']} medium, {counts['INFO']} info")

    threshold = {"high": {"HIGH"}, "medium": {"HIGH", "MEDIUM"}, "never": set()}[args.fail_on]
    return 1 if any(f.severity in threshold for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
