import { describe, expect, it } from 'vitest';
import { JobController, isTerminal, type RunRecord } from '../src/job-controller';
import { containerEnvFor, JOB_BUDGET_SECONDS, JOB_KILL_GRACE_SECONDS } from '../src/config';
import { makeLogger } from '../src/redact';
import { Clock, LibraryFakeContainer, MemoryStore } from './helpers';

const SECRETS = { SECRET_KEY: 'sk-very-secret-value', DATABASE_URL: 'postgresql://u:pw-secret@db.example/app' };
const OVERDUE_MS = (JOB_BUDGET_SECONDS + JOB_KILL_GRACE_SECONDS) * 1000;

function setup(env: Record<string, unknown> = SECRETS) {
  const clock = new Clock();
  const container = new LibraryFakeContainer(clock);
  const store = new MemoryStore();
  const lines: string[] = [];
  const controller = new JobController({
    container,
    store,
    now: clock.now,
    log: makeLogger(() => Object.values(SECRETS), (line) => lines.push(line)),
    envForJob: () => containerEnvFor('job-probe', env),
    missingSecrets: () => ['SECRET_KEY', 'DATABASE_URL'].filter((n) => !env[n]),
  });
  const history = () => (store.data.get('job:history') as RunRecord[] | undefined) ?? [];
  const run = () => store.data.get('job:run') as RunRecord;
  return { clock, container, store, lines, controller, history, run, advance: (ms: number) => clock.advance(ms) };
}

describe('start and overlap', () => {
  it('starts the fixed job-probe role exactly once with only database env', async () => {
    const { container, controller } = setup();
    const result = await controller.tick();
    expect(result.outcome).toBe('started');
    expect(container.starts).toHaveLength(1);
    expect(Object.keys(container.starts[0]).sort()).toEqual(
      ['DATABASE_URL', 'JOB_PROBE_BUDGET_SECONDS', 'PORT', 'PYTHAGORAS_ROLE', 'SECRET_KEY'],
    );
    expect(container.starts[0].PYTHAGORAS_ROLE).toBe('job-probe');
  });

  it('attaches to an active run instead of starting a second', async () => {
    const { container, controller, advance } = setup();
    const first = await controller.tick();
    advance(60_000);
    const second = await controller.tick();
    expect(second).toMatchObject({ outcome: 'attached', runId: first.runId });
    expect(container.starts).toHaveLength(1);
  });

  it('serialises truly concurrent ticks: one start, others attach', async () => {
    const { container, controller } = setup();
    let release!: () => void;
    container.startDelay = new Promise((resolve) => (release = resolve));
    const ticks = [controller.tick(), controller.tick(), controller.tick()];
    release();
    const results = await Promise.all(ticks);
    expect(container.starts).toHaveLength(1);
    expect(results.map((r) => r.outcome).sort()).toEqual(['attached', 'attached', 'started']);
  });

  it('adopts an active container whose record was lost instead of starting another', async () => {
    const { container, controller, clock } = setup();
    container.running = true;
    container.state = { status: 'running', lastChange: clock.now() };
    expect((await controller.tick()).outcome).toBe('attached');
    expect(container.starts).toHaveLength(0);
  });
});

