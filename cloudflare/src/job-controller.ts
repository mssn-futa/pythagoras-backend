/**
 * Singleton job coordinator logic (pure; Cloudflare objects are injected as ports).
 *
 * Cron fires every minute. Each tick:
 *   - if the container is actually running -> ATTACH (no second start), force-stopping
 *     it first if it is older than budget + grace;
 *   - else reconcile the previous run from container state, then start the fixed
 *     `job-probe` role once and persist a run record BEFORE starting.
 *
 * This is coordination, NOT exactly-once correctness: ticks can be missed or
 * duplicated by the platform, and a coordinator restart can lose in-memory state.
 * Database uniqueness/leases remain the correctness authority for real jobs.
 *
 * Exit attribution (@cloudflare/containers 0.3.7): `onStop` is delivered LAZILY (from
 * an alarm, stop() or startAndWaitForPorts()) and, when the library's monitor callback
 * has not yet recorded the exit, it reports a default exitCode of 0 for a container
 * that is no longer running. So an exit is accepted only when
 *   1. the library state is `stopped_with_code` (the code is authoritative), and
 *   2. that state change happened at or after the current run's start, and
 *   3. the current run is `running` (not `starting`, not already terminal).
 * Anything else is left for the next tick's reconciliation, never guessed as success.
 */
import { JOB_BUDGET_SECONDS, JOB_KILL_GRACE_SECONDS, type Role } from './config';
import { errorName, type Logger } from './redact';

export type ContainerStatus = 'running' | 'healthy' | 'stopping' | 'stopped' | 'stopped_with_code';

export interface ContainerSnapshot {
  status: ContainerStatus;
  exitCode?: number;
  /** Epoch ms of the library's last state transition. */
  lastChange: number;
}

export interface ContainerPort {
  getState(): Promise<ContainerSnapshot>;
  /** True only while the container process actually exists (library state can lag). */
  isRunning(): boolean;
  /** Start the image with exactly this env. No entrypoint override is ever passed. */
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
  /** Returns the fixed env for the `job-probe` role. */
  envForJob: () => Record<string, string>;
  /** Names (never values) of missing required secrets. */
  missingSecrets: () => string[];
}

const RUN_KEY = 'job:run';
const HISTORY_KEY = 'job:history';
const HISTORY_LIMIT = 20;
const IN_PROGRESS: readonly RunStatus[] = ['starting', 'running'];

/** Every status except starting/running is terminal and is never overwritten. */
export function isTerminal(status: RunStatus): boolean {
  return !IN_PROGRESS.includes(status);
}

export class JobController {
  /** In-process mutex: one Durable Object instance is single-threaded but `await`s interleave. */
  private chain: Promise<unknown> = Promise.resolve();
  private seq = 0;

  constructor(private readonly deps: JobControllerDeps) {}

  /** Fixed entry point. Takes no arguments: nothing about the command is caller-controlled. */
  tick(): Promise<TickResult> {
    return this.serial(() => this.doTick());
  }

  /** Called from the container's (lazy) stop hook; reads the library state itself. */
  handleStop(): Promise<RunRecord | undefined> {
    return this.serial(() => this.doHandleStop());
  }

  async lastRun(): Promise<RunRecord | undefined> {
    return this.deps.store.get<RunRecord>(RUN_KEY);
  }

  private serial<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.chain.then(fn);
    this.chain = run.catch(() => undefined);
    return run;
  }

  private async doHandleStop(): Promise<RunRecord | undefined> {
    const { container, store, log } = this.deps;
    const run = await store.get<RunRecord>(RUN_KEY);
    if (!run || run.status !== 'running') return run; // starting / terminal / none: ignore
    const state = await container.getState();
    if (container.isRunning() || state.status !== 'stopped_with_code' || state.exitCode === undefined) {
      log('job_exit_unconfirmed', { runId: run.id, state: state.status });
      return run; // not authoritative: next tick reconciles
    }
    if (state.lastChange < run.startedAt) {
      log('job_exit_ignored_stale', { runId: run.id });
      return run; // belongs to an earlier container run
    }
    const updated: RunRecord = {
      ...run,
      status: state.exitCode === 0 ? 'succeeded' : 'failed',
      exitCode: state.exitCode,
      endedAt: this.deps.now(),
    };
    await this.finish(updated);
    log(state.exitCode === 0 ? 'job_run_succeeded' : 'job_run_failed', {
      runId: updated.id,
      exitCode: state.exitCode,
      durationMs: (updated.endedAt ?? 0) - updated.startedAt,
    });
    return updated;
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

    if (container.isRunning()) {
      const inProgress = !run || !isTerminal(run.status);
      if (!run) {
        run = { id: this.newId(), role: 'job-probe', startedAt: Math.min(state.lastChange, now()), status: 'running' };
        await store.put(RUN_KEY, run); // adopt an unrecorded active container, aged by its state
      }
      // Overdue check applies whenever the container is active, whatever the record says.
      const basis = inProgress ? run.startedAt : Math.min(state.lastChange, now());
      const ageMs = now() - basis;
      if (ageMs > (JOB_BUDGET_SECONDS + JOB_KILL_GRACE_SECONDS) * 1000) {
        log('job_run_overdue_killed', { runId: run.id, ageMs });
        await container.destroy();
        if (inProgress) {
          await this.finish({ ...run, status: 'killed_overdue', exitCode: null, endedAt: now() });
        }
        return { outcome: 'killed_overdue', runId: run.id };
      }
      log('job_tick_attached', { runId: run.id, ageMs });
      return { outcome: 'attached', runId: run.id };
    }

    // Container is not running. Reconcile a run whose exit was never recorded.
    let reconciled: RunStatus | undefined;
    if (run && !isTerminal(run.status)) {
      const authoritative =
        state.status === 'stopped_with_code' && state.exitCode !== undefined && state.lastChange >= run.startedAt;
      const exitCode = authoritative ? (state.exitCode as number) : null;
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
    // Promote starting -> running only if nothing terminal was written meanwhile.
    const current = await store.get<RunRecord>(RUN_KEY);
    if (current && current.id === record.id && current.status === 'starting') {
      await store.put(RUN_KEY, { ...record, status: 'running' } satisfies RunRecord);
    }
    log('job_run_started', { runId: record.id, role: record.role, budgetSeconds: JOB_BUDGET_SECONDS });
    return { outcome: 'started', runId: record.id, reconciled };
  }

  /** Persist a terminal record once, and append it to the bounded history. */
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
