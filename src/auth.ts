/**
 * Admin login check for Cloudflare Pages Functions.
 *
 * The admin area is protected by Cloudflare Access (email one-time code). Access adds a signed JWT to every request
 * (`Cf-Access-Jwt-Assertion`); we verify it here instead of trusting that Access is configured, because Access only
 * covers hostnames whose DNS runs at Cloudflare (or `*.pages.dev`) – any other domain pointing at the same site
 * would be unprotected. Requests without a valid token are refused (fail closed).
 *
 * From Cloudflare comes only the secret GITHUB_TOKEN; DEV_ADMIN_EMAIL and GITHUB_API are for local testing
 * (.dev.vars) – DEV_ADMIN_EMAIL is honoured exclusively for requests to localhost.
 */

/** Where a site's admin lives and what it may do – normally spread from the site's own settings file. */
export interface AdminConfig {
  /** The address the admin is used at (the only one behind Cloudflare Access); the admin elsewhere is sent here. */
  host: string;
  /** Cloudflare Zero Trust team domain – where the login's signing keys come from. */
  accessTeamDomain: string;
  /** "Audience" (AUD) tag of the Access application that protects the admin. Not secret. */
  accessAud: string;
  /** Where the admin saves changes: GitHub repository ("owner/name") and branch. */
  githubRepo: string;
  githubBranch: string;
  /** The admin may only change these parts of the repository: folder prefix → pattern for the rest of the path. */
  writable: Record<string, RegExp>;
  /** What it may read. Default: the same as `writable`. */
  readable?: Record<string, RegExp>;
  /** URL path of the admin. Default "/admin"; its API is under "<path>/api/". */
  path?: string;
  /** Appended to the "admin isn't set up yet" message, e.g. where the site's README explains the settings. */
  setupHelp?: string;
}

export interface Env {
  GITHUB_TOKEN?: string;
  DEV_ADMIN_EMAIL?: string;
  GITHUB_API?: string;
}

type Jwk = JsonWebKey & { kid: string };
let jwksCache: { url: string; keys: Jwk[]; at: number } | null = null;

const b64urlToBytes = (s: string) => {
  const b64 = s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=');
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
};
const b64urlToJson = (s: string) => JSON.parse(new TextDecoder().decode(b64urlToBytes(s)));

/** Access signing keys, cached for an hour – or 5 minutes when asked for a key we don't have (keys rotate). */
async function getKeys(teamDomain: string, kid?: string): Promise<Jwk[]> {
  const url = `https://${teamDomain}/cdn-cgi/access/certs`;
  const maxAge = kid && !jwksCache?.keys.some((k) => k.kid === kid) ? 5 * 60 * 1000 : 60 * 60 * 1000;
  if (jwksCache && jwksCache.url === url && Date.now() - jwksCache.at < maxAge) return jwksCache.keys;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Could not load Access keys (${res.status})`);
  const { keys } = (await res.json()) as { keys: Jwk[] };
  jwksCache = { url, keys, at: Date.now() };
  return keys;
}

export const isLocal = (request: Request) => ['localhost', '127.0.0.1', '[::1]'].includes(new URL(request.url).hostname);

/** Settings the admin can't work without that aren't in the site's settings file – listed in error messages. */
export function missingSetup(env: Env, request: Request): string[] {
  if (env.DEV_ADMIN_EMAIL && isLocal(request)) return [];
  return env.GITHUB_TOKEN?.trim() ? [] : ['GITHUB_TOKEN'];
}

/** Returns the signed-in admin's email, or null if the request isn't authorised. */
export async function adminEmail(request: Request, env: Env, admin: AdminConfig): Promise<string | null> {
  if (env.DEV_ADMIN_EMAIL && isLocal(request)) return env.DEV_ADMIN_EMAIL;

  const token = request.headers.get('Cf-Access-Jwt-Assertion');
  const team = admin.accessTeamDomain.replace(/^https?:\/\//, '').replace(/\/$/, '');
  if (!token || !team || !admin.accessAud) return null;

  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const [h, p, sig] = parts;
  try {
    const header = b64urlToJson(h);
    const payload = b64urlToJson(p);
    if (header.alg !== 'RS256') return null;

    const jwk = (await getKeys(team, header.kid)).find((k) => k.kid === header.kid);
    if (!jwk) return null;
    const key = await crypto.subtle.importKey('jwk', jwk, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
    const ok = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, b64urlToBytes(sig), new TextEncoder().encode(`${h}.${p}`));
    if (!ok) return null;

    const now = Math.floor(Date.now() / 1000);
    const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
    if (typeof payload.exp !== 'number' || payload.exp < now) return null;
    if (payload.nbf && payload.nbf > now + 60) return null;
    if (payload.iss !== `https://${team}`) return null;
    if (!aud.includes(admin.accessAud)) return null;

    // Who may sign in at all is decided by the policy of the Access application.
    const email = String(payload.email ?? '').toLowerCase();
    return email || null;
  } catch {
    return null;
  }
}

export const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex' },
  });
