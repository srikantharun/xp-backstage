#!/usr/bin/env python3
"""Self-service IDP portal for xp-split (a lightweight stand-in for Backstage).

A quant fills in the same questions as quant-pricer-template.yaml, presses Create, and gets a
generated repo with platform/ pinned and the four guards run against it. The form is built
FROM the YAML (no hard-coded fields); Create shells out to the existing generate.sh unchanged.

Security: binds 127.0.0.1:7007 only; rejects non-localhost Host headers (DNS-rebinding);
state-changing actions are POST with a matching Origin; all inputs validated server-side;
subprocess uses argument lists (never shell=True) with timeouts; one create/build at a time.
Start:  python3 portal/app.py
"""
import html
import json
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

HOST, PORT = "127.0.0.1", 7007
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}

PORTAL_DIR = Path(__file__).resolve().parent
BACKSTAGE_DIR = PORTAL_DIR.parent              # /root/xp-split/xp-backstage
XPSPLIT = BACKSTAGE_DIR.parent                 # /root/xp-split
TEMPLATE_YAML = BACKSTAGE_DIR / "quant-pricer-template.yaml"
GENERATE = BACKSTAGE_DIR / "generate.sh"
PLATFORM_REPO = XPSPLIT / "xp-platform"
PRICERS_REPO = XPSPLIT / "xp-pricer-templates"
GENERATED = XPSPLIT / "generated"
RUN_GUARDS = PLATFORM_REPO / "guards" / "run_guards.sh"
TOOLCHAIN_IMAGE = "xp-toolchain:dev"
REVIEWER_IMAGE = "xp-ci-reviewer:dev"
RUN_REVIEW = XPSPLIT / "demo" / "run_review.sh"
REVIEW_OUT = XPSPLIT / ".cache" / "review"
PROFILE_BUILD = XPSPLIT / "demo" / "profile_build.sh"
PROFILES_DIR = XPSPLIT / ".cache" / "profiles"
VCPKG_CACHE = XPSPLIT / ".cache" / "vcpkg-archives"

WORK_LOCK = threading.Lock()                   # one create/build at a time

# ---------------------------------------------------------------- YAML = source of truth
def load_spec():
    """Merge spec.parameters.*.properties from the Backstage template into one dict."""
    doc = yaml.safe_load(TEMPLATE_YAML.read_text())
    props, required = {}, []
    for group in doc["spec"]["parameters"]:
        required += group.get("required", [])
        props.update(group.get("properties", {}))
    return props, required


def git_tags(repo, pattern):
    """Real tags, newest version first, straight from git."""
    r = subprocess.run(
        ["git", "-C", str(repo), "tag", "-l", pattern, "--sort=-v:refname"],
        capture_output=True, text=True, timeout=10,
    )
    return [t for t in r.stdout.split() if t]


def tag_source(field):
    """platform_ref -> xp-platform tags, pricers_ref -> xp-pricer-templates tags."""
    if field == "platform_ref":
        return git_tags(PLATFORM_REPO, "platform-v*")
    if field == "pricers_ref":
        return git_tags(PRICERS_REPO, "pricers-v*")
    return None


# ---------------------------------------------------------------- validation (server-side)
class Invalid(ValueError):
    pass


def validate(name, platform_ref, pricers_ref, methods):
    props, _ = load_spec()
    pattern = props["name"]["pattern"]
    if not re.fullmatch(pattern, name):
        raise Invalid(f"name {name!r} does not match pattern {pattern}")

    enum = props["methods"]["items"]["enum"]
    if not methods:
        raise Invalid("choose at least one method")
    if len(set(methods)) != len(methods):
        raise Invalid("duplicate method selected")
    for m in methods:
        if m not in enum:
            raise Invalid(f"method {m!r} is not in the template enum {enum}")

    ptags = git_tags(PLATFORM_REPO, "platform-v*")
    if platform_ref not in ptags:
        raise Invalid(f"platform tag {platform_ref!r} is not a real tag {ptags}")
    rtags = git_tags(PRICERS_REPO, "pricers-v*")
    if pricers_ref not in rtags:
        raise Invalid(f"pricers tag {pricers_ref!r} is not a real tag {rtags}")

    dest = GENERATED / name
    if dest.exists():
        raise Invalid(f"destination already exists: generated/{name}")
    return dest


