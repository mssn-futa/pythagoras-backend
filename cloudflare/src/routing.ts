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
  // Alternative scheme signals: gunicorn/Django honour some of these and they cause
  // "Contradictory scheme headers" (400) when mixed with X-Forwarded-Proto.
  'x-forwarded-ssl',
  'x-forwarded-protocol',
  'x-forwarded-scheme',
  'x-url-scheme',
  'x-scheme',
  'front-end-https',
];

/**
 * Header names containing "_" are dropped wholesale: WSGI servers normalise "-" and
 * "_" into the same environ key, so a client could otherwise smuggle names such as
 * SCRIPT_NAME or PATH_INFO past the dash-based list above.
 */
function isUnderscoreHeader(name: string): boolean {
  return name.includes('_');
}

export function toContainerRequest(request: Request): Request {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  for (const name of STRIP_REQUEST_HEADERS) headers.delete(name);
  for (const name of [...headers.keys()]) {
    if (isUnderscoreHeader(name)) headers.delete(name);
  }
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

const LIBRARY_ERROR_PREFIXES = [
  'Error proxying request to container',
  'Container suddenly disconnected',
  'Failed to start container',
  'There is no Container instance available',
];

/**
 * @cloudflare/containers returns raw exception text (text/plain) when its start or
 * proxy hop fails: 500 "Error proxying...", 500 "Failed to start container: <msg>",
 * 503 "There is no Container instance...", and 429 with an arbitrary `e.message`.
 * Replace each with a fixed JSON body so internals never reach clients. Real Django
 * responses are JSON/HTML, never text/plain with these shapes.
 */
export async function scrubProxyError(response: Response): Promise<Response> {
  const status = response.status;
  if (status !== 500 && status !== 503 && status !== 429) return response;
  const type = response.headers.get('content-type') ?? '';
  if (type && !type.startsWith('text/plain')) return response;
  const text = await response.clone().text();
  if (status === 429) return jsonError(503, 'container_capacity', 5);
  if (LIBRARY_ERROR_PREFIXES.some((prefix) => text.startsWith(prefix))) {
    return status === 500 && text.startsWith('Error proxying')
      ? jsonError(502, 'upstream_unavailable', 2)
      : jsonError(503, 'container_unavailable', 5);
  }
  return response;
}
