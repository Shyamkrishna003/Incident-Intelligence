import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, describeError, setTokenProvider } from "./api";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function mockFetch(...responses: Response[]) {
  const fetchMock = vi.fn<typeof fetch>();
  for (const response of responses) fetchMock.mockResolvedValueOnce(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setTokenProvider(() => Promise.resolve(null));
});

describe("api client", () => {
  it("sends the user's ID token as a bearer credential", async () => {
    setTokenProvider(() => Promise.resolve("token-1"));
    const fetchMock = mockFetch(jsonResponse(200, { services: [] }));

    await api.listServices("p1");

    const [path, init] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe("/v1/projects/p1/services");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer token-1");
  });

  it("retries once with a fresh token after a 401", async () => {
    const provider = vi.fn((force: boolean) => Promise.resolve(force ? "fresh" : "stale"));
    setTokenProvider(provider);
    const fetchMock = mockFetch(
      jsonResponse(401, { error: { code: "unauthenticated", message: "no" } }),
      jsonResponse(200, { services: [] }),
    );

    await api.listServices("p1");

    expect(provider.mock.calls).toEqual([[false], [true]]);
    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("Authorization")).toBe(
      "Bearer fresh",
    );
  });

  it("turns the server's error envelope into an ApiError", async () => {
    mockFetch(
      jsonResponse(
        422,
        {
          error: {
            code: "validation_error",
            message: "Request validation failed.",
            request_id: "req-9",
            details: [{ loc: ["body", "slug"], msg: "Bad slug.", type: "x" }],
          },
        },
        { "X-Request-ID": "req-9" },
      ),
    );

    const error = await api.createOrganization({ slug: "Bad", name: "x" }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.code).toBe("validation_error");
    expect(apiError.requestId).toBe("req-9");
    expect(describeError(apiError)).toBe("Request validation failed. Bad slug.");
  });

  it("describes a rate limit using Retry-After", async () => {
    mockFetch(
      jsonResponse(429, { error: { code: "rate_limited", message: "Too many requests." } }, {
        "Retry-After": "12",
      }),
    );

    const error = await api.me().catch((e: unknown) => e);

    expect(describeError(error)).toBe("Too many requests. Try again in 12 seconds.");
  });

  it("survives a non-JSON error body", async () => {
    mockFetch(new Response("<html>Bad gateway</html>", { status: 502 }));

    const error = (await api.me().catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(502);
    expect(error.message).toBe("Request failed (502).");
  });

  it("reports an unreachable server as a network error", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockRejectedValue(new TypeError("failed")));

    const error = (await api.me().catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("network_error");
  });

  it("encodes path segments and returns nothing for 204", async () => {
    const fetchMock = mockFetch(new Response(null, { status: 204 }));

    await expect(api.revokeApiKey("p/1", "abc")).resolves.toBeUndefined();

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/v1/projects/p%2F1/api-keys/abc");
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("DELETE");
  });
});
