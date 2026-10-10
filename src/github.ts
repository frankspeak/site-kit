/**
 * Minimal GitHub client for the admin: everything the admin saves becomes one commit on the configured branch,
 * which makes Cloudflare Pages rebuild the site. Kept tiny because the Workers free plan allows only 10 ms CPU
 * and 50 subrequests per request – the browser prepares file contents, the server just forwards them.
 *
 * Token: the GITHUB_TOKEN secret (fine-grained, "Contents: read and write" on the one repository).
 * GITHUB_API: local tests only (see bin/mock-github.mjs).
 */
import type { AdminConfig, Env } from './auth';

/** Is `path` inside one of the allowed folders, with an allowed file name? */
export const allowed = (path: string, rules: Record<string, RegExp>) =>
  !path.includes('..') &&
  !path.includes('\\') &&
  Object.entries(rules).some(([prefix, file]) => path.startsWith(prefix) && file.test(path.slice(prefix.length)));

export class GitHub {
  private base: string;
  private branch: string;
  private agent: string;
  constructor(private env: Env, admin: AdminConfig) {
    if (!env.GITHUB_TOKEN) throw new Error('GITHUB_TOKEN not configured');
    this.base = `${(env.GITHUB_API ?? 'https://api.github.com').replace(/\/$/, '')}/repos/${admin.githubRepo}`;
    this.branch = admin.githubBranch;
    this.agent = `${admin.githubRepo.split('/').pop()}-admin`;
  }

  private headers(accept = 'application/vnd.github+json') {
    return {
      Authorization: `Bearer ${this.env.GITHUB_TOKEN}`,
      Accept: accept,
      'User-Agent': this.agent,
      'X-GitHub-Api-Version': '2022-11-28',
    };
  }

  async api(path: string, init: RequestInit = {}, accept?: string) {
    const res = await fetch(`${this.base}${path}`, { ...init, headers: { ...this.headers(accept), ...(init.headers ?? {}) } });
    if (!res.ok) throw new Error(`GitHub ${init.method ?? 'GET'} ${path} failed: ${res.status} ${await res.text()}`);
    return res;
  }

  /** Full file tree of the branch, passed straight through (the browser filters it). */
  tree() {
    return this.api(`/git/trees/${encodeURIComponent(this.branch)}?recursive=1`);
  }

  /** Raw file contents. */
  file(path: string) {
    return this.api(`/contents/${path.split('/').map(encodeURIComponent).join('/')}?ref=${encodeURIComponent(this.branch)}`, {}, 'application/vnd.github.raw+json');
  }

  /** Upload a binary file (base64) as a blob; returns its sha for a later commit. */
  async blob(base64: string): Promise<string> {
    const res = await this.api('/git/blobs', { method: 'POST', body: JSON.stringify({ content: base64, encoding: 'base64' }) });
    return ((await res.json()) as { sha: string }).sha;
  }

  /**
   * One commit with several changes: text files (`content`), previously uploaded blobs (`sha`), or deletions.
   * Retries once if someone else committed in between.
   */
  async commit(message: string, changes: Change[]): Promise<string> {
    const tree = changes.map((c) =>
      c.delete
        ? { path: c.path, mode: '100644', type: 'blob', sha: null }
        : c.sha
          ? { path: c.path, mode: '100644', type: 'blob', sha: c.sha }
          : { path: c.path, mode: '100644', type: 'blob', content: c.content ?? '' },
    );
    for (let attempt = 0; ; attempt++) {
      const ref = (await (await this.api(`/git/ref/heads/${encodeURIComponent(this.branch)}`)).json()) as { object: { sha: string } };
      const head = ref.object.sha;
      const base = (await (await this.api(`/git/commits/${head}`)).json()) as { tree: { sha: string } };
      const newTree = (await (await this.api('/git/trees', { method: 'POST', body: JSON.stringify({ base_tree: base.tree.sha, tree }) })).json()) as { sha: string };
      const commit = (await (await this.api('/git/commits', { method: 'POST', body: JSON.stringify({ message, tree: newTree.sha, parents: [head] }) })).json()) as { sha: string };
      try {
        await this.api(`/git/refs/heads/${encodeURIComponent(this.branch)}`, { method: 'PATCH', body: JSON.stringify({ sha: commit.sha, force: false }) });
        return commit.sha;
      } catch (e) {
        if (attempt >= 1) throw e; // branch moved twice in a row – give up and let the user retry
      }
    }
  }
}

export interface Change {
  path: string;
  content?: string;
  sha?: string;
  delete?: boolean;
}