# ---------------------------------------------------------------- subprocess helpers
def run_generate(name, platform_ref, pricers_ref, methods):
    cmd = ["bash", str(GENERATE), name, platform_ref, pricers_ref, *methods]
    r = subprocess.run(cmd, cwd=str(XPSPLIT), capture_output=True, text=True, timeout=120)
    return cmd, r.returncode, (r.stdout + r.stderr)


def run_guards(dest, platform_ref):
    cmd = ["bash", str(RUN_GUARDS), str(dest), str(PLATFORM_REPO), platform_ref]
    r = subprocess.run(cmd, cwd=str(XPSPLIT), capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr)


def repo_tree(dest, depth=2):
    r = subprocess.run(
        ["find", ".", "-maxdepth", str(depth), "-not", "-path", "./.git*", "-not", "-path", "./platform/*"],
        cwd=str(dest), capture_output=True, text=True, timeout=10,
    )
    lines = sorted(x[2:] for x in r.stdout.splitlines() if x not in (".", ""))
    n = subprocess.run(["bash", "-c", "find platform -type f | wc -l"], cwd=str(dest),
                       capture_output=True, text=True, timeout=10).stdout.strip()
    lines.append(f"platform/  (real content, {n} files)")
    return "\n".join(lines)


def answers_of(dest):
    f = dest / ".copier-answers.yml"
    if not f.exists():
        return {}
    return yaml.safe_load(f.read_text()) or {}


def toolchain_available():
    r = subprocess.run(["docker", "images", "-q", TOOLCHAIN_IMAGE],
                       capture_output=True, text=True, timeout=10)
    return bool(r.stdout.strip())


def generated_repos():
    if not GENERATED.exists():
        return []
    return sorted(p.name for p in GENERATED.iterdir() if (p / ".git").is_dir())


def repo_branches(name):
    r = subprocess.run(["git", "-C", str(GENERATED / name), "branch", "--format=%(refname:short)"],
                       capture_output=True, text=True, timeout=10)
    return [b for b in r.stdout.split() if b]


def _version_tuple(tag):
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def uses_run_step(name):
    """platform-v1.2.0+ generated repos run the CI run_step steps; older ones cmake --workflow."""
    return _version_tuple((answers_of(GENERATED / name) or {}).get("platform_ref", "")) >= (1, 2, 0)


