/**
 * Cloudflare glue (Worker entry + Container Durable Objects).
 *
 * All decision logic lives in the pure modules (routing, readiness, job-controller)
 * which are unit-tested without the Workers runtime.
 *
 * Surface:
 *   - fetch():     every public request is proxied to the `web` container. There is no
 *                  endpoint that starts a job, selects a role, or passes arguments.
 *   - scheduled(): the one-minute Cron Trigger calls the singleton JobContainer.tick().
 */
import { Container, getContainer } from '@cloudflare/containers';
import {
  JOB_BUDGET_SECONDS,
  JOB_KILL_GRACE_SECONDS,
  JOB_SINGLETON_NAME,
  WEB_INSTANCE_NAME,
  WEB_PORT,
  containerEnvFor,
  missingRequiredSecrets,
  secretValues,
  type EnvLike,
} from './config';
import { JobController, type ContainerStatus, type TickResult } from './job-controller';
import { makeLogger } from './redact';
import { waitUntilReady } from './readiness';
import { jsonError, probeHeaders, scrubProxyError, toContainerRequest } from './routing';

export interface Env extends EnvLike {
  WEB: DurableObjectNamespace<WebContainer>;
  JOB: DurableObjectNamespace<JobContainer>;
  /** Public API hostname the Worker is served on (non-secret var); used for the readiness probe. */
  PUBLIC_API_HOST: string;
}

/** Overall readiness budget; Neon/cold starts are slow, so generous but bounded. */
export const READY_BUDGET_MS = 45_000;
export const READY_INTERVAL_MS = 1_000;
/** Per-attempt bound. Must exceed the container's PGCONNECT_TIMEOUT (10 s) to see its 503. */
export const READY_ATTEMPT_TIMEOUT_MS = 12_000;

export class WebContainer extends Container<Env> {
  defaultPort = WEB_PORT;
  requiredPorts = [WEB_PORT];
  sleepAfter = '10m';
  enableInternet = true; // outbound PostgreSQL (TLS) and later Resend/R2
  private ready = false;
  /** Overridable for tests; production values are the exported constants. */
  protected readyBudgetMs = READY_BUDGET_MS;
  protected readyIntervalMs = READY_INTERVAL_MS;
  protected readyAttemptTimeoutMs = READY_ATTEMPT_TIMEOUT_MS;
  private readonly log = makeLogger(() => secretValues(this.env));

  constructor(ctx: ConstructorParameters<typeof Container>[0], env: Env) {
    super(ctx, env);
    this.envVars = containerEnvFor('web', env);
  }

  override async fetch(request: Request): Promise<Response> {
    const missing = missingRequiredSecrets(this.env);
    if (missing.length > 0) {
      this.log('web_misconfigured', { missing: missing.join(',') });
      return jsonError(503, 'service_misconfigured');
    }
    if (!(await this.ensureReady())) {
      return jsonError(503, 'container_not_ready', 5);
    }
    return scrubProxyError(await super.fetch(request));
  }

  /** Start if needed, then require a real HTTP 200 {"status":"ok"} from /readyz/ (DB included). */
  private async ensureReady(): Promise<boolean> {
    const state = await this.getState();
    const running = state.status === 'healthy' || state.status === 'running';
    if (this.ready && running) return true;
    this.ready = false;
    try {
      await this.startAndWaitForPorts({ ports: WEB_PORT });
    } catch {
      this.log('web_start_failed');
      return false;
    }
    const result = await waitUntilReady({
      probe: (signal) =>
        this.containerFetch(
          new Request('http://container.internal/readyz/', {
            headers: probeHeaders(this.env.PUBLIC_API_HOST),
            signal,
          }),
          WEB_PORT,
        ),
      now: Date.now,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      budgetMs: this.readyBudgetMs,
      intervalMs: this.readyIntervalMs,
      attemptTimeoutMs: this.readyAttemptTimeoutMs,
    });
    this.log(result.ready ? 'web_ready' : 'web_not_ready', { attempts: result.attempts });
    this.ready = result.ready;
    return result.ready;
  }

  override onStop(): void {
    this.ready = false;
    this.log('web_container_stopped');
  }

  override onError(error: unknown): void {
    this.log('web_container_error', { error: error instanceof Error ? error.name : 'NonError' });
  }
}

export class JobContainer extends Container<Env> {
  /** No HTTP port is exposed by the job role; the probe runs once and exits. */
  sleepAfter = `${JOB_BUDGET_SECONDS + JOB_KILL_GRACE_SECONDS + 60}s`; // backstop beyond the coordinator's kill
  enableInternet = true;
  private readonly log = makeLogger(() => secretValues(this.env));
  private readonly controller: JobController;

  constructor(ctx: ConstructorParameters<typeof Container>[0], env: Env) {
    super(ctx, env);
    this.controller = new JobController({
      container: {
        getState: async () => {
          const state = await this.getState();
          return {
            status: state.status as ContainerStatus,
            exitCode: 'exitCode' in state ? state.exitCode : undefined,
            lastChange: state.lastChange,
          };
        },
        isRunning: () => Boolean(ctx.container?.running),
        start: (options) => this.start({ envVars: options.envVars }),
        destroy: () => this.destroy(),
      },
      store: {
        get: <T>(key: string) => ctx.storage.get<T>(key),
        put: <T>(key: string, value: T) => ctx.storage.put(key, value),
      },
      now: Date.now,
      log: this.log,
      envForJob: () => containerEnvFor('job-probe', env),
      missingSecrets: () => missingRequiredSecrets(env),
    });
  }

  /** The only way work starts: an internal RPC from scheduled(). Takes no arguments. */
  tick(): Promise<TickResult> {
    return this.controller.tick();
  }

  /**
   * The library delivers this lazily and with a default exitCode of 0 when its monitor
   * has not yet recorded the exit, so the reported code is deliberately ignored: the
   * controller reads the authoritative state itself and validates it against the run.
   */
  override async onStop(): Promise<void> {
    await this.controller.handleStop();
  }

  override onError(error: unknown): void {
    this.log('job_container_error', { error: error instanceof Error ? error.name : 'NonError' });
  }

  /** No HTTP surface at all. */
  override async fetch(): Promise<Response> {
    return new Response(null, { status: 404 });
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    return getContainer(env.WEB, WEB_INSTANCE_NAME).fetch(toContainerRequest(request));
  },

  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    const result = await env.JOB.getByName(JOB_SINGLETON_NAME).tick();
    console.log(JSON.stringify({ event: 'cron_tick', outcome: result.outcome, runId: result.runId ?? null }));
  },
} satisfies ExportedHandler<Env>;
