/**
 * Request shaping for the `web` container.
 *
 * Django is configured with TRUST_CLOUDFLARE_PROXY=true, which makes it believe
 * X-Forwarded-Proto/-Host. That is only safe because this Worker REPLACES those
 * headers with values derived from the verified public request URL and drops any
 * client-supplied forwarding headers.
 */
const STRIP_REQUEST_HEADERS = [
  'forwarded',
  'x-forwarded-for',
  'x-forwarded-host',
  'x-forwarded-proto',
  'x-forwarded-port',
  'x-forwarded-server',
  'x-real-ip',
  'x-original-host',
  'x-original-url',
  'x-rewrite-url',
  'cf-container-target-port', // never let a client pick the container port
];

export function toContainerRequest(request: Request): Request {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  for (const name of STRIP_REQUEST_HEADERS) headers.delete(name);
  headers.set('X-Forwarded-Proto', url.protocol === 'http:' ? 'http' : 'https');
  headers.set('X-Forwarded-Host', url.host);
  const clientIp = request.headers.get('cf-connecting-ip');
  if (clientIp) headers.set('X-Forwarded-For', clientIp);
  return new Request(request, { headers });
}

/** Headers for the controller's own readiness probe (same trust contract as real traffic). */
export function probeHeaders(publicHost: string): Headers {
  return new Headers({ 'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': publicHost, Accept: 'application/json' });
}

export function jsonError(status: number, code: string, retryAfterSeconds?: number): Response {
  const headers = new Headers({ 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  if (retryAfterSeconds !== undefined) headers.set('Retry-After', String(retryAfterSeconds));
  return new Response(JSON.stringify({ error: code }), { status, headers });
}

const PROXY_ERROR_PREFIX = 'Error proxying request to container';

/**
 * @cloudflare/containers returns the raw exception text in a 500 body when the
 * proxy hop fails. Replace that with a fixed message so internals never reach clients.
 */
export async function scrubProxyError(response: Response): Promise<Response> {
  if (response.status !== 500) return response;
  const type = response.headers.get('content-type') ?? '';
  if (type && !type.startsWith('text/plain')) return response;
  const text = await response.clone().text();
  if (text.startsWith(PROXY_ERROR_PREFIX) || text.startsWith('Container suddenly disconnected')) {
    return jsonError(502, 'upstream_unavailable', 2);
  }
  return response;
}
