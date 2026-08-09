import { NextRequest, NextResponse } from "next/server";
import { COOKIE, fetchUserInfo, getConfig } from "@/lib/tapis";

/** Return the logged-in Tapis user's profile, or 401 if not authenticated. */
export async function GET(request: NextRequest) {
  const token = request.cookies.get(COOKIE.accessToken)?.value;
  if (!token) {
    return NextResponse.json({ error: "not_authenticated" }, { status: 401 });
  }

  const info = await fetchUserInfo(getConfig(), token);
  if (!info) {
    return NextResponse.json({ error: "invalid_token" }, { status: 401 });
  }

  return NextResponse.json({ user: info });
}
