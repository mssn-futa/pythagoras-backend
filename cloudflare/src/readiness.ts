/**
 * Readiness is an actual HTTP health check, never `running === true` and never a
 * bare TCP connect: the Containers helper's port check accepts ANY response.
 */
export interface ReadinessOptions {
  /** Performs one probe request against GET /readyz/; must honour the signal where it can. */
  probe: (signal: AbortSignal) => Promise<Response>;
  /** Hard per-attempt bound: a hung probe cannot exceed the overall budget. */
  attemptTimeoutMs: number;
  now: () => number;
  sleep: (ms: number) => Promise<void>;
  budgetMs: number;
  intervalMs: number;
}

export async function isHealthyResponse(response: Response): Promise<boolean> {
  if (response.status !== 200) return false;
  try {
    const body = (await response.json()) as { status?: unknown };
    return body.status === 'ok';
  } catch {
    return false;
  }
}

async function attempt(options: ReadinessOptions, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error('attempt_timeout'));
    }, timeoutMs);
  });
  try {
    // Race so a probe that ignores the signal is still bounded.
    return await Promise.race([options.probe(controller.signal), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** Polls until the app answers 200 {"status":"ok"} or the budget is exhausted. */
export async function waitUntilReady(options: ReadinessOptions): Promise<{ ready: boolean; attempts: number }> {
  const deadline = options.now() + options.budgetMs;
  let attempts = 0;
  for (;;) {
    attempts += 1;
    try {
      if (await isHealthyResponse(await attempt(options, Math.max(1, Math.min(options.attemptTimeoutMs, deadline - options.now()))))) return { ready: true, attempts };
    } catch {
      // not listening yet / connection reset: retry until the budget ends
    }
    if (options.now() + options.intervalMs >= deadline) return { ready: false, attempts };
    await options.sleep(options.intervalMs);
  }
}
