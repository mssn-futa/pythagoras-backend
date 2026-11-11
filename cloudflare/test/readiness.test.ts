import { describe, expect, it } from 'vitest';
import { isHealthyResponse, waitUntilReady } from '../src/readiness';

function clock() {
  let t = 0;
  return { now: () => t, sleep: async (ms: number) => void (t += ms) };
}
const ok = () => new Response('{"status": "ok"}', { status: 200 });

describe('readiness is an actual HTTP health check', () => {
  it('requires 200 and {"status":"ok"}', async () => {
    expect(await isHealthyResponse(ok())).toBe(true);
    expect(await isHealthyResponse(new Response('{"status":"unavailable"}', { status: 503 }))).toBe(false);
    expect(await isHealthyResponse(new Response('{"status":"unavailable"}', { status: 200 }))).toBe(false);
    expect(await isHealthyResponse(new Response('<html>301</html>', { status: 200 }))).toBe(false);
  });

  it('retries through connection errors and 503s until healthy', async () => {
    const c = clock();
    const answers = [() => Promise.reject(new Error('refused')), () => Promise.resolve(new Response('{}', { status: 503 })), () => Promise.resolve(ok())];
    let i = 0;
    const result = await waitUntilReady({ probe: () => answers[i++](), ...c, budgetMs: 30_000, intervalMs: 1000, attemptTimeoutMs: 5000 });
    expect(result).toEqual({ ready: true, attempts: 3 });
  });

  it('bounds each attempt: a hung probe is aborted and cannot exceed the budget', async () => {
    let aborted = false;
    const started = Date.now();
    const result = await waitUntilReady({
      probe: (signal) =>
        new Promise<Response>(() => {
          signal.addEventListener('abort', () => (aborted = true)); // never resolves
        }),
      now: Date.now,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      budgetMs: 300,
      intervalMs: 20,
      attemptTimeoutMs: 100,
    });
    expect(result.ready).toBe(false);
    expect(aborted).toBe(true);
    expect(Date.now() - started).toBeLessThan(1000);
  });

  it('gives up when the budget is exhausted (a port that answers 5xx is never ready)', async () => {
    const c = clock();
    const result = await waitUntilReady({
      probe: async () => new Response('Bad Gateway', { status: 502 }),
      ...c,
      budgetMs: 5000,
      intervalMs: 1000,
      attemptTimeoutMs: 5000,
    });
    expect(result.ready).toBe(false);
    expect(result.attempts).toBeGreaterThan(1);
    expect(c.now()).toBeLessThanOrEqual(5000);
  });
});
