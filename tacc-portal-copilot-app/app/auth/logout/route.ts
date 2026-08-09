import { NextRequest, NextResponse } from "next/server";
import { COOKIE, getConfig, revokeToken } from "@/lib/tapis";

/**
 * Revoke the Tapis tokens (best-effort) and clear the session cookies.
 * Supports GET (link) and POST (form/fetch).
 */
async function handleLogout(request: NextRequest) {
  const cfg = getConfig();

  const accessToken = request.cookies.get(COOKIE.accessToken)?.value;

  if (accessToken) {
    await revokeToken(cfg, accessToken);
  }

  const response = NextResponse.redirect(new URL("/auth/login", request.nextUrl.origin));
  response.cookies.set(COOKIE.accessToken, "", { path: "/", maxAge: 0 });
  return response;
}

export const GET = handleLogout;
export const POST = handleLogout;
