#!/usr/bin/env python3
"""Make a site's Cloudflare setup match its site-setup.json.

    python3 ~/code/site-kit/setup/site_setup.py            in the site's folder: show what is set up and what would change
    python3 ~/code/site-kit/setup/site_setup.py --apply    make the changes

Nothing is changed without --apply. Run it again whenever you like: it only changes what is missing or different.
Secrets are never read or written: it only reports which ones are missing and how to add them.

What it covers (README, "Setup script"):
1. Pages project: it exists; for a project Cloudflare builds from GitHub, the build watch paths and which branches
   get preview builds; R2 bindings; that the secrets the site needs are set.
2. Address: the custom domain on the project, and the zone file's record pointing it at the project's pages.dev name.
3. The pages.dev address redirects (301, path kept) to the site's own domain: a Bulk Redirect.
4. Admin login (sites with an admin): the Access application on <host>/<path>, its policy "Admin" = the admins file,
   and the application's IDs written into the site's config file.
5. Email forwarding (optional): Email Routing, an Email Worker that forwards one address to everyone in a file.

The account comes from the zone file's `account` line; the token and API helper from ~/code/domains/cloudflare-dns.py
(CLOUDFLARE_API_TOKEN, else asked for, input hidden; its permissions: that folder's README, "Cloudflare API token").
"""
import argparse, hashlib, importlib.util, json, os, re, sys, time, urllib.error, urllib.request, uuid

DOMAINS = os.path.expanduser("~/code/domains")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

SETTINGS = "site-setup.json"

# What a type of site needs unless its site-setup.json says otherwise (workspace README, "Site types").
TYPES = {
    "private-archive": {          # family-tree: static files behind one shared password, built by Cloudflare from GitHub
        "build": "cloudflare-git",
        "pages_dev_redirect": {"subdomains": False},   # staging.<project>.pages.dev stays reachable
        "secrets": {"production": ["SITE_USER", "SITE_PASSWORD"], "preview": ["SITE_USER", "SITE_PASSWORD"]},
    },
    "public-with-admin": {        # bavarianspringboks: public pages, /admin behind Access, built by GitHub Actions
        "build": "github-actions",
        "pages_dev_redirect": {"subdomains": True},    # so /admin is only ever served where Access covers it
        "secrets": {"production": ["GITHUB_TOKEN"]},
        "access": {"path": "admin", "policy": "Admin", "admins_file": "admins.txt", "config_file": "src/config.mjs",
                   "session_duration": "24h"},
    },
}

WORKER_CODE = """// Forwards a message to every address in FORWARD_TO (comma-separated). Made by site-kit/setup/site_setup.py.
export default {
  async email(message, env) {
    const to = env.FORWARD_TO.split(',').map((a) => a.trim()).filter(Boolean);
    // One address failing (e.g. not confirmed yet) mustn't stop the others getting the message.
    await Promise.allSettled(to.map((a) => message.forward(a)));
  },
};
"""
CODE_VERSION = hashlib.sha256(WORKER_CODE.encode()).hexdigest()[:12]   # stored with the Worker, so a code change redeploys
MAIL_RE = re.compile(r"# >>> mail \(written by [^)\n]*\)\n.*?# <<< mail", re.S)
MAIL_START, MAIL_END = "# >>> mail (written by site-kit/setup/site_setup.py)", "# <<< mail"


# ── Settings ─────────────────────────────────────────────────────────────────────────────────────────────────────

