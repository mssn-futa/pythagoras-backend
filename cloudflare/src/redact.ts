/**
 * Log scrubbing. Controller logs carry event names and coarse fields only; this is
 * the last line of defence if an error message happens to embed a secret value
 * (for example a connection string inside a thrown error).
 */
const URL_CREDENTIALS = /([a-z][a-z0-9+.-]*:\/\/)[^\s/@:]*:[^\s/@]*@/gi;

export function scrub(text: string, secrets: readonly string[]): string {
  let out = text;
  for (const secret of secrets) {
    if (secret.length >= 4) out = out.split(secret).join('[redacted]');
  }
  return out.replace(URL_CREDENTIALS, '$1[redacted]@');
}

export type Logger = (event: string, fields?: Record<string, string | number | boolean | null>) => void;

/** Structured JSON logger that scrubs every emitted line. `sink` defaults to console.log. */
export function makeLogger(secrets: () => readonly string[], sink: (line: string) => void = console.log): Logger {
  return (event, fields = {}) => {
    sink(scrub(JSON.stringify({ event, ...fields }), secrets()));
  };
}

/** Error class name only: messages are never logged. */
export function errorName(error: unknown): string {
  return error instanceof Error ? error.name : 'NonError';
}
