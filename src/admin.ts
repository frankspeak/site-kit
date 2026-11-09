/**
 * The admin of a site, as Cloudflare Pages Functions: a guard for the admin pages and a small API that reads the
 * repository and saves changes to it as commits. A site creates it once from its settings and re-exports the
 * handlers from its `functions/` files (see README).
 */
import { adminEmail, isLocal, json, missingSetup, type AdminConfig, type Env } from './auth';
import { GitHub, allowed, type Change } from './github';

/** Same as the site should send for its admin path in public/_headers (repeated: Pages doesn't always apply _headers behind Functions). */
const CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'";

/** Generous: a commit is one tree request whatever its size. Pages that upload a lot split it themselves. */
const MAX_CHANGES = 1000;
const MAX_BLOB_BYTES = 8 * 1024 * 1024; // photos arrive resized – anything bigger is a mistake
/** Commits read per folder per history page (GitHub's maximum); a few such lists parse well within the free plan's 10 ms CPU. */
const HISTORY_PAGE = 100;
/** At most this many content folders are read for the history (one GitHub call each). */
const MAX_HISTORY_FOLDERS = 4;
/** The line `commit` adds to every message (the history finds the admin's saves by it). */
const SAVED_BY = 'Saved in the website admin by ';

type Handler = PagesFunction<Env>;

