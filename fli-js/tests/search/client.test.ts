/**
 * HTTP client tests — focused on rate-limiting, retry, error wrapping,
 * timeout, and proxy plumbing. Network calls are stubbed via a fake fetch.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { Client } from "../../src/search/client.ts";
import {
  SearchClientError,
  SearchConnectionError,
  SearchHTTPError,
  SearchTimeoutError,
} from "../../src/search/exceptions.ts";

function okResponse(text = "ok"): Response {
  return new Response(text, { status: 200, headers: { "content-type": "text/plain" } });
}

function errorResponse(status: number): Response {
  return new Response(`error ${status}`, { status });
}

/** Cast `() => Promise<Response>` style stubs to the wide `typeof fetch`. */
function asFetch(fn: (input: unknown, init?: RequestInit) => Promise<Response>): typeof fetch {
  return fn as unknown as typeof fetch;
}

/** Restore an environment entry without leaking changes between tests. */
function restoreEnv(env: NodeJS.ProcessEnv, name: string, value: string | undefined): void {
  if (value === undefined) delete env[name];
  else env[name] = value;
}

describe("environment restoration", () => {
  test("restoring an absent timeout lets the next Client use its default", () => {
    const previous = process.env.FLI_TIMEOUT;
    const env = new Proxy(process.env, {
      set(target, key, value) {
        return Reflect.set(target, key, String(value));
      },
    });
    try {
      delete process.env.FLI_TIMEOUT;
      env.FLI_TIMEOUT = "5";
      restoreEnv(env, "FLI_TIMEOUT", undefined);
      expect(() => new Client()).not.toThrow();
    } finally {
      if (previous === undefined) delete process.env.FLI_TIMEOUT;
      else process.env.FLI_TIMEOUT = previous;
    }
  });

  for (const name of ["FLI_TIMEOUT", "HTTPS_PROXY"]) {
    test(`${name}: restores absence even when assignments stringify undefined`, () => {
      // Model Node-style process.env coercion, regardless of Bun's host behavior.
      const env = new Proxy<NodeJS.ProcessEnv>(
        {},
        {
          set(target, key, value) {
            return Reflect.set(target, key, String(value));
          },
        },
      );
      env[name] = "temporary";
      restoreEnv(env, name, undefined);
      expect(env[name]).toBeUndefined();
      expect(Object.hasOwn(env, name)).toBe(false);
    });

    test(`${name}: restores an existing value`, () => {
      const env: NodeJS.ProcessEnv = { [name]: "original" };
      const previous = env[name];
      env[name] = "temporary";
      restoreEnv(env, name, previous);
      expect(env[name]).toBe("original");
    });
  }
});