def md_to_html(md):
    """Minimal Markdown -> HTML for the review reports (headers, tables, bold, code, bullets)."""
    out, in_table = [], False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|") and set(line.replace("|", "").strip()) <= {"-", ":", " "} and in_table:
            continue  # table separator row
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            tag = "td"
            if not in_table:
                out.append("<table>"); in_table = True; tag = "th"
            out.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>"); in_table = False
        if not line:
            continue
        if line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("- "):
            out.append(f"<div>• {_inline(line[2:])}</div>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def _inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


# ---------------------------------------------------------------- HTML
CSS = """
body{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#1a1a2e}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:1.6rem}
nav a{margin-right:1rem}label{display:block;margin:.8rem 0 .2rem;font-weight:600}
input[type=text],select{width:100%;padding:.4rem;font:inherit;box-sizing:border-box}
.methods label{display:inline-block;font-weight:400;margin-right:1rem}
pre{background:#0d1117;color:#c9d1d9;padding:.8rem;border-radius:6px;overflow-x:auto;font-size:13px}
.ok{color:#1a7f37;font-weight:600}.fail{color:#b62324;font-weight:600}
button{background:#1f6feb;color:#fff;border:0;padding:.5rem 1rem;border-radius:6px;font:inherit;cursor:pointer}
.card{border:1px solid #d0d7de;border-radius:8px;padding:.8rem 1rem;margin:.6rem 0}
.err{background:#ffebe9;border:1px solid #ff8182;padding:.6rem 1rem;border-radius:6px}
.muted{color:#656d76}
table{border-collapse:collapse;margin:.6rem 0;font-size:14px}
th,td{border:1px solid #d0d7de;padding:.3rem .6rem;text-align:left}
th{background:#f6f8fa}
.badge{display:inline-block;padding:.15rem .6rem;border-radius:999px;font-weight:600;font-size:13px}
.pass{background:#dafbe1;color:#1a7f37}.block{background:#ffebe9;color:#b62324}
.report{border:1px solid #d0d7de;border-radius:8px;padding:.2rem 1rem;margin:.6rem 0}
"""

def page(title, body):
    return (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style>"
            f"<nav><a href='/'>Create</a><a href='/catalog'>Catalog</a><a href='/review'>Review</a>"
            f"<a href='/profile'>Profile</a>"
            f"<span class=muted>xp-split self-service portal</span></nav>"
            f"{body}")


def create_page(error=None):
    props, _ = load_spec()
    name = props["name"]
    methods = props["methods"]
    enum = methods["items"]["enum"]
    mdefault = methods.get("default", [])
    rows = [f"<h1>Create a new quant version</h1>"]
    if error:
        rows.append(f"<div class=err>{html.escape(error)}</div>")
    rows.append("<form method=post action=/create id=f>")

    rows.append(f"<label>{html.escape(name.get('title','name'))}</label>")
    rows.append(f"<input type=text name=name id=name pattern='{html.escape(name['pattern'])}' "
                f"placeholder='{html.escape(name['pattern'])}' required>")

    rows.append(f"<label>{html.escape(methods.get('title','methods'))}</label><div class=methods>")
    for m in enum:
        chk = "checked" if m in mdefault else ""
        rows.append(f"<label><input type=checkbox name=method value='{html.escape(m)}' {chk}> {html.escape(m)}</label>")
    rows.append("</div>")

    for field in ("platform_ref", "pricers_ref"):
        p = props[field]
        tags = tag_source(field) or []
        default = p.get("default")
        rows.append(f"<label>{html.escape(p.get('title', field))}</label>")
        rows.append(f"<select name={field} id={field}>")
        for t in tags:
            sel = "selected" if t == default else ""
            rows.append(f"<option {sel}>{html.escape(t)}</option>")
        rows.append("</select>")

    rows.append("<h2>Preview — the exact command Create will run</h2>")
    rows.append("<pre id=preview></pre>")
    rows.append("<p><button type=submit>Create</button></p></form>")

    rows.append("""<script>
const f=document.getElementById('f'), pv=document.getElementById('preview');
function upd(){
  const name=document.getElementById('name').value||'<name>';
  const pt=document.getElementById('platform_ref').value;
  const rt=document.getElementById('pricers_ref').value;
  const ms=[...document.querySelectorAll('input[name=method]:checked')].map(x=>x.value);
  pv.textContent='generate.sh '+name+' '+pt+' '+rt+' '+(ms.join(' ')||'<pick methods>');
}
f.addEventListener('input',upd); upd();
</script>""")
    return page("Create", "".join(rows))


def result_page(name, gen_cmd, gen_rc, gen_out, guard_rc, guard_out, dest):
    build_btn = ""
    if toolchain_available():
        build_btn = (f"<form method=post action=/build style='display:inline'>"
                     f"<input type=hidden name=name value='{html.escape(name)}'>"
                     f"<button type=submit>Build on RHEL 8 (ci-linux-gcc)</button></form>")
    else:
        build_btn = "<p class=muted>Build on RHEL 8 disabled: image xp-toolchain:dev not available.</p>"
    overall = "<span class=ok>ALL GUARDS PASS</span>" if guard_rc == 0 else "<span class=fail>GUARDS FAILED</span>"
    gstat = "ok" if gen_rc == 0 else "fail"
    return page("Result", f"""
<h1>Generated <code>{html.escape(name)}</code></h1>
<p>generate.sh exit <span class={gstat}>{gen_rc}</span> &nbsp; guards: {overall}</p>
<h2>Command</h2><pre>{html.escape(' '.join(gen_cmd))}</pre>
<h2>Generator output</h2><pre>{html.escape(gen_out.strip())}</pre>
<h2>Generated tree (2 levels)</h2><pre>{html.escape(repo_tree(dest))}</pre>
<h2>.copier-answers.yml</h2><pre>{html.escape((dest/'.copier-answers.yml').read_text())}</pre>
<h2>Guards (run from the pinned platform tag)</h2><pre>{html.escape(guard_out.strip())}</pre>
<h2>Build</h2>{build_btn}
<p><a href='/catalog'>Go to catalog</a></p>
""")


def catalog_page():
    rows = ["<h1>Catalog — generated repos</h1>"]
    repos = sorted(p for p in GENERATED.iterdir() if p.is_dir()) if GENERATED.exists() else []
    if not repos:
        rows.append("<p class=muted>No generated repos yet.</p>")
    for dest in repos:
        a = answers_of(dest)
        pref = a.get("platform_ref", "?")
        rc, _ = run_guards(dest, pref) if (dest / "CMakeLists.txt").exists() else (1, "")
        gstat = "<span class=ok>guards PASS</span>" if rc == 0 else "<span class=fail>guards FAIL</span>"
        methods = a.get("methods", [])
        rows.append(f"""<div class=card>
<b>{html.escape(a.get('project_name', dest.name))}</b> &nbsp; {gstat}<br>
<span class=muted>platform:</span> {html.escape(str(pref))} &nbsp;
<span class=muted>pricers:</span> {html.escape(str(a.get('pricers_ref','?')))}<br>
<span class=muted>methods:</span> {html.escape(', '.join(methods) if isinstance(methods,list) else str(methods))}
</div>""")
    return page("Catalog", "".join(rows))


def review_page(error=None):
    repos = generated_repos()
    branchmap = {r: repo_branches(r) for r in repos}
    rows = ["<h1>Review an MR branch</h1>",
            "<p class=muted>Runs <code>review.py</code> (guards + semgrep, exit 87) and "
            "<code>testreview.py</code> (tests, exit 88) from a clone of xp-platform at the repo's "
            "pinned tag, diffing the branch against <code>master</code> — exactly as the CI "
            "component does.</p>"]
    if error:
        rows.append(f"<div class=err>{html.escape(error)}</div>")
    if not repos:
        return page("Review", "".join(rows) + "<p class=muted>No generated repos yet.</p>")
    rows.append("<form method=post action=/review id=rf>")
    rows.append("<label>Repository</label><select name=repo id=repo>")
    for r in repos:
        rows.append(f"<option>{html.escape(r)}</option>")
    rows.append("</select>")
    rows.append("<label>Branch</label><select name=branch id=branch></select>")
    rows.append("<p><button type=submit>Run review + testreview</button></p></form>")
    import json as _json
    rows.append(f"<script>const BR={_json.dumps(branchmap)};"
                "const rs=document.getElementById('repo'),bs=document.getElementById('branch');"
                "function fill(){bs.innerHTML='';(BR[rs.value]||[]).forEach(b=>{"
                "const o=document.createElement('option');o.textContent=b;bs.appendChild(o);});}"
                "rs.addEventListener('change',fill);fill();</script>")
    return page("Review", "".join(rows))


def _badge(exit_code, kind):
    if exit_code == 0:
        return f"<span class='badge pass'>{kind}: PASS</span>"
    return f"<span class='badge block'>{kind}: BLOCKED (exit {exit_code})</span>"


def review_result_page(repo, branch, rexit, texit, rmd, tmd):
    return page("Review result", f"""
<h1>Review of <code>{html.escape(repo)}</code> · branch <code>{html.escape(branch)}</code></h1>
<p>{_badge(rexit, 'review')} &nbsp; {_badge(texit, 'testreview')}</p>
<div class=report>{md_to_html(rmd)}</div>
<div class=report>{md_to_html(tmd)}</div>
<p><a href='/review'>← back to Review</a></p>
""")


# ---------------------------------------------------------------- Profile (build statistics)
def list_profiles():
    """All stored profiles, newest first."""
    if not PROFILES_DIR.exists():
        return []
    out = []
    for d in sorted(PROFILES_DIR.iterdir(), reverse=True):
        f = d / "stats.json"
        if f.is_file():
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                pass
    return out


def read_profile(pid):
    f = PROFILES_DIR / pid / "stats.json"
    return json.loads(f.read_text()) if f.is_file() else None


def profile_page(error=None):
    repos = generated_repos()
    rows = ["<h1>Profile a build</h1>",
            "<p class=muted>Runs a full build in <code>xp-toolchain:dev</code>, assigns a "
            "<b>profile-id</b>, and stores build statistics + a Perfetto-openable trace you can "
            "query later by id (like a BuildBuddy invocation).</p>"]
    if error:
        rows.append(f"<div class=err>{html.escape(error)}</div>")
    if repos:
        rows.append("<form method=post action=/profile><label>Repository</label><select name=repo>")
        for r in repos:
            rows.append(f"<option>{html.escape(r)}</option>")
        rows.append("</select><p><button type=submit>Profile build</button> "
                    "<span class=muted>(full compile — ~20s)</span></p></form>")
    rows.append("<h2>Recent profiles</h2>")
    profs = list_profiles()
    if not profs:
        rows.append("<p class=muted>None yet.</p>")
    else:
        rows.append("<table><tr><th>profile-id</th><th>repo</th><th>build</th><th>targets</th><th>tests</th></tr>")
        for s in profs:
            rows.append(f"<tr><td><a href='/profiles?id={html.escape(s['profile_id'])}'>{html.escape(s['profile_id'])}</a></td>"
                        f"<td>{html.escape(str(s.get('repo','')))}</td>"
                        f"<td>{s.get('build_wall_s','?')}s</td>"
                        f"<td>{s.get('num_targets','?')}</td>"
                        f"<td>{html.escape(str(s.get('tests','') or ''))}</td></tr>")
        rows.append("</table>")
    return page("Profile", "".join(rows))


def profile_detail_page(stats):
    pid = stats["profile_id"]
    slow = "".join(
        f"<tr><td>{s['ms']} ms</td><td><code>{html.escape(s['target'])}</code></td></tr>"
        for s in stats.get("slowest_targets", []))
    gf, portable = stats.get("glibc_floor"), stats.get("portable")
    if gf and portable:
        port_cell = f"<span class='badge pass'>runs on RHEL 8</span> &nbsp;max required {html.escape(gf)} ≤ glibc 2.28"
    elif gf:
        port_cell = f"<span class='badge block'>would FAIL on RHEL 8</span> &nbsp;needs {html.escape(gf)} &gt; 2.28 (wrong sysroot)"
    else:
        port_cell = "(n/a)"
    sug = "".join(f"<li>{html.escape(s)}</li>" for s in stats.get("suggestions", [])) or "<li class=muted>none</li>"
    return page("Profile result", f"""
<h1>Profile <code>{html.escape(pid)}</code></h1>
<p class=muted>repo {html.escape(str(stats.get('repo','')))} · preset {html.escape(str(stats.get('preset','')))}</p>
<table>
<tr><th>restored packages</th><td>{stats.get('restored_packages','?')} (from warm cache)</td></tr>
<tr><th>configure</th><td>{stats.get('configure_s','?')} s</td></tr>
<tr><th>vcpkg restore</th><td>{stats.get('vcpkg_restore_s','?')} s</td></tr>
<tr><th>build wall</th><td>{stats.get('build_wall_s','?')} s</td></tr>
<tr><th>targets built</th><td>{stats.get('num_targets','?')}</td></tr>
<tr><th>tests</th><td>{html.escape(str(stats.get('tests','') or ''))}</td></tr>
<tr><th>portability (glibc floor)</th><td>{port_cell}</td></tr>
</table>
<h2>Suggestions</h2>
<ul>{sug}</ul>
<h2>Slowest targets</h2>
<table><tr><th>time</th><th>target</th></tr>{slow}</table>
<h2>Trace</h2>
<p><a href='/trace?id={html.escape(pid)}'>⬇ download build-trace.json</a> — open it at
<code>https://ui.perfetto.dev</code> (or <code>chrome://tracing</code>) to see the build timeline.</p>
<p><a href='/profile'>← back to Profile</a></p>
""")


# ---------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    server_version = "xp-portal/1.0"

    def _host_ok(self):
        host = self.headers.get("Host", "")
        if host not in ALLOWED_HOSTS:
            self._send(403, page("Forbidden",
                       f"<div class=err>Refused: Host header {html.escape(host)!r} is not "
                       f"localhost:{PORT} (DNS-rebinding protection).</div>"))
            return False
        return True

    def _origin_ok(self):
        origin = self.headers.get("Origin", "")
        if origin not in ALLOWED_ORIGINS:
            self._send(403, page("Forbidden",
                       f"<div class=err>Refused: Origin {html.escape(origin)!r} not allowed "
                       f"for a state-changing request.</div>"))
            return False
        return True

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode()
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def do_GET(self):
        if not self._host_ok():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, create_page())
        elif path == "/catalog":
            self._send(200, catalog_page())
        elif path == "/review":
            self._send(200, review_page())
        elif path == "/profile":
            self._send(200, profile_page())
        elif path == "/profiles":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (qs.get("id", [""])[0])
            stats = read_profile(pid) if pid else None
            self._send(200, profile_detail_page(stats) if stats else profile_page(error=f"unknown profile {pid!r}"))
        elif path == "/trace":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = qs.get("id", [""])[0]
            f = PROFILES_DIR / pid / "build-trace.json"
            if re.fullmatch(r"prof-[0-9A-Za-z\-]+", pid or "") and f.is_file():
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f'attachment; filename="{pid}-trace.json"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, page("Not found", "<p>No such trace.</p>"))
        else:
            self._send(404, page("Not found", "<p>Not found.</p>"))

    def do_POST(self):
        if not self._host_ok():
            return
        if not self._origin_ok():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/create":
            self._create()
        elif path == "/build":
            self._build()
        elif path == "/review":
            self._review()
        elif path == "/profile":
            self._profile()
        else:
            self._send(404, page("Not found", "<p>Not found.</p>"))

    def _create(self):
        form = self._form()
        name = (form.get("name", [""])[0]).strip()
        platform_ref = (form.get("platform_ref", [""])[0]).strip()
        pricers_ref = (form.get("pricers_ref", [""])[0]).strip()
        methods = [m.strip() for m in form.get("method", [])]
        try:
            dest = validate(name, platform_ref, pricers_ref, methods)
        except Invalid as e:
            self._send(400, create_page(error=str(e)))
            return
        if not WORK_LOCK.acquire(blocking=False):
            self._send(409, create_page(error="busy: another create/build is in progress"))
            return
        try:
            cmd, grc, gout = run_generate(name, platform_ref, pricers_ref, methods)
            if grc != 0:
                self._send(500, create_page(error=f"generate.sh failed (exit {grc}):\n{gout}"))
                return
            qrc, qout = run_guards(dest, platform_ref)
            self._send(200, result_page(name, cmd, grc, gout, qrc, qout, dest))
        finally:
            WORK_LOCK.release()

    def _build(self):
        form = self._form()
        name = (form.get("name", [""])[0]).strip()
        dest = GENERATED / name
        if not re.fullmatch(r"quant-pricer[0-9]+", name) or not dest.is_dir():
            self._send(400, page("Build", "<div class=err>unknown repo</div>"))
            return
        if not toolchain_available():
            self._send(400, page("Build", "<div class=err>toolchain image unavailable</div>"))
            return
        if not WORK_LOCK.acquire(blocking=False):
            self._send(409, page("Build", "<div class=err>busy</div>"))
            return
        # stream the build log (connection-close delimited)
        self.protocol_version = "HTTP/1.0"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        try:
            if uses_run_step(name):
                # platform-v1.2.0+: the exact four CI job steps, from the repo root.
                inner = ("set -e; for s in build test-quarantined test budget; do "
                         "echo \"##### run_step $s #####\"; "
                         "python3 platform/tools/ci/run_step.py $s --preset ci-linux-gcc; done")
                cmd = ["docker", "run", "--rm",
                       "-v", f"{dest}:/src",
                       "-v", f"{VCPKG_CACHE}:/cache/vcpkg-archives",
                       "-e", "SCCACHE_DIR=/tmp/sccache",
                       "-e", "VCPKG_BINARY_SOURCES=clear;files,/cache/vcpkg-archives,readwrite",
                       "-w", "/src", TOOLCHAIN_IMAGE, "bash", "-lc", inner]
            else:
                cmd = ["docker", "run", "--rm",
                       "-v", f"{dest}:/src",
                       "-v", f"{VCPKG_CACHE}:/cache/vcpkg-archives",
                       "-e", "SCCACHE_DIR=/tmp/sccache",
                       "-e", "VCPKG_DEFAULT_BINARY_CACHE=/cache/vcpkg-archives",
                       "-w", "/src", TOOLCHAIN_IMAGE, "bash", "-lc",
                       "cmake --workflow --preset ci-linux-gcc"]
            self.wfile.write((" ".join(cmd) + "\n\n").encode())
            proc = subprocess.Popen(cmd, cwd=str(XPSPLIT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            restored = tests = quarantined = budget = ""
            for line in proc.stdout:
                try:
                    self.wfile.write(line.encode()); self.wfile.flush()
                except BrokenPipeError:
                    proc.kill(); break
                if "Restored" in line and "package" in line:
                    restored = line.strip()
                elif "tests passed" in line:
                    tests = line.strip()
                elif "quarantined tests:" in line:
                    quarantined = line.strip()
                elif "within budget" in line or "build budget" in line:
                    budget = line.strip()
            proc.wait()
            summary = (f"\n===== summary =====\n"
                       f"{restored or 'cache: (n/a)'}\n"
                       f"tests: {tests or '(n/a)'}\n"
                       f"quarantined: {quarantined or '(none)'}\n"
                       f"budget: {budget or '(n/a)'}\n"
                       f"docker exit {proc.returncode}\n")
            self.wfile.write(summary.encode())
        finally:
            WORK_LOCK.release()

    def _review(self):
        form = self._form()
        repo = (form.get("repo", [""])[0]).strip()
        branch = (form.get("branch", [""])[0]).strip()
        if repo not in generated_repos():
            self._send(400, review_page(error=f"unknown repo {repo!r}"))
            return
        if branch not in repo_branches(repo):
            self._send(400, review_page(error=f"unknown branch {branch!r}"))
            return
        if not WORK_LOCK.acquire(blocking=False):
            self._send(409, review_page(error="busy: another create/build/review is running"))
            return
        try:
            subprocess.run(["bash", str(RUN_REVIEW), repo, branch],
                           cwd=str(XPSPLIT), capture_output=True, text=True, timeout=180)
            out = REVIEW_OUT / f"{repo}__{branch}"
            exits = (out / "exits.txt").read_text() if (out / "exits.txt").exists() else ""
            rexit = int(re.search(r"REVIEW_EXIT=(\d+)", exits).group(1)) if "REVIEW_EXIT" in exits else -1
            texit = int(re.search(r"TESTREVIEW_EXIT=(\d+)", exits).group(1)) if "TESTREVIEW_EXIT" in exits else -1
            rmd = (out / "review-report.md").read_text() if (out / "review-report.md").exists() else "# review: (no report)"
            tmd = (out / "testreview-report.md").read_text() if (out / "testreview-report.md").exists() else "# testreview: (no report)"
            self._send(200, review_result_page(repo, branch, rexit, texit, rmd, tmd))
        finally:
            WORK_LOCK.release()

    def _profile(self):
        form = self._form()
        repo = (form.get("repo", [""])[0]).strip()
        if repo not in generated_repos():
            self._send(400, profile_page(error=f"unknown repo {repo!r}"))
            return
        if not toolchain_available():
            self._send(400, profile_page(error="toolchain image unavailable"))
            return
        if not WORK_LOCK.acquire(blocking=False):
            self._send(409, profile_page(error="busy: another build/review/profile is running"))
            return
        try:
            pid = "prof-" + time.strftime("%Y%m%d-%H%M%S")
            subprocess.run(["bash", str(PROFILE_BUILD), repo, pid],
                           cwd=str(XPSPLIT), capture_output=True, text=True, timeout=300)
            stats = read_profile(pid)
            if stats:
                self._send(200, profile_detail_page(stats))
            else:
                self._send(500, profile_page(error=f"profile {pid} produced no stats (see .cache/profiles/{pid}/build.log)"))
        finally:
            WORK_LOCK.release()

    def log_message(self, fmt, *args):
        print("[portal]", self.address_string(), fmt % args)


def main():
    GENERATED.mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"xp-split portal on http://{HOST}:{PORT}  (localhost only; tunnel in via SSH)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