def load_settings(path):
    """site-setup.json merged over its type's defaults; paths made absolute (relative to the settings file)."""
    raw = json.loads(read(path))
    if raw.get("type") not in TYPES:
        sys.exit(f"{path}: \"type\" must be one of {', '.join(TYPES)}")
    s = json.loads(json.dumps(TYPES[raw["type"]]))
    for k, v in raw.items():
        s[k] = {**s[k], **v} if isinstance(v, dict) and isinstance(s.get(k), dict) else v
    for k in ("project", "host", "zone_file"):
        if not s.get(k):
            sys.exit(f"{path}: \"{k}\" is missing")
    root = os.path.dirname(os.path.abspath(path))
    here = lambda p: os.path.join(root, os.path.expanduser(p))
    s["root"], s["zone_file"] = root, here(s["zone_file"])
    if s.get("access"):
        s["access"]["admins_file"] = here(s["access"]["admins_file"])
        s["access"]["config_file"] = here(s["access"]["config_file"])
        s["access"].setdefault("name", f"{s['project']} admin")
    if s.get("email_forward"):
        s["email_forward"]["forward_file"] = here(s["email_forward"]["forward_file"])
    return s


def addresses(path):
    """Email addresses, one per line (# comments allowed), lowercased."""
    if not os.path.isfile(path):
        sys.exit(f"Missing {path}: one email address per line.")
    out = []
    for line in read(path).splitlines():
        line = line.split("#")[0].strip()
        if line:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", line):
                sys.exit(f"Not an email address in {path}: {line!r}")
            out.append(line.lower())
    if not out:
        sys.exit(f"No addresses in {path}.")
    return out


# ── Zone file ────────────────────────────────────────────────────────────────────────────────────────────────────

def zone_of(host, zone_file):
    """(zone, label): the zone file's domain that host is in, and host's name inside it ("" for the domain itself)."""
    from zonefile import parse
    for d in sorted(parse(zone_file)[0], key=len, reverse=True):
        if host == d or host.endswith("." + d):
            return d, host[: -len(d) - 1] if host != d else ""
    sys.exit(f"{host} is in none of the domains on {zone_file}'s `domains` line")


def cname_target(zone_file, zone, label):
    from zonefile import parse
    return next((r["content"] for r in parse(zone_file)[1].get(zone, []) if r["type"] == "CNAME" and r["sub"] == label), None)


def write_cname_target(zone_file, zone, label, value):
    """Point the CNAME for label at value: in the [zone] section if there is one, else the first such line."""
    text = read(zone_file)
    pat = re.compile(r"^(CNAME\s+" + re.escape(label or "@") + r"\s+)\S+", re.M)
    sec = re.search(r"^\[" + re.escape(zone) + r"\]\s*$", text, re.M)
    start = sec.end() if sec else 0
    nxt = re.search(r"^\[", text[start:], re.M) if sec else None
    end = start + nxt.start() if nxt else len(text)
    m = pat.search(text, start, end) or pat.search(text)
    if not m:
        raise RuntimeError(f"no `CNAME {label or '@'} …` line in {zone_file}: add `CNAME {label or '@'} {value} proxied`")
    write(zone_file, text[: m.start()] + m.group(1) + value + text[m.end():])


def zone_keys(zone_file, zone):
    from zonefile import parse
    return {(r["type"], r["sub"], r["prio"], r["content"]) for r in parse(zone_file)[1].get(zone, [])}


def write_mail_records(zone_file, lines):
    """Email Routing's records into the zone file, between the mail markers (the old 'no mail' SPF line goes)."""
    text = read(zone_file)
    block = "\n".join([MAIL_START, *lines, MAIL_END])
    if MAIL_RE.search(text):
        text = MAIL_RE.sub(lambda m: block, text, count=1)
    else:
        new = re.sub(r'^TXT\s+@\s+"v=spf1 -all"\s*$', lambda m: block, text, count=1, flags=re.M)
        if new == text:
            raise RuntimeError(f'{zone_file} has neither the mail markers nor the line TXT @ "v=spf1 -all": add the records by hand')
        text = new
    write(zone_file, text)


# ── Config file (the site's own settings in code) ────────────────────────────────────────────────────────────────

def config_value(path, key):
    m = re.search(key + r":\s*'([^']*)'", read(path))
    return m and m.group(1)


def write_config_value(path, key, value):
    text = read(path)
    new, n = re.subn(r"(" + key + r":\s*')[^']*(')", lambda m: m.group(1) + value + m.group(2), text, count=1)
    if not n:
        raise RuntimeError(f"no `{key}: '…'` in {path}")
    write(path, new)