describe("Client", () => {
  let originalEnv: typeof process.env.FLI_TIMEOUT;
  let originalProxy: typeof process.env.HTTPS_PROXY;

  beforeEach(() => {
    originalEnv = process.env.FLI_TIMEOUT;
    originalProxy = process.env.HTTPS_PROXY;
  });
  afterEach(() => {
    restoreEnv(process.env, "FLI_TIMEOUT", originalEnv);
    restoreEnv(process.env, "HTTPS_PROXY", originalProxy);
  });

  test("POST with body sends the expected request", async () => {
    const captured: { url?: string; init?: RequestInit } = {};
    const fake = async (input: unknown, init?: RequestInit): Promise<Response> => {
      captured.url = String(input);
      captured.init = init ?? {};
      return okResponse("hello");
    };
    const c = new Client({ fetchImpl: asFetch(fake) });
    const res = await c.post("https://example.com/api", { body: "x=1" });
    expect(res.text).toBe("hello");
    expect(captured.url).toBe("https://example.com/api");
    expect(captured.init?.method).toBe("POST");
    expect(captured.init?.body).toBe("x=1");
  });

  test("GET sends an empty-body request", async () => {
    const captured: { init?: RequestInit } = {};
    const fake = async (_u: unknown, i?: RequestInit): Promise<Response> => {
      captured.init = i;
      return okResponse();
    };
    const c = new Client({ fetchImpl: asFetch(fake) });
    await c.get("https://example.com");
    expect(captured.init?.method).toBe("GET");
    expect(captured.init?.body).toBeUndefined();
  });

  test("default Chrome-like headers are applied", async () => {
    const captured: { headers?: Record<string, string> } = {};
    const fake = async (_u: unknown, init?: RequestInit): Promise<Response> => {
      captured.headers = init?.headers as Record<string, string>;
      return okResponse();
    };
    const c = new Client({ fetchImpl: asFetch(fake) });
    await c.post("https://example.com", { body: "" });
    expect(captured.headers?.["user-agent"]).toMatch(/Chrome/);
    expect(captured.headers?.["content-type"]).toBe(
      "application/x-www-form-urlencoded;charset=UTF-8",
    );
  });

  test("4xx wraps to SearchHTTPError with status_code", async () => {
    const fake = async (): Promise<Response> => errorResponse(429);
    const c = new Client({ fetchImpl: asFetch(fake), retries: 1 });
    try {
      await c.post("https://example.com", { body: "" });
      expect.unreachable();
    } catch (e) {
      expect(e).toBeInstanceOf(SearchHTTPError);
      expect((e as SearchHTTPError).status_code).toBe(429);
    }
  });

  test("connection error wraps to SearchConnectionError", async () => {
    const fake = async (): Promise<Response> => {
      throw new TypeError("fetch failed");
    };
    const c = new Client({ fetchImpl: asFetch(fake), retries: 1 });
    try {
      await c.post("https://example.com", { body: "" });
      expect.unreachable();
    } catch (e) {
      expect(e).toBeInstanceOf(SearchConnectionError);
    }
  });

  test("AbortError wraps to SearchTimeoutError", async () => {
    const fake = async (_u: unknown, init?: RequestInit): Promise<Response> => {
      return new Promise<Response>((_, reject) => {
        const signal = init?.signal;
        if (signal?.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    };
    const c = new Client({ fetchImpl: asFetch(fake), retries: 1, timeoutMs: 50 });
    try {
      await c.post("https://example.com", { body: "" });
      expect.unreachable();
    } catch (e) {
      expect(e).toBeInstanceOf(SearchTimeoutError);
    }
  });

  test("external AbortSignal propagates without retry and is NOT a SearchTimeoutError", async () => {
    let calls = 0;
    const fake = async (_u: unknown, init?: RequestInit): Promise<Response> => {
      calls++;
      return new Promise<Response>((_, reject) => {
        const signal = init?.signal;
        if (signal?.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    };
    const c = new Client({
      fetchImpl: asFetch(fake),
      retries: 3,
      timeoutMs: 60_000,
      backoffMs: 1,
    });
    const controller = new AbortController();
    const cancelled = new Error("caller aborted");
    setTimeout(() => controller.abort(cancelled), 20);
    try {
      await c.post("https://example.com", { body: "", signal: controller.signal });
      expect.unreachable();
    } catch (e) {
      // Caller's reason propagates as-is — NOT wrapped as a timeout.
      expect(e).toBe(cancelled);
      expect(e).not.toBeInstanceOf(SearchTimeoutError);
    }
    // No retry — the request stops the moment the caller cancels.
    expect(calls).toBe(1);
  });

  test("pre-aborted external signal short-circuits before any fetch retry", async () => {
    let calls = 0;
    // Real `fetch` rejects synchronously on a pre-aborted signal — model that.
    const fake = async (_u: unknown, init?: RequestInit): Promise<Response> => {
      calls++;
      if (init?.signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
      return okResponse();
    };
    const c = new Client({ fetchImpl: asFetch(fake), retries: 3, backoffMs: 1 });
    const reason = new Error("already cancelled");
    const controller = new AbortController();
    controller.abort(reason);
    try {
      await c.post("https://example.com", { body: "", signal: controller.signal });
      expect.unreachable();
    } catch (e) {
      expect(e).toBe(reason);
      expect(e).not.toBeInstanceOf(SearchTimeoutError);
    }
    expect(calls).toBe(1);
  });

  test("retries on transient network failure then succeeds", async () => {
    let calls = 0;
    const fake = async (): Promise<Response> => {
      calls++;
      if (calls < 3) throw new TypeError("fetch failed");
      return okResponse("recovered");
    };
    const c = new Client({ fetchImpl: asFetch(fake), retries: 3, backoffMs: 1 });
    const res = await c.post("https://example.com", { body: "" });
    expect(res.text).toBe("recovered");
    expect(calls).toBe(3);
  });

  test("rate limiter slows down bursts", async () => {
    const fake = async (): Promise<Response> => okResponse();
    const c = new Client({
      fetchImpl: asFetch(fake),
      callsPerSecond: 2,
      retries: 1,
    });
    const start = performance.now();
    await Promise.all([
      c.post("https://x", { body: "" }),
      c.post("https://x", { body: "" }),
      c.post("https://x", { body: "" }),
      c.post("https://x", { body: "" }),
    ]);
    const elapsed = performance.now() - start;
    expect(elapsed).toBeGreaterThan(500);
  });

  test("HTTPS_PROXY env var picked up on construction", async () => {
    const previous = process.env.HTTPS_PROXY;
    process.env.HTTPS_PROXY = "http://proxy.example.com:8080";
    try {
      const captured: { init?: RequestInit & { proxy?: string } } = {};
      const fake = async (_u: unknown, i?: RequestInit): Promise<Response> => {
        captured.init = i as RequestInit & { proxy?: string };
        return okResponse();
      };
      const c = new Client({ fetchImpl: asFetch(fake) });
      await c.post("https://example.com", { body: "" });
      expect(captured.init?.proxy).toBe("http://proxy.example.com:8080");
    } finally {
      restoreEnv(process.env, "HTTPS_PROXY", previous);
    }
  });

  test("explicit proxy option overrides env", async () => {
    process.env.HTTPS_PROXY = "http://wrong.example.com";
    try {
      const captured: { init?: RequestInit & { proxy?: string } } = {};
      const fake = async (_u: unknown, i?: RequestInit): Promise<Response> => {
        captured.init = i as RequestInit & { proxy?: string };
        return okResponse();
      };
      const c = new Client({
        fetchImpl: asFetch(fake),
        proxy: "http://right.example.com",
      });
      await c.post("https://example.com", { body: "" });
      expect(captured.init?.proxy).toBe("http://right.example.com");
    } finally {
      restoreEnv(process.env, "HTTPS_PROXY", originalProxy);
    }
  });

  test("invalid FLI_TIMEOUT throws on construction", () => {
    process.env.FLI_TIMEOUT = "garbage";
    expect(() => new Client()).toThrow(/FLI_TIMEOUT/);
  });

  test("negative FLI_TIMEOUT throws on construction", () => {
    process.env.FLI_TIMEOUT = "-5";
    expect(() => new Client()).toThrow(/FLI_TIMEOUT/);
  });

  test("literal undefined FLI_TIMEOUT remains invalid", () => {
    process.env.FLI_TIMEOUT = "undefined";
    expect(() => new Client()).toThrow(/FLI_TIMEOUT must be a number of seconds/);
  });

  test("retries exhausted re-raises the last error", async () => {
    const fake = async (): Promise<Response> => {
      throw new TypeError("fetch failed");
    };
    const c = new Client({ fetchImpl: asFetch(fake), retries: 2, backoffMs: 1 });
    await expect(c.post("https://x", { body: "" })).rejects.toBeInstanceOf(SearchClientError);
  });
});
