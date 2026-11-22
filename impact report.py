#!/usr/bin/env python3
"""
impact_report.py - change-impact report for a branch/PR.

Works TODAY with no LLM (deterministic blast radius from git + regex).
The LLM is a pluggable add-on: flip "provider" in impact_config.json
from "none" to "openai_compat" when the new model is cleared.

Usage:
  python impact_report.py --init              # write impact_config.json
  python impact_report.py --repo FT            # diff vs the branch checked out in the FT folder
  python impact_report.py --all                # all three -> impact-reports\\<name>-impact.md
  python impact_report.py --repo UAT --head origin/feature/abc   # review a PR branch, no checkout
  python impact_report.py --gate               # before prod deploy: Hotfix commits missing from UAT
  python impact_report.py --probe X.rpt Vehicle   # is a table name searchable inside a Crystal report?
  python impact_report.py                      # current folder vs base_branch
  python impact_report.py --ping               # test the LLM endpoint
Stdlib only (no pip installs: airgap friendly). Run it from anywhere; repo folders come from impact_config.json.
"""
import argparse, functools, json, pathlib, re, subprocess, sys, time, urllib.request

DEFAULT_CFG = {
    "provider": "none",                      # "none" | "openai_compat"
    "base_url": "http://localhost:8000/v1",  # vLLM / llama.cpp / any OpenAI-compatible server
    "model": "CHANGE_ME",
    "api_key_env": "LLM_API_KEY",            # env var name; leave unset if no auth
    "timeout": 120,
    "max_diff_chars": 24000,                 # trim to the model's context window
    "base_branch": "main",
    "max_refs": 15,
    "out_dir": "impact-reports",
    "gate_against": ["@Hotfix"],   # must already be inside UAT; @Hotfix = branch checked out in the Hotfix folder
    "stale_days": 21,              # warn if the base branch tip is older than this
    "repos": {                               # name -> clone folder and the branch to diff against
        "FT":     {"path": "g:\\git-repo\\FT\\Esnaad",     "base": "@current"},
        "UAT":    {"path": "g:\\git-repo\\UAT\\Esnaad",    "base": "@current"},
        "Hotfix": {"path": "g:\\git-repo\\Hotfix\\Esnaad", "base": "@current"},
    },
}

SYSTEM_PROMPT = (
    "You are a senior reviewer for an ERP built on C#, NHibernate, SQL Server, React and Crystal Reports. "
    "Given a git diff and an automatically extracted dependency list, write: "
    "1) a 3-5 line plain-language summary of the change, "
    "2) risks (data, API contract, mapping/schema mismatch, UI), "
    "3) a checklist of what to test before merge. Be specific; do not invent files."
)

# ---------------------------------------------------------------- providers
class NullProvider:
    name = "none"
    def __init__(self, cfg): pass
    def complete(self, system, user): return None

