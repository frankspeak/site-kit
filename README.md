# site-kit

Shared plumbing for small static websites hosted on **Cloudflare Pages** with their code on **GitHub**: a login for
the site's administrators (Cloudflare Access) and a small admin API that saves their changes to the repository as
commits. Each site keeps its own pages and its own admin screens; site-kit is only the part that is the same everywhere.

This repository is public so that Cloudflare's build and other repositories can fetch it. It contains no secrets and
no site content.

## How an admin built on it works

An administrator signs in with a one-time code sent by email; each *Save* becomes **one commit** in the site's GitHub
repository, and the commit starts the site's build (by Cloudflare or GitHub Actions). Nothing is stored anywhere else: no database.

```mermaid
flowchart LR
  B[Admin's browser<br>fills in a form]
  subgraph Cloudflare
    A[Access<br>email code login]
    F[Pages Functions<br>check login and paths]
    P[Pages<br>serves the new version]
  end
  subgraph GitHub
    G[Git API<br>creates one commit]
    M[Branch main<br>the site's files]
    W[Build<br>Actions, or Cloudflare's]
  end
  B --> A --> F --> G --> M --> W --> P --> S[Live website]
```

What a save does, step by step:

1. **The browser** prepares everything: the site's admin pages build the file contents (and shrink photos). Each
   binary file is uploaded on its own first (`POST blob`); GitHub stores it and returns an ID, but it isn't part of
   the site yet. Then one `POST commit` carries a short summary and the list of changes: a path with new text, a
   path with an uploaded file's ID, or a path to delete.
2. **Cloudflare Access** (Zero Trust) sits in front of the admin's path and only lets a request through after the
   email-code sign-in. It attaches a signed proof of who is signed in.
3. **Cloudflare Pages Functions** (this package) check that proof again themselves – signature, audience, expiry –
   never trusting that Access ran, because the same site also answers at addresses Access doesn't cover. A write
   must come from the admin pages themselves (same origin, JSON). Every path is checked against the folders the
   site allows (`writable`); one path outside them refuses the whole save.
4. **GitHub's Git API**, called with `GITHUB_TOKEN`: read the latest commit on the branch, create a new snapshot with
   the changes applied, create a commit on top, move the branch to it. If someone else committed in between, it
   tries once more, then reports an error so the administrator can save again.
5. **The commit** is recorded under the account that owns the token; its message is the summary plus
   "Saved in the website admin by <email>", so the history shows who saved what – also in the admin itself
   (`GET history`: every change to the content, in the admin or not, with names instead of addresses if the site
   passes `names`). Undoing a save is reverting
   that commit.
6. **The build:** a new commit on the branch starts the site's build – by a GitHub Actions workflow that uploads to
   Pages (bavarianspringboks), or by Cloudflare itself for a project connected to the repository. A save in the admin
   and a `git push` are the same to it. When the build succeeds the site switches to the new version; if it fails,
   the previous version stays online and the error is in the workflow's run or the Pages *Deployments* list.

Functions on the free plan get 10 ms of processing and 50 outgoing requests per request, which is why the browser
does the preparing and the server only checks and forwards.

## Using it in a site

```bash
npm install https://github.com/frankspeak/site-kit/archive/refs/tags/v0.1.0.tar.gz
```

(The release download, not `github:frankspeak/site-kit#v0.1.0`: npm records that form as an SSH address in the lock
file, which Cloudflare's build can't fetch.)

One file creates the admin from the site's settings and says which files it may touch:

