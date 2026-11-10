/**
 * Readiness is an actual HTTP health check, never `running === true` and never a
 * bare TCP connect: the Containers helper's port check accepts ANY response.
 */
export interface ReadinessOptions {
  /** Performs one probe request against GET /readyz/. */
  probe: () => Promise<Response>;
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

/** Polls until the app answers 200 {"status":"ok"} or the budget is exhausted. */
export async function waitUntilReady(options: ReadinessOptions): Promise<{ ready: boolean; attempts: number }> {
  const deadline = options.now() + options.budgetMs;
  let attempts = 0;
  for (;;) {
    attempts += 1;
    try {
      if (await isHealthyResponse(await options.probe())) return { ready: true, attempts };
    } catch {
      // not listening yet / connection reset: retry until the budget ends
    }
    if (options.now() + options.intervalMs >= deadline) return { ready: false, attempts };
    await options.sleep(options.intervalMs);
  }
}
