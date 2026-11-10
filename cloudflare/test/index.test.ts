/**
 * Behavioural tests for the Workers glue in src/index.ts. `@cloudflare/containers` (which
 * imports the `cloudflare:workers` runtime module) is replaced by a small stand-in base
 * class so WebContainer / JobContainer logic runs on plain Node.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => {
  const state = {
    containerRunning: true,
    startCalls: 0,
    startAndWaitFails: false,
    readyzAnswers: [] as Array<() => Promise<Response>>,
    probeRequests: [] as Request[],
    proxied: [] as Request[],
    proxyResponse: () => new Response('{"ok":true}', { status: 200, headers: { 'content-type': 'application/json' } }),
    libState: { status: 'healthy', lastChange: 1 } as { status: string; exitCode?: number; lastChange: number },
  };
  class FakeBase {
    defaultPort?: number;
    envVars: Record<string, string> | undefined;
    constructor(public ctx: any, public env: any) {}
    async getState() {
      return { ...state.libState };
    }
    async startAndWaitForPorts() {
      state.startCalls += 1;
      if (state.startAndWaitFails) throw new Error('boom postgresql://u:pw@h/d');
    }
    async start() {
      state.startCalls += 1;
    }
    async destroy() {}
    async containerFetch(request: Request) {
      if (new URL(request.url).pathname === '/readyz/') {
        state.probeRequests.push(request);
        const answer = state.readyzAnswers.shift();
        return answer ? answer() : new Response('{"status":"unavailable"}', { status: 503 });
      }
      state.proxied.push(request);
      return state.proxyResponse();
    }
    async fetch(request: Request) {
      return this.containerFetch(request);
    }
  }
  return { state, FakeBase };
});

vi.mock('@cloudflare/containers', () => ({
  Container: h.FakeBase,
  getContainer: (ns: any, name: string) => ns.getByName(name),
}));

import worker, { JobContainer, WebContainer } from '../src/index';

const SECRETS = { SECRET_KEY: 'k'.repeat(24), DATABASE_URL: 'postgresql://u:pw-secret@db.example/app' };
const okReady = () => Promise.resolve(new Response('{"status": "ok"}', { status: 200 }));

class FastWeb extends WebContainer {
  constructor(ctx: any, env: any) {
    super(ctx, env);
    this.readyBudgetMs = 400;
    this.readyIntervalMs = 5;
    this.readyAttemptTimeoutMs = 50;
  }
}

function web(env: Record<string, unknown> = { ...SECRETS, PUBLIC_API_HOST: 'api.example.com' }) {
  return new FastWeb({}, env as any);
}
const req = () => new Request('https://api.example.com/accounts/me/');

beforeEach(() => {
  Object.assign(h.state, {
    containerRunning: true,
    startCalls: 0,
    startAndWaitFails: false,
    readyzAnswers: [],
    probeRequests: [],
    proxied: [],
    libState: { status: 'stopped', lastChange: 1 },
  });
  h.state.proxyResponse = () => new Response('{"ok":true}', { status: 200, headers: { 'content-type': 'application/json' } });
});

describe('WebContainer readiness gate', () => {
  it('never proxies traffic while /readyz/ keeps failing (port-open or running is not readiness)', async () => {
    h.state.libState = { status: 'healthy', lastChange: 1 }; // library says healthy; app says 503
    const response = await web().fetch(req());
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: 'container_not_ready' });
    expect(response.headers.get('retry-after')).toBe('5');
    expect(h.state.proxied).toHaveLength(0);
    expect(h.state.probeRequests.length).toBeGreaterThan(1);
  });

  it('proxies only after a real 200 {"status":"ok"} and probes with trusted-proxy headers', async () => {
    h.state.readyzAnswers = [() => Promise.reject(new Error('refused')), okReady];
    const response = await web().fetch(req());
    expect(response.status).toBe(200);
    expect(h.state.proxied).toHaveLength(1);
    const probe = h.state.probeRequests[0];
    expect(probe.headers.get('x-forwarded-proto')).toBe('https');
    expect(probe.headers.get('x-forwarded-host')).toBe('api.example.com');
  });

  it('does not re-probe on every request once ready and healthy', async () => {
    h.state.readyzAnswers = [okReady];
    h.state.libState = { status: 'healthy', lastChange: 1 };
    const instance = web();
    await instance.fetch(req());
    await instance.fetch(req());
    expect(h.state.probeRequests).toHaveLength(1);
    expect(h.state.proxied).toHaveLength(2);
  });

  it('bounds a hung readiness probe by the per-attempt timeout', async () => {
    h.state.readyzAnswers = [() => new Promise<Response>(() => {})];
    const started = Date.now();
    const response = await web().fetch(req());
    expect(response.status).toBe(503);
    expect(Date.now() - started).toBeLessThan(2000);
  });

  it('returns 503 without leaking when the container cannot start', async () => {
    h.state.startAndWaitFails = true;
    const response = await web().fetch(req());
    expect(response.status).toBe(503);
    expect(JSON.stringify(await response.json())).not.toContain('pw');
    expect(h.state.proxied).toHaveLength(0);
  });

  it('refuses to start anything when required secrets are missing', async () => {
    const response = await web({ PUBLIC_API_HOST: 'api.example.com', SECRET_KEY: 'x' }).fetch(req());
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: 'service_misconfigured' });
    expect(h.state.startCalls).toBe(0);
  });

  it('replaces raw library error text from the proxy hop', async () => {
    h.state.readyzAnswers = [okReady];
    h.state.proxyResponse = () =>
      new Response('Error proxying request to container: connect ECONNREFUSED 10.0.0.9', {
        status: 500,
        headers: { 'content-type': 'text/plain' },
      });
    const response = await web().fetch(req());
    expect(response.status).toBe(502);
    expect(JSON.stringify(await response.json())).not.toContain('10.0.0.9');
  });

  it('web container env carries web-only config, never a role override from requests', () => {
    const instance = web({ ...SECRETS, PUBLIC_API_HOST: 'api.example.com', ALLOWED_HOSTS: 'api.example.com' });
    expect(instance.envVars).toMatchObject({ PYTHAGORAS_ROLE: 'web', ALLOWED_HOSTS: 'api.example.com' });
  });
});

describe('JobContainer', () => {
  function job() {
    const store = new Map<string, unknown>();
    const ctx = {
      storage: { get: async (k: string) => store.get(k), put: async (k: string, v: unknown) => void store.set(k, v) },
      container: { running: false },
    };
    return { instance: new JobContainer(ctx as any, { ...SECRETS } as any), ctx, store };
  }

  it('has no HTTP surface: every request is 404 and nothing is proxied', async () => {
    const { instance } = job();
    for (const method of ['GET', 'POST']) {
      const response = await (instance.fetch as unknown as (r: Request) => Promise<Response>)(
        new Request('https://x/run?role=migrate', { method, body: method === 'POST' ? '{"cmd":"sh"}' : undefined }),
      );
      expect(response.status).toBe(404);
    }
    expect(h.state.proxied).toHaveLength(0);
  });

  it('tick() takes no arguments and starts exactly the fixed role', async () => {
    const { instance, ctx } = job();
    const spy = vi.spyOn(instance as any, 'start').mockImplementation(async () => void (ctx.container.running = true));
    const result = await instance.tick();
    expect(result.outcome).toBe('started');
    const options = spy.mock.calls[0][0] as { envVars: Record<string, string>; entrypoint?: unknown };
    expect(options.envVars.PYTHAGORAS_ROLE).toBe('job-probe');
    expect(options.entrypoint).toBeUndefined();
    expect(instance.tick.length).toBe(0);
  });

  it('onStop ignores the library default exit code 0 and does not record success', async () => {
    const { instance, ctx, store } = job();
    vi.spyOn(instance as any, 'start').mockImplementation(async () => void (ctx.container.running = true));
    await instance.tick();
    ctx.container.running = false;
    h.state.libState = { status: 'running', lastChange: Date.now() }; // monitor has not recorded the exit
    await (instance.onStop as unknown as () => Promise<void>)();
    expect((store.get('job:run') as { status: string }).status).toBe('running');
  });
});

describe('Worker entry', () => {
  it('routes public requests to the web container with sanitized headers, never the job binding', async () => {
    const seen: Request[] = [];
    const env = {
      WEB: { getByName: () => ({ fetch: async (r: Request) => (seen.push(r), new Response('ok')) }) },
      JOB: { getByName: () => { throw new Error('job binding must not be touched by fetch'); } },
    };
    const response = await worker.fetch(
      new Request('https://api.example.com/x', { headers: { SCRIPT_NAME: '/evil', 'X-Forwarded-Host': 'evil.example' } }),
      env as any,
    );
    expect(response.status).toBe(200);
    expect(seen[0].headers.get('script_name')).toBeNull();
    expect(seen[0].headers.get('x-forwarded-host')).toBe('api.example.com');
  });

  it('scheduled() ticks the singleton job coordinator', async () => {
    const tick = vi.fn(async () => ({ outcome: 'attached', runId: 'r1' }));
    const names: string[] = [];
    const env = { JOB: { getByName: (n: string) => (names.push(n), { tick }) } };
    await worker.scheduled({} as any, env as any);
    expect(names).toEqual(['job-singleton']);
    expect(tick).toHaveBeenCalledTimes(1);
  });
});
