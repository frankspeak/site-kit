"""Tests for site_setup.py against a stand-in Cloudflare API (no network, no token).

    python3 -m unittest discover -s ~/code/site-kit/setup        (needs ~/code/domains for the zone-file parser)
"""
import contextlib, copy, io, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.expanduser("~/code/domains"))
import site_setup as S  # noqa: E402

ACCT = "a" * 32


class FakeCloudflare:
    """Just enough of the API for the steps: state in dicts, every non-GET call recorded."""

    def __init__(self, state):
        self.account, self.token, self.s, self.writes = ACCT, "test", state, []

    def all(self, path):
        return self.call("GET", path)["result"]

    def zone(self, name):
        return self.s["zones"].get(name)

    def records(self, zone):
        return []

    def delete(self, zone, r):
        self.writes.append(("DELETE", r))

    def call(self, method, path, body=None, missing_ok=False):
        p = path.split("?")[0].replace(f"/accounts/{ACCT}", "")
        s = self.s
        if method != "GET":
            self.writes.append((method, p, copy.deepcopy(body)))
        ok = lambda r: {"success": True, "result": r}
        if p.startswith("/pages/projects/"):
            name, _, rest = p[len("/pages/projects/"):].partition("/")
            proj = s["projects"].get(name)
            if not proj:
                return None if missing_ok else sys.exit("no project")
            if rest == "domains":
                if method == "POST":
                    proj["domains"].append(body["name"])
                return ok([{"name": d} for d in proj["domains"]])
            if method == "PATCH":
                for k, v in body.items():
                    if k == "deployment_configs":
                        for env, cfg in v.items():
                            for kk, vv in cfg.items():
                                proj["deployment_configs"].setdefault(env, {}).setdefault(kk, {}).update(vv)
                    else:
                        proj[k] = v
            return ok(proj)
        if p == "/rules/lists":
            if method == "POST":
                s["lists"].append({"id": "L1", **body})
                return ok(s["lists"][-1])
            return ok(s["lists"])
        if p.endswith("/items"):
            if method == "PUT":
                s["items"] = [b["redirect"] for b in body]
                return ok({"operation_id": "op"})
            return ok([{"redirect": i} for i in s["items"]])
        if p.startswith("/rules/lists/bulk_operations/"):
            return ok({"status": "completed"})
        if p == "/rulesets/phases/http_request_redirect/entrypoint":
            if method == "PUT":
                s["rules"] = body["rules"]
            return ok({"rules": s["rules"]})
        if p == "/access/apps":
            if method == "POST":
                s["apps"].append({"id": "app2", "aud": "newaud", "policies": [], **body})
                return ok(s["apps"][-1])
            return ok(s["apps"])
        if p.startswith("/access/apps/") or p.startswith("/access/policies/"):
            return ok({})
        if p == "/access/policies":
            return ok(s.get("policies", [])) if method == "GET" else ok({"id": "pol", **body})
        if p == "/access/identity_providers":
            return ok([{"id": "otp", "type": "onetimepin"}])
        if p == "/access/organizations":
            return ok({"auth_domain": s["team"]})
        if p.endswith("/email/routing/dns"):
            return ok(s["mail_records"]) if method == "GET" else ok({})
        if p.endswith("/email/routing"):
            return ok({"enabled": s["routing"]})
        if p == "/email/routing/addresses":
            return ok(s["destinations"])
        if p.startswith("/workers/scripts/"):
            return ok({"bindings": s["worker"]}) if s["worker"] is not None else None
        if p.endswith("/email/routing/rules"):
            return ok(s["mail_rules"])
        raise AssertionError(f"unexpected call {method} {p}")


ZONE = """account {acct}
domains example.org other.net
CNAME  mail  ghs.google.com
[example.org]
CNAME  family  old-name.pages.dev
[other.net]
CNAME  family  not-this-one.pages.dev
""".format(acct=ACCT)

CLUB_DNS = """account {acct}
domains club.test
[club.test]
CNAME     www  club-xyz.pages.dev  proxied
# >>> mail (written by scripts/cloudflare-setup.py)
MX    @                      13 route1.mx.cloudflare.net
# <<< mail
""".format(acct=ACCT)


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        r = fn(*a)
    return r, out.getvalue()


class SiteSetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        S.write(path, text)
        return path

    # ── a private archive (family-tree's type) ───────────────────────────────
    def archive(self):
        self.write("zone.txt", ZONE)
        s = S.load_settings(self.write("site-setup.json", json.dumps({
            "type": "private-archive", "zone_file": "zone.txt", "project": "tree", "host": "family.example.org",
            "watch_paths": ["index.html", "app.js"], "preview_branches": ["staging"], "r2": {"MEDIA": "tree-media"}})))
        state = {"projects": {"tree": {
            "subdomain": "tree-cdu.pages.dev", "domains": ["family.example.org"],
            "source": {"type": "github", "config": {"owner": "me", "repo_name": "tree", "production_branch": "main",
                                                     "preview_deployment_setting": "all"}},
            "deployment_configs": {
                "production": {"r2_buckets": {"MEDIA": {"name": "tree-media"}},
                               "env_vars": {"SITE_USER": {"type": "secret_text"}, "SITE_PASSWORD": {"type": "secret_text"}}},
                "preview": {"env_vars": {"SITE_USER": {"type": "secret_text"}}}}}},
            "lists": [], "items": [], "rules": [{"expression": "x in $other", "description": "someone else's", "action": "redirect"}]}
        return s, state

    def test_type_defaults_fill_in(self):
        s, _ = self.archive()
        self.assertEqual(s["build"], "cloudflare-git")
        self.assertEqual(s["secrets"]["preview"], ["SITE_USER", "SITE_PASSWORD"])
        self.assertFalse(s["pages_dev_redirect"]["subdomains"])
        self.assertNotIn("access", s)

    def test_preview_changes_nothing(self):
        s, state = self.archive()
        cf = FakeCloudflare(state)
        run, out = quiet(S.run_all, cf, s, False)
        self.assertEqual(cf.writes, [])
        self.assertIn("TODO build watch paths: index.html, app.js; preview builds only for staging", out)
        self.assertIn("TODO R2 bucket tree-media bound as MEDIA (preview)", out)
        self.assertIn("TODO secret SITE_PASSWORD is set (preview)", out)
        self.assertIn("zone.txt points family.example.org at tree-cdu.pages.dev (now old-name.pages.dev)", out)
        self.assertEqual(S.read(s["zone_file"]), ZONE)

    def test_apply_archive(self):
        s, state = self.archive()
        cf = FakeCloudflare(state)
        run, out = quiet(S.run_all, cf, s, True)
        cfg = state["projects"]["tree"]["source"]["config"]
        # The build settings change; the repository and branch stay.
        self.assertEqual(cfg["path_includes"], ["index.html", "app.js"])
        self.assertEqual((cfg["preview_deployment_setting"], cfg["preview_branch_includes"]), ("custom", ["staging"]))
        self.assertEqual((cfg["repo_name"], cfg["production_branch"]), ("tree", "main"))
        self.assertEqual(state["projects"]["tree"]["deployment_configs"]["preview"]["r2_buckets"]["MEDIA"]["name"], "tree-media")
        # Secrets are never written: still a TODO for Frank.
        self.assertIn("TODO secret SITE_PASSWORD is set (preview)", out)
        self.assertFalse(any("env_vars" in json.dumps(w) for w in cf.writes))
        # Only the [example.org] record moved; other.net's same-named record didn't.
        zone = S.read(s["zone_file"])
        self.assertIn("CNAME  family  tree-cdu.pages.dev", zone)
        self.assertIn("CNAME  family  not-this-one.pages.dev", zone)
        # The redirect: bare name only, and the account's other rule kept.
        self.assertEqual(state["items"][0]["source_url"], "tree-cdu.pages.dev/")
        self.assertFalse(state["items"][0]["include_subdomains"])
        self.assertEqual([r.get("description") for r in state["rules"]][0], "someone else's")
        self.assertIn("$tree_pages_dev", state["rules"][1]["expression"])
        self.assertIn("tree-cdu.pages.dev (not its subdomains) redirects to", out)
        self.assertIn("Done: 4 change(s) made.", out)
        # A second run finds nothing left to change but the secret.
        cf2 = FakeCloudflare(state)
        run2, out2 = quiet(S.run_all, cf2, s, True)
        self.assertEqual(cf2.writes, [])
        self.assertEqual(run2.manual, ["secret SITE_PASSWORD is set (preview): Workers & Pages → tree → Settings → "
                                     "Variables and secrets → Preview → Add, type Secret"])

    # ── a public site with an admin (the club's type) ─────────────────────────
    def club(self, apps):
        self.write("dns.txt", CLUB_DNS)
        self.write("admins.txt", "a@x.de\nb@x.de\n")
        self.write("info-forward.txt", "a@x.de\n")
        self.write("src/config.mjs", "accessTeamDomain: 'old-team.cloudflareaccess.com',\naccessAud: 'oldaud',\n")
        s = S.load_settings(self.write("site-setup.json", json.dumps({
            "type": "public-with-admin", "zone_file": "dns.txt", "project": "club", "host": "www.club.test",
            "access": {"name": "club admin"},
            "email_forward": {"address": "info@club.test", "forward_file": "info-forward.txt", "worker": "fwd"}})))
        state = {"projects": {"club": {"subdomain": "club-xyz.pages.dev", "domains": [],
                                       "deployment_configs": {"production": {"env_vars": {"GITHUB_TOKEN": {"type": "secret_text"}}}}}},
                 "lists": [], "items": [], "rules": [], "apps": apps, "team": "club.cloudflareaccess.com",
                 "zones": {"club.test": {"id": "z1", "name": "club.test"}},
                 "mail_records": [{"type": "MX", "name": "club.test", "content": "route1.mx.cloudflare.net", "priority": 13}],
                 "routing": True, "destinations": [{"email": "a@x.de", "verified": True}],
                 "worker": [{"type": "plain_text", "name": "CODE_VERSION", "text": S.CODE_VERSION},
                            {"type": "plain_text", "name": "FORWARD_TO", "text": "a@x.de"}],
                 "mail_rules": [{"id": "r1", "enabled": True, "matchers": [{"type": "literal", "field": "to", "value": "info@club.test"}],
                                 "actions": [{"type": "worker", "value": ["fwd"]}]}]}
        return s, state

    def test_apply_club_existing_app(self):
        app = {"id": "app1", "aud": "realaud", "name": "club admin", "destinations": [{"type": "public", "uri": "www.club.test/admin"}],
               "policies": [{"id": "p1", "name": "Admin", "decision": "allow", "reusable": True,
                             "include": [{"email": {"email": "a@x.de"}}]}]}
        s, state = self.club([app])
        cf = FakeCloudflare(state)
        run, out = quiet(S.run_all, cf, s, True)
        cfg = S.read(os.path.join(self.dir, "src/config.mjs"))
        self.assertIn("accessAud: 'realaud'", cfg)
        self.assertIn("accessTeamDomain: 'club.cloudflareaccess.com'", cfg)
        self.assertIn(("POST", "/pages/projects/club/domains", {"name": "www.club.test"}), cf.writes)
        put = [w for w in cf.writes if w[1] == "/access/policies/p1"]
        self.assertEqual(sorted(i["email"]["email"] for i in put[0][2]["include"]), ["a@x.de", "b@x.de"])
        self.assertTrue(state["items"][0]["include_subdomains"])
        # Mail: records already between (old) markers, Worker current, rule right: nothing to do there.
        self.assertIn("OK   dns.txt lists the mail records Email Routing needs", out)
        self.assertIn("OK   Worker fwd (current code) forwards to the 1 addresses", out)
        self.assertIn("OK   routing rule: info@club.test → Worker fwd", out)
        self.assertIn("Next: src/config.mjs changed (accessAud): commit and push it", out)

    def test_apply_club_creates_app(self):
        s, state = self.club([])
        cf = FakeCloudflare(state)
        run, out = quiet(S.run_all, cf, s, True)
        made = [w for w in cf.writes if w[:2] == ("POST", "/access/apps")][0][2]
        self.assertEqual((made["domain"], made["allowed_idps"]), ("www.club.test/admin", ["otp"]))
        cfg = S.read(os.path.join(self.dir, "src/config.mjs"))
        self.assertIn("accessAud: 'newaud'", cfg)
        self.assertIn("accessTeamDomain: 'club.cloudflareaccess.com'", cfg)
        self.assertIn("refuses every sign-in", out)

    def test_missing_project_says_how(self):
        s, state = self.club([])
        state["projects"] = {}
        run, out = quiet(S.run_all, FakeCloudflare(state), s, False)
        self.assertIn("Deploy workflow creates it", out)

    def test_mail_markers_written_fresh(self):
        path = self.write("z.txt", 'account x\n[club.test]\nTXT @ "v=spf1 -all"\n')
        S.write_mail_records(path, ["MX    @ 13 route1.mx.cloudflare.net"])
        text = S.read(path)
        self.assertIn(S.MAIL_START, text)
        self.assertNotIn("v=spf1 -all", text)


if __name__ == "__main__":
    unittest.main()
