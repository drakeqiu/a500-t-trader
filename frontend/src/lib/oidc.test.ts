import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const issuer = "http://identity.example/realm";
const metadata = {
  issuer,
  authorization_endpoint: `${issuer}/authorize`,
  token_endpoint: `${issuer}/token`,
};
const key = "a500.oidc.transaction";
// Dynamic imports exercise build-time public configuration with a fresh module per test.

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_OIDC_ISSUER", issuer);
  vi.stubEnv("NEXT_PUBLIC_OIDC_CLIENT_ID", "a500-browser-client");
  sessionStorage.clear();
  history.replaceState(null, "", "/");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  sessionStorage.clear();
  history.replaceState(null, "", "/");
});

describe("authorization code with PKCE", () => {
  it("computes the RFC 7636 S256 vector without crypto.subtle", async () => {
    const { pkceChallenge } = await import("./oidc");
    expect(pkceChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });

  it("rejects untrusted discovery endpoints and unavailable secure randomness", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        ...metadata,
        authorization_endpoint: "http://attacker.example/authorize",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { beginAuthorization } = await import("./oidc");
    await expect(beginAuthorization()).rejects.toThrow(/端点无效/);
    expect(sessionStorage.getItem(key)).toBeNull();
    fetchMock.mockResolvedValue(Response.json(metadata));
    vi.stubGlobal("crypto", {});
    await expect(beginAuthorization()).rejects.toThrow(/安全随机数/);
    expect(sessionStorage.getItem(key)).toBeNull();
  });

  it("resolves a same-origin relative issuer against the browser origin", async () => {
    const relativeIssuer = `${location.origin}/fixture-idp`;
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        issuer: relativeIssuer,
        authorization_endpoint: `${relativeIssuer}/authorize`,
        token_endpoint: `${relativeIssuer}/token`,
      }),
    );
    vi.stubEnv("NEXT_PUBLIC_OIDC_ISSUER", "/fixture-idp");
    vi.stubGlobal("fetch", fetchMock);
    const { beginAuthorization } = await import("./oidc");
    const authorization = new URL(await beginAuthorization());

    expect(fetchMock.mock.calls[0][0]).toBe(`${relativeIssuer}/.well-known/openid-configuration`);
    expect(authorization.origin).toBe(location.origin);
  });

  it("preserves the issuer's trailing slash while joining the discovery URL", async () => {
    const trailingSlashIssuer = "https://identity.example/auth/v1/";
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        issuer: trailingSlashIssuer,
        authorization_endpoint: `${trailingSlashIssuer}authorize`,
        token_endpoint: `${trailingSlashIssuer}token`,
      }),
    );
    vi.stubEnv("NEXT_PUBLIC_OIDC_ISSUER", trailingSlashIssuer);
    vi.stubGlobal("fetch", fetchMock);
    const { beginAuthorization } = await import("./oidc");

    await beginAuthorization();

    expect(fetchMock.mock.calls[0][0]).toBe(
      "https://identity.example/auth/v1/.well-known/openid-configuration",
    );
  });

  it("uses random state and S256, consumes the callback before exchanging and never stores the token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(metadata))
      .mockResolvedValueOnce(Response.json(metadata))
      .mockResolvedValueOnce(Response.json({ access_token: "secret-token", expires_in: 120 }));
    vi.stubGlobal("fetch", fetchMock);
    const { beginAuthorization, completeAuthorization, pkceChallenge } = await import("./oidc");
    const authorization = new URL(await beginAuthorization());
    const transaction = JSON.parse(sessionStorage.getItem(key)!);
    expect(authorization.searchParams.get("client_id")).toBe("a500-browser-client");
    expect(authorization.searchParams.get("redirect_uri")).toBe(`${location.origin}/`);
    expect(authorization.searchParams.get("response_type")).toBe("code");
    expect(authorization.searchParams.get("code_challenge_method")).toBe("S256");
    expect(authorization.searchParams.get("code_challenge")).toBe(
      pkceChallenge(transaction.verifier),
    );
    expect(authorization.searchParams.get("state")).toBe(transaction.state);
    history.replaceState(
      null,
      "",
      `/?code=auth-code&state=${transaction.state}&session_state=other`,
    );
    const pending = completeAuthorization();
    expect(location.search).toBe("");
    expect(sessionStorage.getItem(key)).toBeNull();
    expect(completeAuthorization()).toBeNull();
    await expect(pending).resolves.toEqual({ token: "secret-token", expiresIn: 120 });
    const body = fetchMock.mock.calls[2][1].body as URLSearchParams;
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code_verifier")).toBe(transaction.verifier);
    expect(body.get("redirect_uri")).toBe(`${location.origin}/`);
    expect(JSON.stringify(sessionStorage)).not.toContain("secret-token");
    expect(JSON.stringify(localStorage)).not.toContain("secret-token");
  });

  it.each(["mismatch", "expired", "missing", "duplicate"])(
    "rejects %s state without exchanging",
    async (kind) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const { completeAuthorization } = await import("./oidc");
      if (kind !== "missing")
        sessionStorage.setItem(
          key,
          JSON.stringify({
            state: "valid",
            verifier: "v".repeat(43),
            created: Date.now() - (kind === "expired" ? 600001 : 0),
          }),
        );
      const state = kind === "mismatch" ? "wrong" : "valid";
      history.replaceState(
        null,
        "",
        `/?code=secret-code&state=${state}${kind === "duplicate" ? "&state=valid" : ""}`,
      );
      await expect(completeAuthorization()).rejects.toThrow(/授权会话/);
      expect(location.search).toBe("");
      expect(sessionStorage.getItem(key)).toBeNull();
      expect(fetchMock).not.toHaveBeenCalled();
      history.replaceState(null, "", `/?code=secret-code&state=${state}`);
      await expect(completeAuthorization()).rejects.toThrow(/授权会话/);
    },
  );

  it("clears a denied callback and reports a safe error", async () => {
    const { completeAuthorization } = await import("./oidc");
    sessionStorage.setItem(
      key,
      JSON.stringify({ state: "valid", verifier: "v".repeat(43), created: Date.now() }),
    );
    history.replaceState(null, "", "/?error=access_denied&error_description=private&state=valid");
    await expect(completeAuthorization()).rejects.toThrow("授权已拒绝");
    expect(location.search).toBe("");
  });

  it("does not expose an expired authorization code or token endpoint error", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(Response.json(metadata))
        .mockResolvedValueOnce(Response.json({ error_description: "sensitive" }, { status: 400 })),
    );
    const { completeAuthorization } = await import("./oidc");
    sessionStorage.setItem(
      key,
      JSON.stringify({ state: "valid", verifier: "v".repeat(43), created: Date.now() }),
    );
    history.replaceState(null, "", "/?code=expired&state=valid");
    await expect(completeAuthorization()).rejects.toThrow(/授权码可能已过期/);
    expect(location.search).toBe("");
  });
});