```ts
// functions/_lib/admin.ts
import { createAdmin } from 'site-kit';

export const admin = createAdmin({
  host: 'www.example.com',                              // the address behind Cloudflare Access
  accessTeamDomain: 'your-team.cloudflareaccess.com',   // Zero Trust → Settings
  accessAud: '…',                                       // the Access application's "audience" tag (not secret)
  githubRepo: 'owner/repo',
  githubBranch: 'main',
  writable: { 'src/content/events/': /^[^/]+\.md$/ },   // folder → pattern for the rest of the path
  // readable: defaults to `writable`;  path: defaults to '/admin'
  // names: { 'ann@example.com': 'Ann' } – shown instead of addresses; namesFromList() reads them from an admins
  //   list with the name as a comment (`ann@example.com  # Ann`)
});
```

Pages Functions are routed by file name, so each route is a one-line file:

| File | Content |
|---|---|
| `functions/admin/_middleware.ts` | `export const onRequest = admin.guard;` |
| `functions/admin/api/_middleware.ts` | `export const onRequest = admin.apiGuard;` |
| `functions/admin/api/whoami.ts` | `export const onRequestGet = admin.whoami;` |
| `functions/admin/api/history.ts` | `export const onRequestGet = admin.history;` |
| `functions/admin/api/tree.ts` | `export const onRequestGet = admin.tree;` |
| `functions/admin/api/file.ts` | `export const onRequestGet = admin.file;` |
| `functions/admin/api/blob.ts` | `export const onRequestPost = admin.blob;` |
| `functions/admin/api/commit.ts` | `export const onRequestPost = admin.commit;` |

The API the site's admin pages then call (all under `/admin/api/`):

| Call | Does |
|---|---|
| `GET whoami` | `{ email, name }` of the signed-in administrator |
| `GET history?before=…&all=1` | changes to the content folders (the writable prefixes cut to two levels), newest first, about 100 a page: `{ changes: [{ sha, date, summary, by, via, url }], next }`; `via`: `admin`, `code` or `automatic`; `all=1`: every commit; `next` → `before` for older ones |
| `GET tree` | the repository's file list (GitHub's format) |
| `GET file?path=…` | raw contents of a readable file |
| `POST blob` `{ content: base64 }` | uploads one binary file, returns `{ sha }` for the next commit |
| `POST commit` `{ message, changes: [{ path, content \| sha \| delete }] }` | saves everything as one commit |

**The one secret:** `GITHUB_TOKEN` in Cloudflare Pages → *Settings → Variables and secrets* – a fine-grained GitHub
token with *Contents: Read and write* on the one repository. Without it, or without a valid login, the admin refuses
everyone (it fails closed) and says what is missing.

## Testing an admin locally

No real token or repository needed. In the site's folder, `.dev.vars` holds:

```
DEV_ADMIN_EMAIL=you@example.com      # skips the login – honoured for localhost only
GITHUB_TOKEN=local-test
GITHUB_API=http://127.0.0.1:8788
```

```bash
npx site-kit-mock-github src/content src/data    # a stand-in for GitHub: serves these folders, records commits in .mock-github/
npx wrangler pages dev dist --port 8789          # the built site with its Functions
```

The working copy is never changed; every "commit" is listed in `.mock-github/commits.log`.

## Layout

```
src/index.ts             what the package exports
src/auth.ts              checks the Cloudflare Access login (signature, audience, expiry); the settings type
src/github.ts            GitHub client: file list, file contents, upload, one commit with several changes
src/admin.ts             createAdmin(): the guards and the six API handlers
bin/mock-github.mjs      the stand-in GitHub for local testing
setup/site_setup.py      the setup script (Python, not in the npm package)
setup/test_site_setup.py its tests
```

`npm run check` type-checks it. The code is TypeScript used as-is (Cloudflare bundles it); there is no build step.

## Releases

Sites pin a release (`v0.3.0`), so a change here reaches a site only when that site moves to a newer tag.
After a change: `npm run check`, test in a site with `npm install ../site-kit`, then tag and push
(`git tag v0.x.0 && git push --tags`).

## Sites using it

- `bavarianspringboks` – club website with an admin for events, photos and newsletters.
- `family-tree` – the setup script only (a private archive; no admin).

## Setup script

`setup/site_setup.py` makes a site's Cloudflare setup match the `site-setup.json` in its folder. Run it there:

```bash
python3 ~/code/site-kit/setup/site_setup.py            # shows what is set up and what would change
python3 ~/code/site-kit/setup/site_setup.py --apply    # makes the changes
```

It never reads or writes a secret's value; it lists the missing ones with the dashboard path to add them. It uses the
Cloudflare API helper and token of the private `~/code/domains` repository (its README, "Cloudflare API token"), and
the account named on the zone file's `account` line, so it (and its tests) only run where that repository is checked
out. Not part of the npm package.

| Step | What it checks, and sets with `--apply` | API docs |
|---|---|---|
| 1. Pages project | exists; for a project Cloudflare builds from GitHub: build watch paths, preview branches; R2 bindings; secrets set (names only) | [Pages projects](https://developers.cloudflare.com/api/resources/pages/subresources/projects/methods/edit/), [build watch paths](https://developers.cloudflare.com/pages/configuration/build-watch-paths/), [branch controls](https://developers.cloudflare.com/pages/configuration/branch-build-controls/) |
| 2. Address | the custom domain; the zone file's CNAME pointing at the project's pages.dev name | [Custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/) |
| 3. pages.dev address | a Bulk Redirect (301, path kept) to the site's domain; with or without subdomains | [Bulk Redirects](https://developers.cloudflare.com/rules/url-forwarding/bulk-redirects/) |
| 4. Admin login | the Access application on `<host>/<path>`, policy "Admin" = the admins file, its AUD and team domain written into the site's config | [Self-hosted applications](https://developers.cloudflare.com/cloudflare-one/applications/configure-apps/self-hosted-public-app/), [One-time PIN](https://developers.cloudflare.com/cloudflare-one/identity/one-time-pin/) |
| 5. Email forwarding | Email Routing, its records in the zone file, destination addresses, an Email Worker forwarding one address to everyone in a file | [Email Routing](https://developers.cloudflare.com/email-routing/) |

**Site types.** `"type"` fills in what that kind of site needs; anything in `site-setup.json` overrides it:

| Type | Example | Defaults |
|---|---|---|
| `private-archive` | family-tree | Cloudflare builds from GitHub; secrets `SITE_USER`, `SITE_PASSWORD` in production and preview; pages.dev redirect without subdomains (staging stays) |
| `public-with-admin` | bavarianspringboks | built by GitHub Actions; secret `GITHUB_TOKEN`; pages.dev redirect with subdomains; admin login on `/admin` from `admins.txt`, IDs into `src/config.mjs` |

Other keys: `project`, `host`, `zone_file` (required); `watch_paths`, `preview_branches`, `r2` (`{"BINDING": "bucket"}`),
`access` (`name`, `path`, `admins_file`, `config_file`), `email_forward` (`address`, `forward_file`, `worker`).
Tests (no network): `python3 -m unittest discover -s setup`.

## Planned

1. **Access rules per path** – one middleware with three modes (`public`, shared `password`, `access`), so a
   whole-site login like family-tree's uses the same code. When the family edits.
2. **GitHub App instead of a personal token** – saves no longer depend on one person's GitHub account. When the
   club's repository moves to a club organisation.
3. Shared GitHub workflows (daily rebuild, photo conversion).
