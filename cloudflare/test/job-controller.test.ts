import { describe, expect, it } from 'vitest';
import { JobController, type RunRecord } from '../src/job-controller';
import { containerEnvFor, JOB_BUDGET_SECONDS, JOB_KILL_GRACE_SECONDS } from '../src/config';
import { makeLogger } from '../src/redact';
import { FakeContainer, MemoryStore } from './helpers';

const SECRETS = { SECRET_KEY: 'sk-very-secret-value', DATABASE_URL: 'postgresql://u:pw-secret@db.example/app' };

function setup(env: Record<string, unknown> = SECRETS) {
  const container = new FakeContainer();
  const store = new MemoryStore();
  const lines: string[] = [];
  let clock = 1_000_000;
  const controller = new JobController({
    container,
    store,
    now: () => clock,
    log: makeLogger(() => Object.values(SECRETS), (line) => lines.push(line)),
    envForJob: () => containerEnvFor('job-probe', env),
    missingSecrets: () => ['SECRET_KEY', 'DATABASE_URL'].filter((n) => !env[n]),
  });
  return { container, store, lines, controller, advance: (ms: number) => (clock += ms) };
}

describe('job controller', () => {
  it('starts the fixed job-probe role exactly once with only database env', async () => {
    const { container, controller } = setup();
    const result = await controller.tick();
    expect(result.outcome).toBe('started');
    expect(container.starts).toHaveLength(1);
    expect(container.starts[0].PYTHAGORAS_ROLE).toBe('job-probe');
    expect(container.starts[0].JOB_PROBE_BUDGET_SECONDS).toBe(String(JOB_BUDGET_SECONDS));
    expect(Object.keys(container.starts[0]).sort()).toEqual(
      ['DATABASE_URL', 'JOB_PROBE_BUDGET_SECONDS', 'PORT', 'PYTHAGORAS_ROLE', 'SECRET_KEY'],
    );
  });

  it('attaches to an active run instead of starting a second (overlapping ticks)', async () => {
    const { container, controller, advance } = setup();
    const first = await controller.tick();
    advance(60_000);
    const second = await controller.tick();
    expect(second.outcome).toBe('attached');
    expect(second.runId).toBe(first.runId);
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

  it('records a monitored successful exit and starts a fresh run on the next tick', async () => {
    const { container, controller, advance } = setup();
    const first = await controller.tick();
    container.status = 'stopped_with_code';
    container.exitCode = 0;
    const exit = await controller.recordExit(0);
    expect(exit?.status).toBe('succeeded');
    advance(60_000);
    const next = await controller.tick();
    expect(next.outcome).toBe('started');
    expect(next.runId).not.toBe(first.runId);
    expect(container.starts).toHaveLength(2);
  });

  it('records a monitored failure with its exit code and logs it', async () => {
    const { container, controller, lines } = setup();
    await controller.tick();
    container.status = 'stopped_with_code';
    container.exitCode = 1;
    const exit = await controller.recordExit(1);
    expect(exit).toMatchObject({ status: 'failed', exitCode: 1 });
    expect(lines.some((l) => l.includes('job_run_failed') && l.includes('"exitCode":1'))).toBe(true);
  });

  it('recordExit is idempotent for duplicate stop events', async () => {
    const { controller, store } = setup();
    await controller.tick();
    await controller.recordExit(0);
    const again = await controller.recordExit(7);
    expect(again?.status).toBe('succeeded');
    expect((store.data.get('job:history') as RunRecord[]).length).toBe(1);
  });

  it('reconciles a run whose exit was never recorded (coordinator restart) from container state', async () => {
    const { container, controller, store } = setup();
    await controller.tick();
    container.status = 'stopped_with_code';
    container.exitCode = 3;
    const result = await controller.tick();
    expect(result.reconciled).toBe('failed');
    expect(result.outcome).toBe('started');
    const history = store.data.get('job:history') as RunRecord[];
    expect(history[0]).toMatchObject({ status: 'failed', exitCode: 3 });
  });

  it('marks an unknown vanished run as lost and still starts a new one', async () => {
    const { container, controller } = setup();
    await controller.tick();
    container.status = 'stopped';
    const result = await controller.tick();
    expect(result.reconciled).toBe('lost');
    expect(result.outcome).toBe('started');
  });

  it('force-stops an overdue run, then starts a new one on the next tick', async () => {
    const { container, controller, advance } = setup();
    await controller.tick();
    advance((JOB_BUDGET_SECONDS + JOB_KILL_GRACE_SECONDS) * 1000 + 1);
    const killed = await controller.tick();
    expect(killed.outcome).toBe('killed_overdue');
    expect(container.destroyed).toBe(1);
    const next = await controller.tick();
    expect(next.outcome).toBe('started');
  });

  it('does not force-stop a run still inside its budget', async () => {
    const { container, controller, advance } = setup();
    await controller.tick();
    advance((JOB_BUDGET_SECONDS + JOB_KILL_GRACE_SECONDS) * 1000 - 1);
    expect((await controller.tick()).outcome).toBe('attached');
    expect(container.destroyed).toBe(0);
  });

  it('adopts an active container whose record was lost instead of starting another', async () => {
    const { container, controller } = setup();
    container.status = 'running';
    const result = await controller.tick();
    expect(result.outcome).toBe('attached');
    expect(container.starts).toHaveLength(0);
  });

  it('a start failure is recorded without leaking the error message', async () => {
    const { container, controller, lines } = setup();
    container.failStart = true;
    const result = await controller.tick();
    expect(result.outcome).toBe('start_failed');
    expect(lines.join('\n')).toContain('job_start_failed');
    expect(lines.join('\n')).not.toContain('topsecret');
    container.failStart = false;
    expect((await controller.tick()).outcome).toBe('started');
  });

  it('refuses to start when required secrets are missing and logs names only', async () => {
    const { container, controller, lines } = setup({ SECRET_KEY: 'x'.repeat(10) });
    const result = await controller.tick();
    expect(result.outcome).toBe('misconfigured');
    expect(container.starts).toHaveLength(0);
    expect(lines.join('\n')).toContain('DATABASE_URL');
  });

  it('never writes secret values to logs across a full lifecycle', async () => {
    const { container, controller, lines, advance } = setup();
    await controller.tick();
    advance(1000);
    await controller.tick();
    container.status = 'stopped_with_code';
    await controller.recordExit(1);
    await controller.tick();
    const all = lines.join('\n');
    for (const value of Object.values(SECRETS)) expect(all).not.toContain(value);
    expect(all).not.toContain('pw-secret');
  });

  it('exposes no argument-taking entry point', () => {
    const { controller } = setup();
    expect(controller.tick.length).toBe(0);
  });
});
