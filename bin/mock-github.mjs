#!/usr/bin/env node
/**
 * A tiny stand-in for the GitHub API, for testing a site's admin locally without a real token or repository.
 * Serves the files of the folder it is started in and records every commit to OUT_DIR (default: .mock-github/),
 * mirroring paths, plus a commits.log – the working copy itself is never changed.
 *
 *   npx site-kit-mock-github [folder ...]      # folders the admin can see (default: src/content src/data public/images)
 *
 * Listens on http://127.0.0.1:8788 (PORT to change). Point the admin at it with GITHUB_API in .dev.vars.
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const ROOT = process.cwd();
const OUT = path.resolve(ROOT, process.env.OUT_DIR || '.mock-github');
const PORT = Number(process.env.PORT || 8788);
const SCAN = (process.argv.length > 2 ? process.argv.slice(2) : ['src/content', 'src/data', 'public/images'])
  .map((d) => d.replace(/\/$/, '')).filter((d) => fs.existsSync(path.join(ROOT, d)));

const files = new Map(); // path -> Buffer (null = deleted)
const blobs = new Map(); // sha -> Buffer
const trees = new Map(); // tree sha -> entries
let head = 'c0';
let commitNo = 0;

const sha = (buf) => crypto.createHash('sha1').update(buf).digest('hex');
function walk(dir, out = []) {
  for (const e of fs.readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
    const p = `${dir}/${e.name}`;
    if (e.isDirectory()) walk(p, out); else if (!e.name.startsWith('.')) out.push(p);
  }
  return out;
}
const read = (p) => (files.has(p) ? files.get(p) : fs.existsSync(path.join(ROOT, p)) ? fs.readFileSync(path.join(ROOT, p)) : null);

function allPaths() {
  const set = new Set(SCAN.flatMap((d) => walk(d)));
  for (const [p, b] of files) b === null ? set.delete(p) : set.add(p);
  return [...set].sort();
}

const body = (req) => new Promise((r) => { const c = []; req.on('data', (d) => c.push(d)); req.on('end', () => r(Buffer.concat(c).toString())); });
const send = (res, status, data, type = 'application/json') => { res.writeHead(status, { 'Content-Type': type }); res.end(type === 'application/json' ? JSON.stringify(data) : data); };

http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const m = url.pathname.match(/^\/repos\/[^/]+\/[^/]+(\/.*)$/);
  if (!m) return send(res, 404, { message: 'not found' });
  const p = m[1];
  if (!req.headers.authorization) return send(res, 401, { message: 'no token' });
  try {
    if (req.method === 'GET' && p.startsWith('/git/trees/')) {
      return send(res, 200, { sha: 't', truncated: false, tree: allPaths().map((f) => ({ path: f, type: 'blob', sha: sha(read(f)) })) });
    }
    if (req.method === 'GET' && p.startsWith('/contents/')) {
      const f = decodeURIComponent(p.slice('/contents/'.length));
      const b = read(f);
      return b ? send(res, 200, b, 'text/plain; charset=utf-8') : send(res, 404, { message: 'Not Found' });
    }
    if (req.method === 'POST' && p === '/git/blobs') {
      const { content, encoding } = JSON.parse(await body(req));
      const buf = Buffer.from(content, encoding === 'base64' ? 'base64' : 'utf8');
      const s = sha(buf); blobs.set(s, buf);
      return send(res, 201, { sha: s });
    }
    if (req.method === 'GET' && p.startsWith('/git/ref/heads/')) return send(res, 200, { object: { sha: head } });
    if (req.method === 'GET' && p.startsWith('/git/commits/')) return send(res, 200, { sha: p.split('/').pop(), tree: { sha: `tree-${head}` } });
    if (req.method === 'POST' && p === '/git/trees') {
      const { tree } = JSON.parse(await body(req));
      const id = `tree-${crypto.randomUUID()}`; trees.set(id, tree);
      return send(res, 201, { sha: id });
    }
    if (req.method === 'POST' && p === '/git/commits') {
      const { message, tree } = JSON.parse(await body(req));
      const id = `c${++commitNo}`; trees.set(id, { message, tree });
      return send(res, 201, { sha: id });
    }
    if (req.method === 'PATCH' && p.startsWith('/git/refs/heads/')) {
      const { sha: id } = JSON.parse(await body(req));
      const { message, tree } = trees.get(id);
      const entries = trees.get(tree);
      const log = [`── commit ${id}: ${message.split('\n')[0]}`];
      for (const e of entries) {
        let buf = null;
        if (e.sha === null) { files.set(e.path, null); log.push(`   - ${e.path}`); continue; }
        if (e.content !== undefined) buf = Buffer.from(e.content);
        else buf = blobs.get(e.sha) ?? allPaths().map((f) => [f, read(f)]).find(([, b]) => sha(b) === e.sha)?.[1];
        if (!buf) throw new Error(`unknown blob ${e.sha} for ${e.path}`);
        files.set(e.path, buf);
        const dest = path.join(OUT, e.path); fs.mkdirSync(path.dirname(dest), { recursive: true }); fs.writeFileSync(dest, buf);
        log.push(`   + ${e.path} (${buf.length} bytes)`);
      }
      fs.mkdirSync(OUT, { recursive: true });
      fs.appendFileSync(path.join(OUT, 'commits.log'), log.join('\n') + '\n');
      console.log(log.join('\n'));
      head = id;
      return send(res, 200, { object: { sha: id } });
    }
    send(res, 404, { message: `mock: ${req.method} ${p} not implemented` });
  } catch (e) {
    send(res, 500, { message: String(e) });
  }
}).listen(PORT, '127.0.0.1', () => console.log(`mock GitHub on http://127.0.0.1:${PORT} – commits go to ${OUT}`));
