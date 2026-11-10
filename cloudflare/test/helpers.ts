import type { ContainerPort, ContainerStatus, StorePort } from '../src/job-controller';

export class FakeContainer implements ContainerPort {
  status: ContainerStatus = 'stopped';
  exitCode: number | undefined;
  starts: Array<Record<string, string>> = [];
  destroyed = 0;
  failStart = false;
  startDelay: Promise<void> | undefined;

  async getState() {
    return { status: this.status, exitCode: this.exitCode };
  }
  async start(options: { envVars: Record<string, string> }) {
    await this.startDelay;
    if (this.failStart) throw new Error('boom postgresql://u:topsecret@h/db');
    this.starts.push(options.envVars);
    this.status = 'running';
  }
  async destroy() {
    this.destroyed += 1;
    this.status = 'stopped';
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
