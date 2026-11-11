import { defineConfig } from 'vitest/config';

// Pure-logic tests run on plain Node (Request/Response/Headers are Node 22 globals);
// the Workers-runtime glue in src/index.ts is covered by `tsc` and the staged proof.
export default defineConfig({ test: { include: ['test/**/*.test.ts'], environment: 'node' } });
