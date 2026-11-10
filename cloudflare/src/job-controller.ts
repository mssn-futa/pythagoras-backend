/**
 * Singleton job coordinator logic (pure; Cloudflare objects are injected as ports).
 *
 * Cron fires every minute. Each tick:
 *   - if a container run is already active -> ATTACH (no second start);
 *   - else start the fixed `job-probe` role once and persist the run record;
 *   - exit status is recorded from the container's stop event (onStop).
 *
 * This is coordination, NOT exactly-once correctness: ticks can be missed or
 * duplicated by the platform, and a coordinator restart can lose in-memory state.
 * Database uniqueness/leases remain the correctness authority for real jobs.
 */
import { JOB_BUDGET_SECONDS, JOB_KILL_GRACE_SECONDS, type Role } from './config';
import { errorName, type Logger } from './redact';

export type ContainerStatus = 'running' | 'healthy' | 'stopping' | 'stopped' | 'stopped_with_code';

export interface ContainerPort {
  getState(): Promise<{ status: ContainerStatus; exitCode?: number }>;
  /** Start the image with exactly this role/env. No entrypoint override is ever passed. */
  start(options: { envVars: Record<string, string> }): Promise<void>;
  destroy(): Promise<void>;
}

export interface StorePort {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
}

export type RunStatus = 'starting' | 'running' | 'succeeded' | 'failed' | 'start_failed' | 'killed_overdue' | 'lost';

export interface RunRecord {
  id: string;
  role: Role;
  startedAt: number;
  status: RunStatus;
  endedAt?: number;
  exitCode?: number | null;
}

export type TickOutcome = 'started' | 'attached' | 'killed_overdue' | 'start_failed' | 'misconfigured';

export interface TickResult {
  outcome: TickOutcome;
  runId?: string;
  /** Terminal status of the previous run if this tick reconciled one. */
  reconciled?: RunStatus;
}

export interface JobControllerDeps {
  container: ContainerPort;
  store: StorePort;
  now: () => number;
  log: Logger;
  /** Returns the fixed env for the `job-probe` role, or throws if required secrets are missing. */
  envForJob: () => Record<string, string>;
  /** Names (never values) of missing required secrets. */
  missingSecrets: () => string[];
}

const RUN_KEY = 'job:run';
const HISTORY_KEY = 'job:history';
const HISTORY_LIMIT = 20;
const ACTIVE: readonly ContainerStatus[] = ['running', 'healthy', 'stopping'];

export class JobController {
  /** In-process mutex: one Durable Object instance is single-threaded but `await`s interleave. */
  private chain: Promise<unknown> = Promise.resolve();
  private seq = 0;

  constructor(private readonly deps: JobControllerDeps) {}

  /** Fixed entry point. Takes no arguments: nothing about the command is caller-controlled. */
  tick(): Promise<TickResult> {
    const run = this.chain.then(() => this.doTick());
    this.chain = run.catch(() => undefined);
    return run;
  }

  /** Called from the container's stop lifecycle hook with the monitored exit code. */
  recordExit(exitCode: number): Promise<RunRecord | undefined> {
    const done = this.chain.then(() => this.doRecordExit(exitCode));
    this.chain = done.catch(() => undefined);
    return done;
  }

  private async doRecordExit(exitCode: number): Promise<RunRecord | undefined> {
    const run = await this.deps.store.get<RunRecord>(RUN_KEY);
    if (!run || run.status === 'succeeded' || run.status === 'failed' || run.status === 'killed_overdue') {
      return run; // idempotent: a late/duplicate stop event changes nothing
    }
    const updated: RunRecord = {
      ...run,
      status: exitCode === 0 ? 'succeeded' : 'failed',
      exitCode,
      endedAt: this.deps.now(),
    };
    await this.finish(updated);
    const level = exitCode === 0 ? 'job_run_succeeded' : 'job_run_failed';
    this.deps.log(level, { runId: updated.id, exitCode, durationMs: (updated.endedAt ?? 0) - updated.startedAt });
    return updated;
  }

  async lastRun(): Promise<RunRecord | undefined> {
    return this.deps.store.get<RunRecord>(RUN_KEY);
  }

  private async doTick(): Promise<TickResult> {
    const { container, store, now, log } = this.deps;

    const missing = this.deps.missingSecrets();
    if (missing.length > 0) {
      log('job_misconfigured', { missing: missing.join(',') }); // names only
      return { outcome: 'misconfigured' };
    }

    const state = await container.getState();
    let run = await store.get<RunRecord>(RUN_KEY);

    if (ACTIVE.includes(state.status)) {
      if (!run || run.status === 'starting' || run.status === 'running') {
        if (!run) {
          // Active container with no record (coordinator state lost): adopt it.
          run = { id: this.newId(), role: 'job-probe', startedAt: now(), status: 'running' };
          await store.put(RUN_KEY, run);
        }
        const ageMs = now() - run.startedAt;
        if (ageMs > (JOB_BUDGET_SECONDS + JOB_KILL_GRACE_SECONDS) * 1000) {
          log('job_run_overdue_killed', { runId: run.id, ageMs });
          await container.destroy();
          await this.finish({ ...run, status: 'killed_overdue', exitCode: null, endedAt: now() });
          return { outcome: 'killed_overdue', runId: run.id };
        }
        log('job_tick_attached', { runId: run.id, ageMs });
        return { outcome: 'attached', runId: run.id };
      }
      // Stale terminal record yet container active: another start raced us; attach, do not start.
      log('job_tick_attached', { runId: run.id });
      return { outcome: 'attached', runId: run.id };
    }

    // Container is not running. Reconcile a run that never recorded its exit.
    let reconciled: RunStatus | undefined;
    if (run && (run.status === 'starting' || run.status === 'running')) {
      const exitCode = state.status === 'stopped_with_code' ? (state.exitCode ?? null) : null;
      const status: RunStatus = exitCode === null ? 'lost' : exitCode === 0 ? 'succeeded' : 'failed';
      await this.finish({ ...run, status, exitCode, endedAt: now() });
      log('job_run_reconciled', { runId: run.id, status, exitCode });
      reconciled = status;
    }

    const record: RunRecord = { id: this.newId(), role: 'job-probe', startedAt: now(), status: 'starting' };
    await store.put(RUN_KEY, record);
    try {
      await container.start({ envVars: this.deps.envForJob() });
    } catch (error) {
      await this.finish({ ...record, status: 'start_failed', exitCode: null, endedAt: now() });
      log('job_start_failed', { runId: record.id, error: errorName(error) });
      return { outcome: 'start_failed', runId: record.id, reconciled };
    }
    // The job may already have exited and been recorded by recordExit(); never regress that.
    const current = await store.get<RunRecord>(RUN_KEY);
    if (current && current.id === record.id && current.status === 'starting') {
      await store.put(RUN_KEY, { ...record, status: 'running' } satisfies RunRecord);
    }
    log('job_run_started', { runId: record.id, role: record.role, budgetSeconds: JOB_BUDGET_SECONDS });
    return { outcome: 'started', runId: record.id, reconciled };
  }

  private async finish(run: RunRecord): Promise<void> {
    await this.deps.store.put(RUN_KEY, run);
    const history = (await this.deps.store.get<RunRecord[]>(HISTORY_KEY)) ?? [];
    history.push(run);
    await this.deps.store.put(HISTORY_KEY, history.slice(-HISTORY_LIMIT));
  }

  private newId(): string {
    this.seq += 1;
    return `${this.deps.now()}-${this.seq}`;
  }
}