describe('exit recording', () => {
  it('records a monitored success and starts a fresh run next tick', async () => {
    const { container, controller, advance, history } = setup();
    const first = await controller.tick();
    advance(5_000);
    container.exit(0);
    expect((await controller.handleStop())?.status).toBe('succeeded');
    advance(60_000);
    const next = await controller.tick();
    expect(next.outcome).toBe('started');
    expect(next.runId).not.toBe(first.runId);
    expect(history()).toHaveLength(1);
  });

  it('records a monitored failure with its exit code and logs it', async () => {
    const { container, controller, advance, lines } = setup();
    await controller.tick();
    advance(1000);
    container.exit(1);
    expect(await controller.handleStop()).toMatchObject({ status: 'failed', exitCode: 1 });
    expect(lines.some((l) => l.includes('job_run_failed') && l.includes('"exitCode":1'))).toBe(true);
  });

  it('handleStop is idempotent for duplicate stop events', async () => {
    const { container, controller, advance, history } = setup();
    await controller.tick();
    advance(1000);
    container.exit(0);
    await controller.handleStop();
    container.state = { ...container.state, exitCode: 7 };
    expect((await controller.handleStop())?.status).toBe('succeeded');
    expect(history()).toHaveLength(1);
  });

  it('never reports success from the library default exit code 0 when the monitor has not recorded the exit', async () => {
    const { container, controller, advance, lines, run } = setup();
    await controller.tick();
    advance(1000);
    container.exit(1, { monitorRecorded: false }); // state stale 'running'; lazy onStop would say 0
    await controller.handleStop();
    expect(run().status).toBe('running');
    expect(lines.join('\n')).toContain('job_exit_unconfirmed');
    advance(60_000);
    const next = await controller.tick();
    expect(next.reconciled).toBe('lost'); // honest "unknown", not 'succeeded'
    expect(next.outcome).toBe('started');
  });

  it('lazy exit of run A delivered after run B started is not applied to B', async () => {
    const { container, controller, advance, run, history } = setup();
    const a = await controller.tick();
    advance(1000);
    container.exit(1); // A fails; onStop for A is still pending (lazy)
    advance(60_000);
    const tickB = await controller.tick(); // reconciles A from state, starts B
    expect(tickB).toMatchObject({ outcome: 'started', reconciled: 'failed' });
    advance(1000);
    await controller.handleStop(); // A's pending stop arrives now, container B is running
    expect(run().id).toBe(tickB.runId);
    expect(run().status).toBe('running');
    expect(history().map((h) => [h.id, h.status])).toEqual([[a.runId, 'failed']]);
  });

  it('lazy exit of run A arriving after B also exited is attributed to B, using B\'s own state', async () => {
    const { container, controller, advance, run } = setup();
    await controller.tick();
    advance(1000);
    container.exit(1);
    advance(60_000);
    await controller.tick(); // B starts
    advance(1000);
    container.exit(0); // B succeeded
    await controller.handleStop(); // (stale A event) library state is B's
    expect(run()).toMatchObject({ status: 'succeeded', exitCode: 0 });
  });

  it('ignores a stop state older than the current run start', async () => {
    const { container, controller, advance, run, lines } = setup();
    await controller.tick();
    container.running = false;
    container.state = { status: 'stopped_with_code', exitCode: 1, lastChange: run().startedAt - 10 };
    advance(1000);
    await controller.handleStop();
    expect(run().status).toBe('running');
    expect(lines.join('\n')).toContain('job_exit_ignored_stale');
  });

  it('ignores an exit while the run is still starting', async () => {
    const { container, controller, store } = setup();
    let release!: () => void;
    container.startDelay = new Promise((resolve) => (release = resolve));
    const tick = controller.tick();
    await Promise.resolve();
    await store.put('job:run', { id: 'x', role: 'job-probe', startedAt: 1, status: 'starting' });
    container.state = { status: 'stopped_with_code', exitCode: 0, lastChange: 5_000_000 };
    const stop = controller.handleStop(); // queued behind the tick (serialised)
    release();
    await tick;
    await stop;
    expect(await controller.lastRun()).not.toMatchObject({ status: 'succeeded' });
  });

  it('a container that exits during start() records start_failed once and is not overwritten', async () => {
    const { container, controller, advance, history, run } = setup();
    container.exitDuringStart = 3;
    const result = await controller.tick();
    expect(result.outcome).toBe('start_failed');
    advance(1000);
    await controller.handleStop(); // stray lazy stop
    expect(run().status).toBe('start_failed');
    expect(history()).toHaveLength(1);
    container.exitDuringStart = undefined;
    advance(60_000);
    const next = await controller.tick();
    expect(next.outcome).toBe('started');
    expect(next.reconciled).toBeUndefined(); // start_failed is terminal: not reconciled again
    expect(history()).toHaveLength(1);
  });

  it('terminal statuses other than succeeded/failed are never overwritten or duplicated', () => {
    for (const s of ['succeeded', 'failed', 'start_failed', 'killed_overdue', 'lost'] as const) expect(isTerminal(s)).toBe(true);
    expect(isTerminal('starting')).toBe(false);
    expect(isTerminal('running')).toBe(false);
  });

  it('a lost run is finalised once', async () => {
    const { container, controller, advance, history } = setup();
    await controller.tick();
    container.running = false;
    container.state = { status: 'stopped', lastChange: 1 };
    advance(1000);
    await controller.tick(); // reconcile -> lost, start next
    container.exit(0);
    await controller.handleStop();
    expect(history().map((h) => h.status)).toEqual(['lost', 'succeeded']);
  });
});