# ── The steps ────────────────────────────────────────────────────────────────────────────────────────────────────

class Run:
    """Prints each check as OK / TODO (preview) / DO (with --apply) and collects what's left."""

    def __init__(self, apply):
        self.apply, self.todo, self.failed, self.notes = apply, [], [], []

    def step(self, done, text, action=None):
        print(f"  {'OK  ' if done else ('DO  ' if self.apply and action else 'TODO')} {text}")
        if done:
            return
        if self.apply and action:
            try:
                action()
                return
            except (RuntimeError, SystemExit) as e:   # an API error exits; the other steps report their own state
                print(f"       FAILED: {e}")
                self.failed.append(text)
                return
        self.todo.append(text)

    def note(self, text):
        print(f"  NOTE {text}")
        self.notes.append(text)


def check_project(cf, s, run):
    acct = f"/accounts/{cf.account}"
    print("1. Pages project")
    res = cf.call("GET", f"{acct}/pages/projects/{s['project']}", missing_ok=True)
    if not res:
        how = ("Workers & Pages → Create → Pages → Connect to Git" if s["build"] == "cloudflare-git"
               else "the site's Deploy workflow creates it: GitHub → Actions → Deploy → Run workflow")
        run.step(False, f"Pages project {s['project']} exists ({how})")
        return None
    project = res["result"]
    run.step(True, f"Pages project {s['project']} ({project['subdomain']})")

    source = (project.get("source") or {}).get("config") or {}
    if s["build"] == "cloudflare-git":
        if not source:
            run.note("the project isn't connected to GitHub, so build settings were skipped")
        else:
            want = {}
            if "watch_paths" in s and sorted(source.get("path_includes") or ["*"]) != sorted(s["watch_paths"]):
                want["path_includes"] = s["watch_paths"]
            if "preview_branches" in s and (source.get("preview_deployment_setting") != "custom"
                                            or sorted(source.get("preview_branch_includes") or []) != sorted(s["preview_branches"])):
                want.update(preview_deployment_setting="custom", preview_branch_includes=s["preview_branches"])
            label = []
            if "watch_paths" in s:
                label.append(f"build watch paths: {', '.join(s['watch_paths'])}")
            if "preview_branches" in s:
                label.append(f"preview builds only for {', '.join(s['preview_branches'])}")
            if label:
                # PATCH with the whole source config, so nothing else in it (repository, branch) is lost.
                run.step(not want, "; ".join(label), lambda: cf.call(
                    "PATCH", f"{acct}/pages/projects/{s['project']}",
                    {"source": {**project["source"], "config": {**source, **want}}}))

    envs = project.get("deployment_configs") or {}
    for name, bucket in (s.get("r2") or {}).items():
        for env in ("production", "preview"):
            have = ((envs.get(env) or {}).get("r2_buckets") or {}).get(name, {}).get("name")
            run.step(have == bucket, f"R2 bucket {bucket} bound as {name} ({env})", lambda env=env, name=name, bucket=bucket: cf.call(
                "PATCH", f"{acct}/pages/projects/{s['project']}",
                {"deployment_configs": {env: {"r2_buckets": {name: {"name": bucket}}}}}))

    for env, names in (s.get("secrets") or {}).items():
        have = (envs.get(env) or {}).get("env_vars") or {}
        for n in names:
            ok = (have.get(n) or {}).get("type") == "secret_text"
            run.step(ok, f"secret {n} is set ({env})" + ("" if ok else
                     f": Workers & Pages → {s['project']} → Settings → Variables and secrets → {env.title()} → Add, type Secret"))
    return project


