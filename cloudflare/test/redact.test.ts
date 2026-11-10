import { describe, expect, it } from 'vitest';
import { errorName, makeLogger, scrub } from '../src/redact';

describe('redaction', () => {
  it('replaces secret values and URL credentials', () => {
    expect(scrub('key=abcd1234 end', ['abcd1234'])).toBe('key=[redacted] end');
    expect(scrub('failed postgresql://user:hunter2@db.example:5432/x', [])).toBe('failed postgresql://[redacted]@db.example:5432/x');
  });

  it('logger scrubs emitted lines', () => {
    const lines: string[] = [];
    const log = makeLogger(() => ['s3cr3t-value'], (l) => lines.push(l));
    log('event', { note: 'contains s3cr3t-value here' });
    expect(lines[0]).not.toContain('s3cr3t-value');
  });

  it('error helper never exposes messages', () => {
    expect(errorName(new TypeError('postgresql://u:p@h/d'))).toBe('TypeError');
    expect(errorName('string')).toBe('NonError');
  });
});
