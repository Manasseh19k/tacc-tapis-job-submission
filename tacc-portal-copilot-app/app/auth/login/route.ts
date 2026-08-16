import { NextRequest, NextResponse } from "next/server";
import { randomBytes } from "crypto";
import { COOKIE, buildAuthorizeUrl, getConfig } from "@/lib/tapis";

/**
 * First step of the authorization code flow.
 * Generate a random state, remember it in an httpOnly cookie, and redirect the
 * user to the Tapis authorization server.
 */
export async function GET(request: NextRequest) {
  const cfg = getConfig();

  const state = randomBytes(24).toString("hex");
  const authorizeUrl = buildAuthorizeUrl(cfg, state);

  // Preserve where the user was trying to go.
  const returnTo =
    request.nextUrl.searchParams.get("returnTo") || cfg.postLoginPath;

  const response = NextResponse.redirect(authorizeUrl);
  const secure = request.nextUrl.protocol === "https:";

  // `lax` (not `strict`) so the cookie survives the cross-site redirect back
  // from tapis.io to the /auth/callback.
  response.cookies.set(COOKIE.state, state, {
    httpOnly: true,
    secure,
    sameSite: "lax",
    path: "/auth",
    maxAge: 60 * 10, // 10 minutes to complete login
  });
  response.cookies.set(COOKIE.returnTo, returnTo, {
    httpOnly: true,
    secure,
    sameSite: "lax",
    path: "/auth",
    maxAge: 60 * 10,
  });

  return response;
}
