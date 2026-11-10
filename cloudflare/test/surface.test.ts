import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

// Structural guard: the Workers-runtime entry cannot be imported on plain Node, so
// assert on its source that the public fetch() path never touches the job binding.
const source = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8');
const exported = source.slice(source.indexOf('export default {'));
const fetchBody = exported.slice(exported.indexOf('async fetch('), exported.indexOf('async scheduled('));

describe('public surface', () => {
  it('public fetch only routes to the web container', () => {
    expect(fetchBody).toContain('env.WEB');
    expect(fetchBody).not.toMatch(/env\.JOB|tick\(|JOB_SINGLETON_NAME|scheduled/);
  });

  it('job container has no HTTP surface and the only job entry is scheduled()', () => {
    expect(source).toMatch(/class JobContainer[\s\S]*override async fetch\(\): Promise<Response> \{\s*return new Response\(null, \{ status: 404 \}\)/);
    expect(exported.match(/\.tick\(\)/g)).toHaveLength(1);
    expect(exported.indexOf('.tick()')).toBeGreaterThan(exported.indexOf('async scheduled('));
  });

  it('never passes an entrypoint override or request-derived values to start()', () => {
    expect(source).not.toMatch(/entrypoint/);
    expect(source).not.toMatch(/request\.(url|headers|json|text)[\s\S]{0,40}start\(/);
  });
});
