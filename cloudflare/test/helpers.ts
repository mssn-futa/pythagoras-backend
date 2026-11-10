import type { ContainerPort, ContainerSnapshot, StorePort } from '../src/job-controller';

export class Clock {
  constructor(public t = 1_000_000) {}
  now = () => this.t;
  advance(ms: number) {
    this.t += ms;
  }
}

/**
 * Models the @cloudflare/containers 0.3.7 semantics that matter for exit tracking:
 *  - start() sets state `running` (new lastChange) and throws if the process exits early;
 *  - the library's monitor callback records `stopped_with_code` only some time after the
 *    process exits (`exit(code, {monitorRecorded:false})` leaves the stale `running`);
 *  - the lazy onStop hook is NOT modelled here: JobContainer.onStop ignores its argument
 *    and calls controller.handleStop(), which reads this state. Tests call handleStop()
 *    at the interleaving point of their choice.
 */
export class LibraryFakeContainer implements ContainerPort {
  running = false;
  state: ContainerSnapshot;
  starts: Array<Record<string, string>> = [];
  destroyed = 0;
  failStart = false;
  /** If set, the process dies during start() with this code and start() throws. */
  exitDuringStart: number | undefined;
  startDelay: Promise<void> | undefined;

  constructor(private readonly clock: Clock) {
    this.state = { status: 'stopped', lastChange: clock.now() };
  }

  async getState(): Promise<ContainerSnapshot> {
    return { ...this.state };
  }
  isRunning() {
    return this.running;
  }
  async start(options: { envVars: Record<string, string> }) {
    await this.startDelay;
    if (this.running) throw new Error('start() cannot be called on a container that is already running.');
    if (this.failStart) throw new Error('boom postgresql://u:topsecret@h/db');
    if (this.exitDuringStart !== undefined) {
      this.state = { status: 'stopped_with_code', exitCode: this.exitDuringStart, lastChange: this.clock.now() };
      throw new Error(`Container exited before we could determine the container health, exit code: ${this.exitDuringStart}`);
    }
    this.starts.push(options.envVars);
    this.running = true;
    this.state = { status: 'running', lastChange: this.clock.now() };
  }
  async destroy() {
    this.destroyed += 1;
    this.running = false;
    this.state = { status: 'stopped_with_code', exitCode: 137, lastChange: this.clock.now() };
  }
  /** The container process ends. By default the monitor has recorded it already. */
  exit(code: number, options: { monitorRecorded?: boolean } = {}) {
    this.running = false;
    if (options.monitorRecorded !== false) {
      this.state = { status: 'stopped_with_code', exitCode: code, lastChange: this.clock.now() };
    }
  }
}

export class MemoryStore implements StorePort {
  data = new Map<string, unknown>();
  async get<T>(key: string) {
    return this.data.get(key) as T | undefined;
  }
  async put<T>(key: string, value: T) {
    this.data.set(key, structuredClone(value));
  }
}
