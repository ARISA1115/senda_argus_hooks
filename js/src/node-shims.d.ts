declare const process: {
  version: string; platform: string; arch: string; pid: number;
  stdout: { write(value: string): void };
};
declare module "node:async_hooks" { export class AsyncLocalStorage<T> { getStore(): T | undefined; run<R>(store: T, callback: () => R): R; } }
declare module "node:crypto" { export function randomUUID(): string; export function createHash(name: string): { update(value: string): any; digest(encoding: "hex"): string }; }
declare module "node:fs" { export function appendFileSync(path: string, data: string, encoding: string): void; export function mkdirSync(path: string, opts?: { recursive?: boolean }): void; }
declare module "node:path" { export function dirname(path: string): string; }
declare module "node:perf_hooks" { export const performance: { now(): number }; }
declare module "node:test" { const test: (name: string, fn: () => void | Promise<void>) => void; export default test; }
declare module "node:assert/strict" { const assert: any; export default assert; }
