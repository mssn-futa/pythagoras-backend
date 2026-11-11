import { describe, expect, it } from 'vitest';
import {
  containerEnvFor,
  isAllowedRole,
  missingRequiredSecrets,
  OUTBOX_ROLE,
  ROLES,
  secretValues,
  type Role,
} from '../src/config';

describe('role allow-list', () => {
  it('only permits web, job-probe and outbox-once; migrate and arbitrary roles are rejected', () => {
    expect([...ROLES]).toEqual(['web', 'job-probe', 'outbox-once']);
    expect(OUTBOX_ROLE).toBe('outbox-once');
    for (const bad of ['migrate', 'sh', 'python manage.py migrate', '', 'WEB', undefined, null, 5]) {
      expect(isAllowedRole(bad)).toBe(false);
    }
    expect(() => containerEnvFor('migrate' as Role, {})).toThrow();
  });
});

describe('container env', () => {
  const env = {
    SECRET_KEY: 'k'.repeat(20),
    DATABASE_URL: 'postgresql://u:p@h/d',
    RESEND_API_KEY: 're_secret',
    ALLOWED_HOSTS: 'api.example.com',
    TRUST_CLOUDFLARE_PROXY: 'true',
    WEB: { not: 'a string' },
    UNRELATED_BINDING: 'x',
  };

  it('web gets required + bound optional secrets and plain config, nothing else', () => {
    const out = containerEnvFor('web', env);
    expect(out).toMatchObject({ PYTHAGORAS_ROLE: 'web', PORT: '8000', ALLOWED_HOSTS: 'api.example.com' });
    expect(out.RESEND_API_KEY).toBe('re_secret');
    expect(out).not.toHaveProperty('WEB');
    expect(out).not.toHaveProperty('UNRELATED_BINDING');
  });

  it('job-probe never receives optional provider secrets or web config', () => {
    const out = containerEnvFor('job-probe', env);
    expect(out).not.toHaveProperty('RESEND_API_KEY');
    expect(out).not.toHaveProperty('ALLOWED_HOSTS');
    expect(out.PYTHAGORAS_ROLE).toBe('job-probe');
  });

  it('outbox-once gets the database, Resend key and mail settings, but no web config or R2 secrets', () => {
    const out = containerEnvFor('outbox-once', {
      ...env,
      DEFAULT_FROM_EMAIL: 'noreply@example.com',
      EMAIL_IDEMPOTENCY_NAMESPACE: 'staging',
      R2_SECRET_ACCESS_KEY: 'r2_secret',
    });
    expect(out).toMatchObject({
      PYTHAGORAS_ROLE: 'outbox-once',
      OUTBOX_BUDGET_SECONDS: '240',
      SECRET_KEY: env.SECRET_KEY,
      DATABASE_URL: env.DATABASE_URL,
      RESEND_API_KEY: 're_secret',
      DEFAULT_FROM_EMAIL: 'noreply@example.com',
      EMAIL_IDEMPOTENCY_NAMESPACE: 'staging',
    });
    expect(out).not.toHaveProperty('ALLOWED_HOSTS');
    expect(out).not.toHaveProperty('TRUST_CLOUDFLARE_PROXY');
    expect(out).not.toHaveProperty('R2_SECRET_ACCESS_KEY');
    expect(out).not.toHaveProperty('JOB_PROBE_BUDGET_SECONDS');
    expect(out).not.toHaveProperty('UNRELATED_BINDING');
  });

  it('reports missing secrets by name only', () => {
    expect(missingRequiredSecrets({ SECRET_KEY: 'x' })).toEqual(['DATABASE_URL']);
    expect(missingRequiredSecrets({ SECRET_KEY: '', DATABASE_URL: '' })).toEqual(['SECRET_KEY', 'DATABASE_URL']);
  });

  it('lists secret values for scrubbing', () => {
    expect(secretValues(env)).toEqual([env.SECRET_KEY, env.DATABASE_URL, 're_secret']);
  });
});