def check_address(cf, s, project, run):
    acct = f"/accounts/{cf.account}"
    print("\n2. Address")
    domains = [d["name"] for d in cf.call("GET", f"{acct}/pages/projects/{s['project']}/domains")["result"]]
    run.step(s["host"] in domains, f"{s['host']} is a custom domain of {s['project']}",
             lambda: cf.call("POST", f"{acct}/pages/projects/{s['project']}/domains", {"name": s["host"]}))
    zone, label = zone_of(s["host"], s["zone_file"])
    now = cname_target(s["zone_file"], zone, label)
    target = project["subdomain"]
    rel = os.path.relpath(s["zone_file"], s["root"])

    def write():
        write_cname_target(s["zone_file"], zone, label, target)
        run.notes.append(f"{rel} changed: ~/code/domains/cloudflare-dns.py --file {s['zone_file']} (then --apply), and commit it")
    run.step(now == target, f"{rel} points {label or '@'}.{zone} at {target}" + ("" if now in (None, target) else f" (now {now})"), write)


def bulk_redirect(cf, list_name, item, run):
    """One Bulk Redirect list holding item, and a rule in the account's redirect phase that uses it."""
    acct = f"/accounts/{cf.account}"
    lists = cf.call("GET", f"{acct}/rules/lists", missing_ok=True)
    if lists is None:
        run.step(False, "the token can read redirect lists (add Account → Account Filter Lists → Edit and Bulk URL Redirects → Edit)")
        return
    lst = next((l for l in lists["result"] if l.get("name") == list_name), None)
    items = [i["redirect"] for i in cf.call("GET", f"{acct}/rules/lists/{lst['id']}/items")["result"]] if lst else []
    entry = cf.call("GET", f"{acct}/rulesets/phases/http_request_redirect/entrypoint", missing_ok=True)
    rules = entry["result"].get("rules", []) if entry else []
    same = len(items) == 1 and all(items[0].get(k) == v for k, v in item.items())
    on = any(list_name in r.get("expression", "") and r.get("enabled", True) for r in rules)

    def apply():
        nonlocal lst
        if not lst:
            lst = cf.call("POST", f"{acct}/rules/lists", {"name": list_name, "kind": "redirect",
                                                          "description": f"{item['source_url']} → {item['target_url']}"})["result"]
        op = cf.call("PUT", f"{acct}/rules/lists/{lst['id']}/items", [{"redirect": item}])["result"]
        status = {}
        for _ in range(30):   # the item change is asynchronous
            status = cf.call("GET", f"{acct}/rules/lists/bulk_operations/{op['operation_id']}")["result"]
            if status.get("status") in ("completed", "failed"):
                break
            time.sleep(1)
        if status.get("status") != "completed":
            raise RuntimeError(f"updating the redirect list didn't finish: {status}")
        rule = {"expression": f"http.request.full_uri in ${list_name}", "description": lst.get("description", list_name),
                "action": "redirect", "enabled": True,
                "action_parameters": {"from_list": {"name": list_name, "key": "http.request.full_uri"}}}
        # The phase holds every redirect rule of the account: keep the others as they are.
        keep = [{k: r[k] for k in ("expression", "description", "action", "action_parameters", "enabled") if k in r}
                for r in rules if list_name not in r.get("expression", "")]
        if cf.call("PUT", f"{acct}/rulesets/phases/http_request_redirect/entrypoint", {"rules": keep + [rule]},
                   missing_ok=True) is None:
            raise RuntimeError("the token can't change redirect rules (add Account → Bulk URL Redirects → Edit)")
    others = [r.get("description") or r.get("expression") for r in rules if list_name not in r.get("expression", "")]
    if others:
        print(f"       (other redirect rules in this account, kept: {', '.join(others)})")
    sub = "and its subdomains " if item["include_subdomains"] else "(not its subdomains) "
    run.step(bool(lst) and same and on, f"{item['source_url'].rstrip('/')} {sub}redirects to {item['target_url']}", apply)


