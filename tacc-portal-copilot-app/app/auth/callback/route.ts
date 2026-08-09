import { NextRequest, NextResponse } from "next/server";
import { COOKIE, exchangeCodeForToken, getConfig } from "@/lib/tapis";

/**
 * Step 2 of the authorization code flow.
 * Tapis redirects here with ?code=...&state=.... It verify the state matches
 * the cookie set in /auth/login, exchange the code for a Tapis JWT, and store
 * the token(s) in httpOnly cookies.
 */
export async function GET(request: NextRequest) {
  const cfg = getConfig();
  const url = request.nextUrl;

  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const oauthError = url.searchParams.get("error");

  const expectedState = request.cookies.get(COOKIE.state)?.value;
  const returnTo =
    request.cookies.get(COOKIE.returnTo)?.value || cfg.postLoginPath;

  const secure = url.protocol === "https:";

  // Helper to clear the transient login cookies on any exit path.
  const clearTransient = (res: NextResponse) => {
    res.cookies.set(COOKIE.state, "", { path: "/auth", maxAge: 0 });
    res.cookies.set(COOKIE.returnTo, "", { path: "/auth", maxAge: 0 });
    return res;
  };

  if (oauthError) {
    return clearTransient(redirectToLogin(url, `oauth_error:${oauthError}`));
  }
  if (!code || !state) {
    return clearTransient(redirectToLogin(url, "missing_code_or_state"));
  }
  // CSRF check: the state returned must match what was issued.
  if (!expectedState || state !== expectedState) {
    return clearTransient(redirectToLogin(url, "state_mismatch"));
  }

  let tokens;
  try {
    tokens = await exchangeCodeForToken(cfg, code);
  } catch (err) {
    console.error("Tapis token exchange failed:", err);
    return clearTransient(redirectToLogin(url, "token_exchange_failed"));
  }

  const dest = new URL(returnTo.startsWith("/") ? returnTo : "/", url.origin);
  const response = NextResponse.redirect(dest);

  response.cookies.set(COOKIE.accessToken, tokens.accessToken, {
    httpOnly: true,
    secure,
    sameSite: "lax",
    path: "/",
    expires: tokens.accessExpiresAt,
  });

  return clearTransient(response);
}

function redirectToLogin(url: URL, reason: string): NextResponse {
  const login = new URL("/auth/login", url.origin);
  login.searchParams.set("error", reason);
  return NextResponse.redirect(login);
}
