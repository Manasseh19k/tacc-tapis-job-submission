export const COOKIE = {
  /** Short-lived CSRF state, only needed during the redirect round-trip. */
  state: "tapis_oauth_state",
  /** Optional post-login return path stashed alongside the state. */
  returnTo: "tapis_return_to",
  /** The Tapis access token (JWT) used as X-Tapis-Token. */
  accessToken: "tapis_token",
} as const;

export interface TapisConfig {
  tenantBaseUrl: string;
  clientId: string;
  clientKey: string;
  callbackUrl: string;
  postLoginPath: string;
}

/** Read + validate the Tapis config from environment variables. */
export function getConfig(): TapisConfig {
  const tenantBaseUrl = requiredEnv("TAPIS_TENANT_URL").replace(/\/+$/, "");
  const clientId = requiredEnv("TAPIS_CLIENT_ID");
  const clientKey = requiredEnv("TAPIS_CLIENT_KEY");
  const callbackUrl = requiredEnv("TAPIS_CALLBACK_URL");
  const postLoginPath = process.env.APP_POST_LOGIN_PATH || "/";
  return { tenantBaseUrl, clientId, clientKey, callbackUrl, postLoginPath };
}

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `Missing required environment variable ${name}. See .env.local`,
    );
  }
  return value;
}

/** Build the Tapis authorization URL to redirect the user to. */
export function buildAuthorizeUrl(cfg: TapisConfig, state: string): string {
  const params = new URLSearchParams({
    client_id: cfg.clientId,
    redirect_uri: cfg.callbackUrl,
    response_type: "code",
    state,
  });
  return `${cfg.tenantBaseUrl}/v3/oauth2/authorize?${params.toString()}`;
}

/** Shape of the piece of the token response that is care about. */
export interface TapisTokenResult {
  accessToken: string;
  /** Absolute expiry of the access token. */
  accessExpiresAt: Date;
}

/**
 * Exchange an authorization code for a Tapis JWT.
 * POST {tenant}/v3/oauth2/tokens with the code + HTTP Basic client auth.
 */
export async function exchangeCodeForToken(
  cfg: TapisConfig,
  code: string,
): Promise<TapisTokenResult> {
  const basic = Buffer.from(`${cfg.clientId}:${cfg.clientKey}`).toString(
    "base64",
  );

  const res = await fetch(`${cfg.tenantBaseUrl}/v3/oauth2/tokens`, {
    method: "POST",
    headers: {
      Authorization: `Basic ${basic}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: cfg.callbackUrl,
    }),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `Tapis token exchange failed (${res.status}): ${text.slice(0, 500)}`,
    );
  }

  const json = (await res.json()) as {
    result?: {
      access_token?: {
        access_token?: string;
        expires_at?: string;
        expires_in?: number;
      };
    };
  };

  const result = json.result;
  const accessToken = result?.access_token?.access_token;
  if (!accessToken) {
    throw new Error("Tapis token response did not include an access_token.");
  }

  return {
    accessToken,
    accessExpiresAt: parseExpiry(
      result?.access_token?.expires_at,
      result?.access_token?.expires_in,
    ),
  };
}

/** Best-effort token revocation used on logout. */
export async function revokeToken(
  cfg: TapisConfig,
  token: string,
): Promise<void> {
  try {
    await fetch(`${cfg.tenantBaseUrl}/v3/oauth2/tokens/revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      cache: "no-store",
    });
  } catch {
    // Revocation is best-effort; clearing cookies still logs the user out.
  }
}

/** Fetch the authenticated user's profile from Tapis. */
export async function fetchUserInfo(
  cfg: TapisConfig,
  accessToken: string,
): Promise<Record<string, unknown> | null> {
  const res = await fetch(`${cfg.tenantBaseUrl}/v3/oauth2/userinfo`, {
    headers: { "X-Tapis-Token": accessToken },
    cache: "no-store",
  });
  if (!res.ok) return null;
  const json = (await res.json()) as { result?: Record<string, unknown> };
  return json.result ?? null;
}

function parseExpiry(expiresAt?: string, expiresIn?: number): Date {
  if (expiresAt) {
    const d = new Date(expiresAt);
    if (!Number.isNaN(d.getTime())) return d;
  }
  if (typeof expiresIn === "number") {
    return new Date(Date.now() + expiresIn * 1000);
  }
  // Tapis access tokens default to 4 hours; fall back to that.
  return new Date(Date.now() + 4 * 60 * 60 * 1000);
}