def check_redirect(cf, s, project, run):
    print("\n3. pages.dev address")
    if not s.get("pages_dev_redirect"):
        print("  (not wanted)")
        return
    item = {"source_url": f"{project['subdomain']}/", "target_url": f"https://{s['host']}/", "status_code": 301,
            "include_subdomains": bool(s["pages_dev_redirect"].get("subdomains")), "subpath_matching": True,
            "preserve_path_suffix": True, "preserve_query_string": True}
    bulk_redirect(cf, re.sub(r"[^a-z0-9_]", "_", s["project"].lower()) + "_pages_dev", item, run)


def check_access(cf, s, run):
    a = s.get("access")
    if not a:
        return
    acct = f"/accounts/{cf.account}"
    print("\n4. Admin login")
    target = f"{s['host']}/{a['path']}"
    admins = addresses(a["admins_file"])
    rel_cfg = os.path.relpath(a["config_file"], s["root"])
    aud = config_value(a["config_file"], "accessAud")
    apps = cf.all(f"{acct}/access/apps")
    app = next((x for x in apps if x.get("aud") == aud), None) or next((x for x in apps if x.get("name") == a["name"]), None)

    def create():
        otp = [i["id"] for i in cf.all(f"{acct}/access/identity_providers") if i.get("type") == "onetimepin"]
        if not otp:
            raise RuntimeError("no One-time PIN login method: Zero Trust → Integrations → Identity providers → Add → One-time PIN")
        pol = next((p for p in cf.all(f"{acct}/access/policies") if p.get("name") == a["policy"]), None) or cf.call(
            "POST", f"{acct}/access/policies",
            {"name": a["policy"], "decision": "allow", "include": [{"email": {"email": e}} for e in admins]})["result"]
        new = cf.call("POST", f"{acct}/access/apps", {
            "name": a["name"], "type": "self_hosted", "domain": target,
            "destinations": [{"type": "public", "uri": target}], "session_duration": a["session_duration"],
            "allowed_idps": otp, "auto_redirect_to_identity": True, "app_launcher_visible": False,
            "policies": [{"id": pol["id"], "precedence": 1}]})["result"]
        write_config_value(a["config_file"], "accessAud", new["aud"])
        team = (cf.call("GET", f"{acct}/access/organizations")["result"] or {}).get("auth_domain")
        if team:
            write_config_value(a["config_file"], "accessTeamDomain", team)
        run.notes.append(f"{rel_cfg} has the new application's AUD: commit and push it, or the admin refuses every sign-in")
    if not app:
        run.step(False, f"Access application “{a['name']}” on {target}, policy “{a['policy']}” = {os.path.basename(a['admins_file'])}", create)
        return

    def set_aud():
        write_config_value(a["config_file"], "accessAud", app["aud"])
        run.notes.append(f"{rel_cfg} changed (accessAud): commit and push it")
    run.step(app.get("aud") == aud, f"{rel_cfg} has the AUD of “{app.get('name')}”", set_aud)

    team = (cf.call("GET", f"{acct}/access/organizations")["result"] or {}).get("auth_domain")
    have_team = config_value(a["config_file"], "accessTeamDomain")

    def set_team():
        write_config_value(a["config_file"], "accessTeamDomain", team)
        run.notes.append(f"{rel_cfg} changed (accessTeamDomain): commit and push it")
    run.step(have_team == team, f"{rel_cfg} has the Zero Trust team domain {team}", set_team)

    covered = [d.get("uri") for d in app.get("destinations", [])] or app.get("self_hosted_domains", [])

    def add_host():
        body = {k: v for k, v in app.items() if k not in ("id", "uid", "aud", "created_at", "updated_at")}
        if app.get("destinations"):
            body["destinations"] = app["destinations"] + [{"type": "public", "uri": target}]
            body.pop("self_hosted_domains", None)
        else:
            body["self_hosted_domains"] = app.get("self_hosted_domains", []) + [target]
        cf.call("PUT", f"{acct}/access/apps/{app['id']}", body)
    run.step(target in covered, f"“{app.get('name')}” covers {target}", add_host)

    for pol in [p for p in app.get("policies", []) if p.get("decision") == "allow"]:
        emails = sorted(i["email"]["email"].lower() for i in pol.get("include", []) if "email" in i)
        others = [i for i in pol.get("include", []) if "email" not in i]

        def set_admins(pol=pol, others=others):
            body = {k: pol[k] for k in ("name", "decision", "exclude", "require") if k in pol}
            body["include"] = others + [{"email": {"email": e}} for e in admins]
            path = (f"{acct}/access/policies/{pol['id']}" if pol.get("reusable")
                    else f"{acct}/access/apps/{app['id']}/policies/{pol['id']}")
            cf.call("PUT", path, body)
        gone, new = sorted(set(emails) - set(admins)), sorted(set(admins) - set(emails))
        change = "; ".join(x for x in (gone and f"remove {', '.join(gone)}", new and f"add {', '.join(new)}") if x)
        run.step(emails == sorted(admins), f"policy “{pol.get('name')}” lets in exactly {os.path.basename(a['admins_file'])}"
                 + (f" ({change})" if change else ""), set_admins)


