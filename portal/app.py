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
import re
import subprocess
import threading
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
"""

def page(title, body):
    return (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style>"
            f"<nav><a href='/'>Create</a><a href='/catalog'>Catalog</a>"
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
            cmd = ["docker", "run", "--rm",
                   "-v", f"{dest}:/src",
                   "-v", f"{VCPKG_CACHE}:/cache/vcpkg-archives",
                   "-e", "SCCACHE_DIR=/tmp/sccache",
                   "-e", "VCPKG_DEFAULT_BINARY_CACHE=/cache/vcpkg-archives",
                   "-w", "/src", TOOLCHAIN_IMAGE,
                   "bash", "-lc", "cmake --workflow --preset ci-linux-gcc"]
            self.wfile.write((" ".join(cmd) + "\n\n").encode())
            proc = subprocess.Popen(cmd, cwd=str(XPSPLIT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                try:
                    self.wfile.write(line.encode()); self.wfile.flush()
                except BrokenPipeError:
                    proc.kill(); break
            proc.wait()
            self.wfile.write(f"\n=== docker exit {proc.returncode} ===\n".encode())
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