describe('reconciliation and overdue', () => {
  it('reconciles an unrecorded exit from authoritative container state', async () => {
    const { container, controller, advance, history } = setup();
    await controller.tick();
    advance(1000);
    container.exit(3);
    advance(60_000);
    const result = await controller.tick();
    expect(result).toMatchObject({ reconciled: 'failed', outcome: 'started' });
    expect(history()[0]).toMatchObject({ status: 'failed', exitCode: 3 });
  });

  it('force-stops an overdue run, then starts a new one on the next tick', async () => {
    const { container, controller, advance, history } = setup();
    await controller.tick();
    advance(OVERDUE_MS + 1);
    expect((await controller.tick()).outcome).toBe('killed_overdue');
    expect(container.destroyed).toBe(1);
    expect(history().map((h) => h.status)).toEqual(['killed_overdue']);
    expect((await controller.tick()).outcome).toBe('started');
  });

  it('does not force-stop a run still inside its budget', async () => {
    const { container, controller, advance } = setup();
    await controller.tick();
    advance(OVERDUE_MS - 1);
    expect((await controller.tick()).outcome).toBe('attached');
    expect(container.destroyed).toBe(0);
  });

  it('applies the overdue check even when the stored record is already terminal', async () => {
    const { container, controller, advance, store } = setup();
    await controller.tick();
    // record says terminal, but a container is (still/again) active and old
    const record = (await controller.lastRun())!;
    await store.put('job:run', { ...record, status: 'failed', exitCode: 1, endedAt: record.startedAt });
    advance(OVERDUE_MS + 1);
    expect((await controller.tick()).outcome).toBe('killed_overdue');
    expect(container.destroyed).toBe(1);
  });

  it('applies the overdue check to an adopted unrecorded container based on its state age', async () => {
    const { container, controller, clock } = setup();
    container.running = true;
    container.state = { status: 'running', lastChange: clock.now() - OVERDUE_MS - 5 };
    expect((await controller.tick()).outcome).toBe('killed_overdue');
    expect(container.destroyed).toBe(1);
  });
});

describe('failures and secrecy', () => {
  it('a start failure is recorded without leaking the error message and retried next tick', async () => {
    const { container, controller, lines } = setup();
    container.failStart = true;
    expect((await controller.tick()).outcome).toBe('start_failed');
    expect(lines.join('\n')).toContain('job_start_failed');
    expect(lines.join('\n')).not.toContain('topsecret');
    container.failStart = false;
    expect((await controller.tick()).outcome).toBe('started');
  });

  it('refuses to start when required secrets are missing and logs names only', async () => {
    const { container, controller, lines } = setup({ SECRET_KEY: 'x'.repeat(10) });
    expect((await controller.tick()).outcome).toBe('misconfigured');
    expect(container.starts).toHaveLength(0);
    expect(lines.join('\n')).toContain('DATABASE_URL');
  });

  it('never writes secret values to logs across a full lifecycle', async () => {
    const { container, controller, lines, advance } = setup();
    await controller.tick();
    advance(1000);
    await controller.tick();
    container.exit(1);
    await controller.handleStop();
    await controller.tick();
    const all = lines.join('\n');
    for (const value of Object.values(SECRETS)) expect(all).not.toContain(value);
    expect(all).not.toContain('pw-secret');
  });

  it('exposes no argument-taking entry point', () => {
    const { controller } = setup();
    expect(controller.tick.length).toBe(0);
    expect(controller.handleStop.length).toBe(0);
  });
});