def upload_worker(cf, worker, to):
    """PUT the Worker as an ES module with FORWARD_TO as a plain-text binding (multipart, so not cf.call)."""
    meta = {"main_module": "worker.js", "compatibility_date": "2026-10-01",
            "bindings": [{"type": "plain_text", "name": "FORWARD_TO", "text": ",".join(to)},
                         {"type": "plain_text", "name": "CODE_VERSION", "text": CODE_VERSION}]}
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"metadata\"\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{b}\r\nContent-Disposition: form-data; name=\"worker.js\"; filename=\"worker.js\"\r\n"
            f"Content-Type: application/javascript+module\r\n\r\n{WORKER_CODE}\r\n--{b}--\r\n").encode()
    req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/accounts/{cf.account}/workers/scripts/{worker}",
                                 method="PUT", data=body, headers={"Authorization": f"Bearer {cf.token}",
                                                                   "Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        res = json.loads(e.read() or b"{}")
    if not res.get("success"):
        raise RuntimeError(f"uploading the Worker failed: {res.get('errors')} (the token needs Account → Workers Scripts → Edit)")


def check_email(cf, s, run):
    e = s.get("email_forward")
    if not e:
        return
    acct = f"/accounts/{cf.account}"
    print("\n5. Email forwarding")
    from zonefile import norm
    zone_name, _ = zone_of(e["address"].split("@")[1], s["zone_file"])
    zone = cf.zone(zone_name)
    if not zone:
        run.step(False, f"{zone_name} is a zone in this account")
        return
    rel = os.path.relpath(s["zone_file"], s["root"])
    keys, lines = set(), []
    for r in cf.call("GET", f"/zones/{zone['id']}/email/routing/dns")["result"]:
        if isinstance(r, dict) and r.get("type") and r.get("content"):
            name = r["name"].lower()
            sub = "" if name == zone_name else name[: -len(zone_name) - 1]
            content, prio = norm(r["type"], r["content"]), str(r.get("priority") or "") if r["type"] == "MX" else ""
            keys.add((r["type"], sub, prio, content))
            value = f'"{content}"' if r["type"] == "TXT" else content
            lines.append(f"{r['type']:5} {sub or '@':22} {prio + ' ' if prio else ''}{value}")

    def write():
        write_mail_records(s["zone_file"], lines)
        run.notes.append(f"{rel} changed: ~/code/domains/cloudflare-dns.py --file {s['zone_file']} (then --apply), and commit it")
    run.step(keys <= zone_keys(s["zone_file"], zone_name), f"{rel} lists the mail records Email Routing needs", write)

    routing = cf.call("GET", f"/zones/{zone['id']}/email/routing")["result"]

    def enable():
        # An old "no mail from this domain" SPF record would clash with Email Routing's own SPF record.
        for r in cf.records(zone):
            if r["type"] == "TXT" and r["sub"] == "" and "v=spf1" in r["content"] and "_spf.mx.cloudflare.net" not in r["content"]:
                cf.delete(zone, r)
        cf.call("POST", f"/zones/{zone['id']}/email/routing/dns", {})
    run.step(bool(routing.get("enabled")), f"Email Routing is on for {zone_name}", enable)

    to = addresses(e["forward_file"])
    known = {x["email"].lower(): x for x in cf.all(f"{acct}/email/routing/addresses")}
    for addr in to:
        if addr not in known:
            run.step(False, f"{addr}: add as destination (Cloudflare then emails a confirmation link)",
                     lambda addr=addr: cf.call("POST", f"{acct}/email/routing/addresses", {"email": addr}))
        elif known[addr].get("verified"):
            run.step(True, f"{addr}: confirmed")
        else:
            print(f"  WAIT {addr}: waiting for its owner to click the link in Cloudflare’s email")

    def bindings():
        res = cf.call("GET", f"{acct}/workers/scripts/{e['worker']}/settings", missing_ok=True)
        return None if not res else {b.get("name"): b.get("text", "") for b in res["result"].get("bindings", [])
                                     if b.get("type") == "plain_text"}
    cur = bindings()
    same = cur is not None and cur.get("CODE_VERSION") == CODE_VERSION and \
        sorted(x.strip() for x in cur.get("FORWARD_TO", "").split(",") if x.strip()) == sorted(to)
    run.step(same, f"Worker {e['worker']} (current code) forwards to the {len(to)} addresses",
             lambda: upload_worker(cf, e["worker"], to))

    want = {"name": f"{e['address']} → {e['worker']}", "enabled": True,
            "matchers": [{"type": "literal", "field": "to", "value": e["address"]}],
            "actions": [{"type": "worker", "value": [e["worker"]]}]}
    rule = next((r for r in cf.all(f"/zones/{zone['id']}/email/routing/rules")
                 if any(m.get("value") == e["address"] for m in r.get("matchers", []))), None)

    def set_rule():
        if bindings() is None:
            raise RuntimeError(f"the Worker {e['worker']} isn't there yet: the rule follows once it is")
        if rule:
            cf.call("PUT", f"/zones/{zone['id']}/email/routing/rules/{rule['id']}", want)
        else:
            cf.call("POST", f"/zones/{zone['id']}/email/routing/rules", want)
    run.step(bool(rule and rule.get("enabled") and rule.get("actions") == want["actions"]),
             f"routing rule: {e['address']} → Worker {e['worker']}", set_rule)


def load_cloudflare(zone_file):
    sys.path.insert(0, DOMAINS)
    spec = importlib.util.spec_from_file_location("cloudflare_dns", os.path.join(DOMAINS, "cloudflare-dns.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from zonefile import account
    return mod.Cloudflare(account(zone_file))


def run_all(cf, s, apply):
    run = Run(apply)
    print(f"{s['project']} ({s['type']}) → https://{s['host']}{'' if apply else '   (preview: nothing is changed)'}\n")
    project = check_project(cf, s, run)
    if project:
        check_address(cf, s, project, run)
        check_redirect(cf, s, project, run)
    check_access(cf, s, run)
    check_email(cf, s, run)
    print()
    for n in run.notes:
        print(f"Next: {n}")
    if run.failed:
        print(f"{len(run.failed)} step(s) failed (see above); fix that and run again with --apply.")
    elif run.todo and not apply:
        print(f"{len(run.todo)} change(s) to make: run again with --apply.")
    elif run.todo:
        print(f"{len(run.todo)} thing(s) only you can do: see TODO above.")
    else:
        print("Everything is set up." if not apply or not run.notes else "Done.")
    return run


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    ap.add_argument("settings", nargs="?", default=SETTINGS, help=f"the site's settings (default: ./{SETTINGS})")
    args = ap.parse_args()
    s = load_settings(args.settings)
    sys.path.insert(0, DOMAINS)
    run_all(load_cloudflare(s["zone_file"]), s, args.apply)


if __name__ == "__main__":
    main()