export function createAdmin(config: AdminConfig) {
  const base = (config.path ?? '/admin').replace(/\/$/, '');
  const api = `${base}/api/`;
  const readable = config.readable ?? config.writable;
  const nameOf = (email: string) => config.names?.[email.toLowerCase()] ?? email;
  // The content folders the history follows: the writable prefixes cut to two levels (src/content/events/ → src/content).
  const historyFolders = [...new Set(Object.keys(config.writable).map((p) => p.split('/').filter(Boolean).slice(0, 2).join('/')))]
    .filter(Boolean).slice(0, MAX_HISTORY_FOLDERS);

  /**
   * Guards the admin pages (functions<path>/_middleware.ts). On any other host than `config.host` (one without
   * Cloudflare Access) visitors are sent to the protected address; everyone else must carry a valid Access login.
   * The API checks the login itself (`apiGuard`) and answers in JSON.
   */
  const guard: Handler = async ({ request, env, next }) => {
    const url = new URL(request.url);
    if (config.host && url.hostname !== config.host && !isLocal(request)) {
      return Response.redirect(`https://${config.host}${url.pathname}${url.search}`, 302);
    }
    const text = (msg: string, status: number) =>
      new Response(msg, { status, headers: { 'Content-Type': 'text/plain; charset=utf-8', 'X-Robots-Tag': 'noindex' } });
    if (url.pathname.startsWith(api)) return next();
    const missing = missingSetup(env, request);
    if (missing.length) {
      return text(`The admin isn't set up yet: this secret is missing in Cloudflare Pages: ${missing.join(', ')}. ${config.setupHelp ?? 'Add it, then redeploy.'}`, 503);
    }
    if (!(await adminEmail(request, env, config))) {
      return text(`Not signed in, or this email address is not allowed to use the admin. Open https://${config.host}${base} and sign in with an administrator's email address.`, 403);
    }
    const res = await next();
    const out = new Response(res.body, res);
    out.headers.set('X-Robots-Tag', 'noindex');
    out.headers.set('Cache-Control', 'no-store');
    out.headers.set('Content-Security-Policy', CSP);
    return out;
  };

  /** Every admin API call needs a valid login (functions<path>/api/_middleware.ts). Sets `data.email`. */
  const apiGuard: Handler = async (ctx) => {
    const { request } = ctx;
    // Writes must come from the admin pages themselves: same origin and a JSON body (a plain form post from another
    // site can't set either), so a signed-in administrator can't be tricked into saving something elsewhere.
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      const origin = request.headers.get('Origin');
      if (origin !== new URL(request.url).origin) return json({ error: 'Request from another site refused' }, 403);
      if (!(request.headers.get('Content-Type') ?? '').startsWith('application/json')) return json({ error: 'Expected JSON' }, 415);
    }
    const missing = missingSetup(ctx.env, request);
    if (missing.length) return json({ error: `The admin isn't set up yet – missing settings: ${missing.join(', ')}` }, 503);
    const email = await adminEmail(request, ctx.env, config);
    if (!email) return json({ error: 'Not signed in – reload the page to sign in again.' }, 401);
    ctx.data.email = email;
    try {
      return await ctx.next();
    } catch (e) {
      return json({ error: e instanceof Error ? e.message : String(e) }, 500);
    }
  };

  /** GET – who is signed in: `{ email, name }` (name: from `config.names`, else the email). */
  const whoami: Handler = async ({ data }) => json({ email: data.email, name: nameOf(String(data.email)) });

  /**
   * GET ?before=<ISO time>&all=1 – changes to the site's content, newest first:
   * `{ changes: [{ sha, date, summary, by, via, url }], next }`.
   * `via`: 'admin' (saved in the admin; `by` is the name from `config.names`, else the email), 'code' (a commit made
   * elsewhere; `by` from the author's email via `config.names`, else the author's name) or 'automatic' (a bot).
   * Content = commits touching the writable folders (one GitHub call per folder, 100 commits each, merged); `all=1`:
   * every commit on the branch. `next`: pass as `before` for older changes (the first may repeat – drop it by sha);
   * null when there are none.
   */
  const history: Handler = async ({ request, env }) => {
    const params = new URL(request.url).searchParams;
    const before = params.get('before') ?? '';
    const until = before && !Number.isNaN(Date.parse(before)) ? new Date(before).toISOString() : undefined;
    const gh = new GitHub(env, config);
    const paths = params.get('all') === '1' ? [undefined] : historyFolders;
    const lists = await Promise.all(paths.map((path) => gh.commits(HISTORY_PAGE, { path, until })));
    // A full list may go on beyond its oldest commit: everything older than the newest such cut-off waits for `next`.
    const cut = lists.filter((l) => l.length === HISTORY_PAGE).map((l) => l[l.length - 1].commit.committer.date).sort().pop() ?? null;
    const seen = new Set<string>();
    const changes = lists.flat()
      .filter((c) => (!cut || c.commit.committer.date >= cut) && !seen.has(c.sha) && seen.add(c.sha))
      .sort((x, y) => (x.commit.committer.date < y.commit.committer.date ? 1 : -1))
      .map((c) => {
        const msg = c.commit.message;
        const at = msg.lastIndexOf(SAVED_BY);
        const a = c.commit.author;
        const [by, via] = at >= 0
          ? [nameOf(msg.slice(at + SAVED_BY.length).trim().split(/\s/)[0]), 'admin']
          : /\[bot\]$/.test(a.name) ? ['Automatic', 'automatic']
          : [config.names?.[a.email.toLowerCase()] ?? a.name, 'code'];
        return { sha: c.sha, date: c.commit.author.date, summary: msg.split('\n')[0], by, via, url: c.html_url };
      });
    return new Response(JSON.stringify({ changes, next: cut }), {
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    });
  };

  /** GET – the repository's file list (passed through; the browser picks out what it needs). */
  const tree: Handler = async ({ env }) => {
    const res = await new GitHub(env, config).tree();
    return new Response(res.body, { headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
  };

  /** GET ?path=… – raw contents of a readable file. */
  const file: Handler = async ({ request, env }) => {
    const path = new URL(request.url).searchParams.get('path') ?? '';
    if (!allowed(path, readable)) return json({ error: 'Path not allowed' }, 400);
    const res = await new GitHub(env, config).file(path);
    return new Response(res.body, { headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' } });
  };

  /** POST { content: base64 } – uploads one photo; returns { sha } to include in the following commit. */
  const blob: Handler = async ({ request, env }) => {
    const { content } = (await request.json()) as { content?: string };
    if (typeof content !== 'string' || !content || content.length > (MAX_BLOB_BYTES * 4) / 3) return json({ error: 'Invalid photo' }, 400);
    return json({ sha: await new GitHub(env, config).blob(content) });
  };

  /** POST { message, changes: [{ path, content? | sha? | delete? }] } – saves everything as one commit. */
  const commit: Handler = async ({ request, env, data }) => {
    const body = (await request.json()) as { message?: string; changes?: Change[] };
    const changes = body.changes ?? [];
    if (!changes.length) return json({ error: 'Nothing to save' }, 400);
    if (changes.length > MAX_CHANGES) return json({ error: `Too many changes in one save (${changes.length}, at most ${MAX_CHANGES})` }, 400);
    for (const c of changes) {
      if (!allowed(c.path, config.writable)) return json({ error: `Not allowed: ${c.path}` }, 400);
      if (!c.delete && !c.sha && typeof c.content !== 'string') return json({ error: `No content for ${c.path}` }, 400);
    }
    const message = `${(body.message ?? 'Admin update').slice(0, 120)}\n\n${SAVED_BY}${data.email}`;
    return json({ commit: await new GitHub(env, config).commit(message, changes) });
  };

  return { guard, apiGuard, whoami, history, tree, file, blob, commit };
}
