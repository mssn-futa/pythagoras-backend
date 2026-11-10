import { describe, expect, it } from 'vitest';
import { jsonError, probeHeaders, scrubProxyError, toContainerRequest } from '../src/routing';

describe('toContainerRequest (trusted proxy boundary)', () => {
  it('replaces client-supplied forwarding headers with values from the verified URL', () => {
    const request = new Request('https://api.example.com/accounts/login/?x=1', {
      method: 'POST',
      headers: {
        'X-Forwarded-Host': 'evil.example',
        'X-Forwarded-Proto': 'http',
        'X-Forwarded-For': '6.6.6.6',
        Forwarded: 'for=6.6.6.6;host=evil.example',
        'X-Real-IP': '6.6.6.6',
        'CF-Container-Target-Port': '22',
        'CF-Connecting-IP': '203.0.113.9',
        Authorization: 'Bearer abc',
        'Content-Type': 'application/json',
      },
      body: '{}',
    });
    const out = toContainerRequest(request);
    expect(out.headers.get('x-forwarded-host')).toBe('api.example.com');
    expect(out.headers.get('x-forwarded-proto')).toBe('https');
    expect(out.headers.get('x-forwarded-for')).toBe('203.0.113.9');
    expect(out.headers.get('forwarded')).toBeNull();
    expect(out.headers.get('x-real-ip')).toBeNull();
    expect(out.headers.get('cf-container-target-port')).toBeNull();
    expect(out.headers.get('authorization')).toBe('Bearer abc');
    expect(out.method).toBe('POST');
    expect(new URL(out.url).pathname + new URL(out.url).search).toBe('/accounts/login/?x=1');
  });

  it('drops a spoofed X-Forwarded-For when Cloudflare supplied no client IP', () => {
    const out = toContainerRequest(new Request('https://api.example.com/', { headers: { 'X-Forwarded-For': '1.2.3.4' } }));
    expect(out.headers.get('x-forwarded-for')).toBeNull();
  });
});

describe('probe + errors', () => {
  it('readiness probe uses the same trust contract as real traffic', () => {
    const headers = probeHeaders('api.example.com');
    expect(headers.get('x-forwarded-proto')).toBe('https');
    expect(headers.get('x-forwarded-host')).toBe('api.example.com');
  });

  it('jsonError is fixed-shape and uncached', async () => {
    const response = jsonError(503, 'container_not_ready', 5);
    expect(response.status).toBe(503);
    expect(response.headers.get('retry-after')).toBe('5');
    expect(await response.json()).toEqual({ error: 'container_not_ready' });
  });

  it('replaces raw proxy exception text with a fixed 502', async () => {
    const leaky = new Response('Error proxying request to container: connect ECONNREFUSED 10.0.0.1', {
      status: 500,
      headers: { 'content-type': 'text/plain' },
    });
    const out = await scrubProxyError(leaky);
    expect(out.status).toBe(502);
    expect(JSON.stringify(await out.json())).not.toContain('10.0.0.1');
  });

  it('passes through genuine application 500s and non-500s untouched', async () => {
    const app500 = new Response('{"success":false}', { status: 500, headers: { 'content-type': 'application/json' } });
    expect(await scrubProxyError(app500)).toBe(app500);
    const ok = new Response('fine');
    expect(await scrubProxyError(ok)).toBe(ok);
  });
});
