import { NextRequest, NextResponse } from "next/server";
import { COOKIE } from "@/lib/tapis";

/**
 * Auth gate (Next.js 16 "proxy", formerly "middleware").
 *
 * If there is no Tapis access-token cookie, send the user to /auth/login,
 * preserving where they were headed via ?returnTo=. Everything under /auth is
 * public so the login round-trip can complete.
 */
export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  const hasToken = Boolean(request.cookies.get(COOKIE.accessToken)?.value);
  if (hasToken) return NextResponse.next();

  const login = new URL("/auth/login", request.nextUrl.origin);
  login.searchParams.set("returnTo", pathname + search);
  return NextResponse.redirect(login);
}

export const config = {
  // Run on everything EXCEPT the auth routes.
  matcher: [
    "/((?!auth|api/copilotkit|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