class OpenAICompatProvider:
    name = "openai_compat"
    def __init__(self, cfg):
        self.cfg = cfg
    def complete(self, system, user):
        import os
        c = self.cfg
        body = json.dumps({
            "model": c["model"], "temperature": 0.1,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(c["api_key_env"])
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(c["base_url"].rstrip("/") + "/chat/completions",
                                     body, headers)
        with urllib.request.urlopen(req, timeout=c["timeout"]) as r:
            return json.load(r)["choices"][0]["message"]["content"]

# To add another model API later: write one class with complete(system, user) and register it.
PROVIDERS = {"none": NullProvider, "openai_compat": OpenAICompatProvider}

def get_provider(cfg):
    return PROVIDERS[cfg["provider"]](cfg)

# ---------------------------------------------------------------- git helpers
REPO = "."        # set per run; all git/file access is relative to this folder
ENV_PATHS = {}     # env name -> clone folder (filled from config)
HERE = pathlib.Path(__file__).resolve().parent
HEAD_REF = "HEAD"  # the branch under review (no checkout needed when set to origin/<branch>)

def git(*args):
    r = subprocess.run(["git", "-C", REPO, "-c", "core.quotepath=false", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else ""

def upstream(repo_path):
    r = subprocess.run(["git", "-C", repo_path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""

def resolve(ref):
    """'@current' = branch checked out in this repo folder (its origin/ copy);
       '@Hotfix'  = branch checked out in the Hotfix folder;
       'origin/X*' = newest remote branch matching the pattern."""
    if ref == "@current":
        return upstream(REPO) or "HEAD"
    if ref.startswith("@"):
        return upstream(ENV_PATHS.get(ref[1:], "")) or ref
    if "*" not in ref:
        return ref
    out = git("for-each-ref", "--sort=-committerdate", "--count=1",
              "--format=%(refname:short)", "refs/remotes/" + ref).strip()
    return out or ref

def exists(ref):
    return bool(git("rev-parse", "--verify", "--quiet", ref + "^{commit}").strip())

def fetch():
    r = subprocess.run(["git", "-C", REPO, "fetch", "--all", "--prune", "--quiet"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"WARNING: git fetch failed in {REPO}: {r.stderr.strip()[:200]}")
    return r.returncode == 0

def tip_info(ref):
    ct = git("log", "-1", "--format=%ct", ref).strip()
    if not ct.isdigit():
        return "unknown", None
    days = int((time.time() - int(ct)) / 86400)
    return f"{time.strftime('%Y-%m-%d', time.gmtime(int(ct)))}, {days}d ago", days

def changed_files(base):
    files = []
    for line in git("diff", "--name-status", f"{base}...{HEAD_REF}").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append((parts[0][0], parts[-1]))   # (status, path); renames -> new path
    return files

def read(path):
    try:
        if HEAD_REF != "HEAD":
            return git("show", f"{HEAD_REF}:{path}")
        return pathlib.Path(REPO, path).read_text(encoding="utf-8-sig", errors="ignore")
    except Exception:
        return ""

def refs(term, max_refs, exclude=()):
    """Poor man's dependency index: other files mentioning the term."""
    if HEAD_REF != "HEAD":
        out = [l.split(":", 1)[1] for l in
               git("grep", "-l", "-i", "-w", term, HEAD_REF, "--").splitlines() if ":" in l]
    else:
        out = git("grep", "-l", "-i", "-w", "--", term).splitlines()
    return [f for f in out if f not in exclude][:max_refs]

# ---------------------------------------------------------------- extraction
RULES = [
    ("mapping", re.compile(r"\.hbm\.xml$|Map\.cs$", re.I)),
    ("report", re.compile(r"\.rpt$", re.I)),   # Crystal Reports (binary)
    ("sql", re.compile(r"\.sql$", re.I)),
    ("api", re.compile(r"Controller\.cs$", re.I)),
    ("react", re.compile(r"\.(tsx|jsx|ts|js)$")),
    ("csharp", re.compile(r"\.cs$")),
]

def classify(path):
    for kind, rx in RULES:
        if rx.search(path):
            return kind
    return "other"

RX_TABLE = [
    re.compile(r'table\s*=\s*"\[?(?:\w+\]?\.\[?)?(\w+)', re.I),                 # hbm.xml
    re.compile(r'\.Table\(\s*"\[?(?:\w+\]?\.\[?)?(\w+)'),                       # Fluent NHibernate
    re.compile(r'\b(?:CREATE|ALTER|DROP)\s+TABLE\s+\[?(?:\w+\]?\.\[?)?(\w+)', re.I),
    re.compile(r'\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\[?(?:\w+\]?\.\[?)?(\w+)', re.I),
]
RX_OBJ = re.compile(r'\b(?:CREATE|ALTER|DROP)\s+(?:OR\s+ALTER\s+)?(?:PROC(?:EDURE)?|VIEW|FUNCTION)\s+\[?(?:\w+\]?\.\[?)?(\w+)', re.I)
RX_ROUTE = re.compile(r'\[(?:Route|Http(?:Get|Post|Put|Delete|Patch))\(\s*"([^"]+)"')
RX_CALL = re.compile(r'(?:fetch|axios\.\w+|api\.\w+|http\.\w+)\(\s*[`\'"]([^`\'"]+)')

def extract(kind, text):
    tables, routes = set(), set()
    if kind in ("mapping", "sql", "csharp"):
        for rx in RX_TABLE:
            tables |= {m for m in rx.findall(text) if len(m) > 2}
        tables |= {m for m in RX_OBJ.findall(text) if len(m) > 2}   # procs, views, functions
    if kind == "api":
        routes |= set(RX_ROUTE.findall(text))
    if kind == "react":
        routes |= {u.split("?")[0] for u in RX_CALL.findall(text)}
    return tables, routes

# ---------------------------------------------------------------- report
@functools.lru_cache(maxsize=None)
def _rx(name, wide):
    if wide:   # UTF-16LE text, as often stored inside .rpt files
        pat = re.escape(name.encode("utf-16le"))
        return re.compile(rb"(?<![A-Za-z0-9_]\x00)" + pat + rb"(?![A-Za-z0-9_]\x00)", re.I)
    return re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(name.encode("utf-8")) + rb"(?![A-Za-z0-9_])", re.I)

def _find(data, name):
    return bool(_rx(name, False).search(data) or _rx(name, True).search(data))

def git_bytes(*args):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True)
    return r.stdout if r.returncode == 0 else b""

def report_hits(names):
    """Which Crystal .rpt files mention each table/proc/view name (whole-word, ASCII or UTF-16)."""
    hits = {n: [] for n in names}
    if not names:
        return hits
    rpts = [f for f in git("ls-tree", "-r", "--name-only", HEAD_REF).splitlines()
            if f.lower().endswith(".rpt")]
    for f in rpts:
        data = git_bytes("show", f"{HEAD_REF}:{f}")
        for n in names:
            if _find(data, n):
                hits[n].append(f)
    return hits

def probe(path, name):
    """Check on ONE real report whether a known table/proc name is searchable."""
    data = pathlib.Path(path).read_bytes()
    print(f"{path}: {len(data)} bytes")
    print(f"  '{name}' as ASCII : {bool(_rx(name, False).search(data))}")
    print(f"  '{name}' as UTF-16: {bool(_rx(name, True).search(data))}")
    if not _find(data, name):
        print("  Not found. Either the name is not in this report, or the report stores it compressed.")

def build_findings(base, max_refs):
    files = changed_files(base)
    by_kind, tables, routes = {}, set(), set()
    for status, path in files:
        kind = classify(path)
        by_kind.setdefault(kind, []).append(f"{status} {path}")
        t, r = extract(kind, read(path) if kind != "report" else "")
        tables |= t
        routes |= r
    changed = {p for _, p in files}
    table_refs = {t: refs(t, max_refs, changed) for t in sorted(tables)}
    route_refs = {}
    for r in sorted(routes):
        leaf = r.strip("/").split("/")[-1]
        if leaf and not leaf.startswith("{"):
            route_refs[r] = refs(leaf, max_refs, changed)
    rpt_refs = report_hits(sorted(tables))
    return files, by_kind, table_refs, route_refs, rpt_refs

def render(base, files, by_kind, table_refs, route_refs, rpt_refs, llm_text, llm_note):
    L = [f"# Change impact report\n", f"Base: `{base}`  |  Files changed: {len(files)}\n"]
    L.append("## Changed files by layer")
    for kind in ("mapping", "sql", "api", "csharp", "react", "report", "other"):
        if kind in by_kind:
            L.append(f"**{kind}**")
            L += [f"- `{x}`" for x in by_kind[kind]]
    L.append("\n## Tables and DB objects touched, and other files that reference them")
    L += [f"- **{t}** -> " + (", ".join(f"`{f}`" for f in fs) or "no other references found")
          for t, fs in table_refs.items()] or ["- none detected"]
    L.append("\n## Crystal reports that reference these tables/objects")
    hit = {n: fs for n, fs in rpt_refs.items() if fs}
    L += [f"- **{n}** -> " + ", ".join(f"`{f}`" for f in fs) for n, fs in hit.items()] \
        or ["- none found (or report text is not searchable: see --probe)"]
    if hit:
        L.append("> QA: open each report in Crystal and run Database > Verify Database. "
                 "A changed column or result set fails at runtime, not at build time.")
    L.append("\n## Endpoints touched and other files that reference them")
    L += [f"- **{r}** -> " + (", ".join(f"`{f}`" for f in fs) or "no other references found")
          for r, fs in route_refs.items()] or ["- none detected"]
    if any(k in by_kind for k in ("mapping", "sql")) and not (
            "mapping" in by_kind and "sql" in by_kind):
        L.append("\n> Warning: mapping and SQL changed independently. "
                 "Confirm the schema script or the NHibernate mapping is not missing.")
    L.append("\n## AI summary")
    L.append(llm_text if llm_text else f"_Not generated: {llm_note}_")
    return "\n".join(L) + "\n"

def run_one(name, repo, base, cfg, provider, out, head=None):
    global REPO, HEAD_REF
    REPO = repo
    HEAD_REF = "HEAD"
    fetch()
    base = resolve(base)
    HEAD_REF = resolve(head) if head else "HEAD"
    if not git("rev-parse", "--is-inside-work-tree").strip():
        print(f"[{name}] not a git repo: {repo}"); return
    for label, ref in (("base", base), ("head", HEAD_REF)):
        if not exists(ref):
            print(f"[{name}] ERROR: {label} branch not found: {ref}"); return
    files, by_kind, table_refs, route_refs, rpt_refs = build_findings(base, cfg["max_refs"])
    if not files:
        print(f"[{name}] no changes: {HEAD_REF} vs {base}"); return

    llm_text, note = None, "LLM provider is 'none' (deterministic report only)"
    if provider.name != "none":
        diff = git("diff", f"{base}...{HEAD_REF}")[: cfg["max_diff_chars"]]
        facts = json.dumps({"tables": table_refs, "endpoints": route_refs, "reports": rpt_refs}, indent=1)
        try:
            llm_text = provider.complete(SYSTEM_PROMPT, f"DEPENDENCIES:\n{facts}\n\nDIFF:\n{diff}")
        except Exception as e:
            note = f"LLM call failed ({e}); deterministic report only"

    branch = HEAD_REF if HEAD_REF != "HEAD" else git("rev-parse", "--abbrev-ref", "HEAD").strip()
    age_txt, age = tip_info(base)
    text = render(f"{base} (last commit {age_txt})", files, by_kind, table_refs, route_refs, rpt_refs, llm_text, note)
    text = text.replace("# Change impact report\n", f"# Change impact report: {name} ({branch})\n", 1)
    if age is not None and age > cfg["stale_days"]:
        msg = f"base branch {base} is {age} days old. Check the {name} folder is on the current sprint branch."
        print(f"[{name}] WARNING: {msg}")
        text = text.replace("\n## Changed files by layer", f"\n> WARNING: {msg}\n\n## Changed files by layer", 1)
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out).write_text(text, encoding="utf-8")
    print(f"[{name}] wrote {out}")

def run_gate(repo, uat, against, out):
    """Release gate: commits on Hotfix/Adhoc that are NOT yet in the UAT branch."""
    global REPO, HEAD_REF
    REPO, HEAD_REF = repo, "HEAD"
    fetched = fetch()
    uat = resolve(uat)
    if not exists(uat):
        print(f"ERROR: UAT branch not found: {uat}"); return
    L = [f"# Release gate: {uat} (last commit {tip_info(uat)[0]})\n"]
    clean = True
    if not fetched:
        clean = False
        L.append("WARNING: git fetch failed. Branch data may be stale, so this result cannot be trusted.\n")
    for pat in against:
        other = resolve(pat)
        if not exists(other):
            if pat.startswith("@"):
                clean = False
                L.append(f"## {pat}\nCould not resolve this branch. Gate cannot be trusted.\n")
            else:
                L.append(f"## {pat}\nNo matching branch found (skipped).\n")
            continue
        miss = git("log", "--no-merges", "--cherry-pick", "--right-only",
                   "--format=%h  %an  %s", f"{uat}...{other}").strip()
        if miss:
            clean = False
            L.append(f"## {other}: NOT in UAT\n```\n{miss}\n```\n")
        else:
            L.append(f"## {other}: all commits are in UAT\n")
    L.append("RESULT: " + ("clear to release" if clean else "BLOCK: resolve the items above before release"))
    text = "\n".join(L) + "\n"
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out).write_text(text, encoding="utf-8")
    print(text)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "impact_config.json"))
    ap.add_argument("--repo", help="name from config (FT, UAT, Hotfix) or a folder path")
    ap.add_argument("--all", action="store_true", help="run every repo in config")
    ap.add_argument("--base", help="branch to diff against; 'origin/FT*' = newest match")
    ap.add_argument("--head", help="branch under review, e.g. origin/feature/abc (no checkout needed)")
    ap.add_argument("--probe", nargs=2, metavar=("RPT_FILE", "NAME"), help="test whether NAME is searchable inside one .rpt")
    ap.add_argument("--gate", action="store_true", help="pre-release check: Hotfix commits missing from UAT")
    ap.add_argument("--out", help="output file (single repo runs)")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--ping", action="store_true")
    a = ap.parse_args()

    if a.probe:
        probe(*a.probe); return
    cfg_path = pathlib.Path(a.config)
    if a.init:
        if not cfg_path.exists():
            cfg_path.write_text(json.dumps(DEFAULT_CFG, indent=2))
        print("Config at", cfg_path); return
    cfg = dict(DEFAULT_CFG)
    if cfg_path.exists():
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
    provider = get_provider(cfg)

    if a.ping:
        try:
            print("LLM replied:", provider.complete("Reply with OK.", "ping") or "(provider=none)")
        except Exception as e:
            print("LLM unreachable:", e); sys.exit(1)
        return

    ENV_PATHS.update({k: v["path"] for k, v in cfg["repos"].items()})
    out_dir = pathlib.Path(cfg["out_dir"])
    if not out_dir.is_absolute():
        out_dir = HERE / out_dir
    if a.gate:
        u = cfg["repos"]["UAT"]
        run_gate(u["path"], a.base or u["base"], cfg["gate_against"], a.out or str(out_dir / "release-gate.md"))
        return
    if a.all:
        targets = list(cfg["repos"].items())
    elif a.repo in cfg["repos"]:
        targets = [(a.repo, cfg["repos"][a.repo])]
    elif a.repo:
        targets = [(pathlib.Path(a.repo).parent.name or "repo", {"path": a.repo, "base": cfg["base_branch"]})]
    else:
        targets = [("local", {"path": ".", "base": cfg["base_branch"]})]

    for name, r in targets:
        out = a.out if (a.out and len(targets) == 1) else str(out_dir / f"{name}-impact.md")
        run_one(name, r["path"], a.base or r["base"], cfg, provider, out, a.head)

if __name__ == "__main__":
    main()
