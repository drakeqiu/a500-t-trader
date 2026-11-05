import { createHash, generateKeyPairSync, sign } from "node:crypto";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

function base64url(value) {
  return Buffer.from(value).toString("base64url");
}

export function signJwt(claims, privateKey, jwk) {
  const header = base64url(JSON.stringify({ alg: "RS256", kid: jwk.kid, typ: "JWT" }));
  const payload = base64url(JSON.stringify(claims));
  const input = `${header}.${payload}`;
  return `${input}.${sign("RSA-SHA256", Buffer.from(input), privateKey).toString("base64url")}`;
}

export async function configureIdentityProvider(page, {
  issuer,
  clientId,
  audience,
  subject,
  accessToken,
  deny = false,
  onJwks,
  onAuthorize,
  onToken,
}) {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "test-signing-key", alg: "RS256", use: "sig" };
  await onJwks?.(jwk);

  let nonce = "";
  let challenge = "";
  await page.route(`${issuer}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === "/.well-known/openid-configuration") {
      return route.fulfill({ json: {
        issuer,
        authorization_endpoint: `${issuer}/authorize`,
        token_endpoint: `${issuer}/token`,
        jwks_uri: `${issuer}/jwks`,
      }, headers: cors });
    }
    if (url.pathname === "/jwks") return route.fulfill({ json: { keys: [jwk] }, headers: cors });
    if (url.pathname === "/authorize") {
      const authorization = url.searchParams;
      if (authorization.get("client_id") !== clientId
        || authorization.get("response_type") !== "code"
        || authorization.get("resource") !== audience
        || authorization.get("code_challenge_method") !== "S256") {
        throw new Error("Unexpected OIDC authorization request");
      }
      if (onAuthorize) await onAuthorize(authorization);
      nonce = authorization.get("nonce");
      challenge = authorization.get("code_challenge");
      const callback = new URL(authorization.get("redirect_uri"));
      if (deny) callback.searchParams.set("error", "access_denied");
      else callback.searchParams.set("code", "one-time-code");
      callback.searchParams.set("state", authorization.get("state"));
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: `<script>location.replace(${JSON.stringify(callback.toString())})</script>`,
      });
    }
    if (url.pathname === "/token" && request.method() === "POST") {
      const form = new URLSearchParams(request.postData() ?? "");
      if (form.get("grant_type") !== "authorization_code"
        || form.get("client_id") !== clientId
        || form.get("resource") !== audience) {
        throw new Error("Unexpected OIDC token request");
      }
      const verifier = form.get("code_verifier");
      if (!verifier || createHash("sha256").update(verifier).digest("base64url") !== challenge) {
        throw new Error("PKCE code verifier does not match the authorization challenge");
      }
      await onToken?.(form);
      const now = Math.floor(Date.now() / 1000);
      const token = typeof accessToken === "function"
        ? accessToken({ jwk, privateKey, now })
        : accessToken;
      return route.fulfill({ json: {
        access_token: token,
        token_type: "Bearer",
        expires_in: 300,
        id_token: signJwt({ iss: issuer, aud: clientId, sub: subject, nonce, iat: now, exp: now + 300 }, privateKey, jwk),
      }, headers: cors });
    }
    return route.fulfill({ status: 404, headers: cors });
  });
  return { jwk, privateKey };
}
