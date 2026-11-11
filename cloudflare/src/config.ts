/**
 * Fixed runtime configuration for the Pythagoras Cloudflare controller.
 *
 * Everything here is a NAME or a constant. No secret value is ever stored in
 * this repository: values come from Worker Secrets (`wrangler secret put`) and
 * non-secret `vars` in wrangler.jsonc.
 */

/** Allow-list of container roles. Mirrors /app/docker-entrypoint.sh. `migrate` is deliberately absent. */
export const ROLES = ['web', 'job-probe', 'outbox-once'] as const;
export type Role = (typeof ROLES)[number];

export function isAllowedRole(value: unknown): value is Role {
  return typeof value === 'string' && (ROLES as readonly string[]).includes(value);
}

/** Worker Secrets that must exist before any container is started. */
export const REQUIRED_SECRET_NAMES = ['SECRET_KEY', 'DATABASE_URL'] as const;

/**
 * Worker Secrets forwarded to the `web` role only when bound (features that land
 * later: PLAT-03 Resend, DAWAH-02 R2). The job probe never receives them.
 */
export const OPTIONAL_WEB_SECRET_NAMES = [
  'RESEND_API_KEY',
  'R2_ACCESS_KEY_ID',
  'R2_SECRET_ACCESS_KEY',
] as const;

/** Non-secret Worker `vars` forwarded to the `web` role when defined. */
export const WEB_PLAIN_NAMES = [
  'ALLOWED_HOSTS',
  'CORS_ALLOWED_ORIGINS',
  'CSRF_TRUSTED_ORIGINS',
  'DEBUG',
  'TRUST_CLOUDFLARE_PROXY',
  'SECURE_SSL_REDIRECT',
  'WEB_CONCURRENCY',
  'DEFAULT_FROM_EMAIL',
  'EMAIL_REPLY_TO',
  'RESEND_TIMEOUT_SECONDS',
  'RESEND_REQUESTS_PER_SECOND',
  'RESEND_DAILY_REQUEST_BUDGET',
  'R2_ACCOUNT_ID',
  'R2_S3_ENDPOINT_URL',
  'R2_PRIVATE_BUCKET',
  'R2_PUBLIC_BUCKET',
  'R2_PUBLIC_DOMAIN',
  'R2_UPLOAD_INTENT_TTL_SECONDS',
  'R2_PREVIEW_TTL_SECONDS',
  'R2_CORS_ALLOWED_ORIGINS',
  'AUTOMATION_TIMEZONE',
  'AUTOMATION_CATCH_UP_DAYS',
  'AUTOMATION_REMINDER_GRACE_SECONDS',
] as const;

/** The job probe only needs the database. */
export const JOB_PROBE_FORWARDED_NAMES = ['SECRET_KEY', 'DATABASE_URL'] as const;

/**
 * Bounded broadcast outbox pass (COMM-02): `manage.py dawah_outbox --once`.
 * RELEASE-03 switches the Cron job from `job-probe` to this role.
 */
export const OUTBOX_ROLE = 'outbox-once' as const;

/** The outbox pass needs the database, the Resend key and the mail/automation settings only. */
export const OUTBOX_FORWARDED_NAMES = [
  'SECRET_KEY',
  'DATABASE_URL',
  'RESEND_API_KEY',
  'DEBUG',
  'DEFAULT_FROM_EMAIL',
  'EMAIL_REPLY_TO',
  'EMAIL_IDEMPOTENCY_NAMESPACE',
  'RESEND_TIMEOUT_SECONDS',
  'RESEND_REQUESTS_PER_SECOND',
  'OUTBOX_REQUESTS_PER_SECOND',
  'AUTOMATION_TIMEZONE',
  'AUTOMATION_CATCH_UP_DAYS',
  'AUTOMATION_REMINDER_GRACE_SECONDS',
] as const;

export const WEB_PORT = 8000;
/** Single named web instance for the proof; RELEASE-03 may widen this with getRandom(). */
export const WEB_INSTANCE_NAME = 'web-primary';
/** The one-and-only job coordinator Durable Object. */
export const JOB_SINGLETON_NAME = 'job-singleton';

/** Engineering wall budget for a job run (D5: 240 s, below Cloudflare scheduled/monitor limits). */
export const JOB_BUDGET_SECONDS = 240;
/** Grace after the budget before the coordinator force-stops an overdue run. */
export const JOB_KILL_GRACE_SECONDS = 60;

export type EnvLike = Record<string, unknown>;

function str(env: EnvLike, name: string): string | undefined {
  const value = env[name];
  return typeof value === 'string' && value !== '' ? value : undefined;
}

/** Names (never values) of required secrets that are absent. */
export function missingRequiredSecrets(env: EnvLike): string[] {
  return REQUIRED_SECRET_NAMES.filter((name) => str(env, name) === undefined);
}

/** Environment passed to the container for a role. The role itself is fixed here, not caller-supplied. */
export function containerEnvFor(role: Role, env: EnvLike): Record<string, string> {
  if (!isAllowedRole(role)) {
    throw new Error('role not in allow-list');
  }
  const out: Record<string, string> = { PYTHAGORAS_ROLE: role, PORT: String(WEB_PORT) };
  const names: readonly string[] =
    role === 'web'
      ? [...REQUIRED_SECRET_NAMES, ...OPTIONAL_WEB_SECRET_NAMES, ...WEB_PLAIN_NAMES]
      : role === OUTBOX_ROLE
        ? OUTBOX_FORWARDED_NAMES
        : JOB_PROBE_FORWARDED_NAMES;
  for (const name of names) {
    const value = str(env, name);
    if (value !== undefined) out[name] = value;
  }
  if (role === 'job-probe') {
    out.JOB_PROBE_BUDGET_SECONDS = String(JOB_BUDGET_SECONDS);
  }
  if (role === OUTBOX_ROLE) {
    out.OUTBOX_BUDGET_SECONDS = String(JOB_BUDGET_SECONDS);
  }
  return out;
}

/** Every secret value currently bound, for log scrubbing. */
export function secretValues(env: EnvLike): string[] {
  return [...REQUIRED_SECRET_NAMES, ...OPTIONAL_WEB_SECRET_NAMES]
    .map((name) => str(env, name))
    .filter((v): v is string => v !== undefined);
}
