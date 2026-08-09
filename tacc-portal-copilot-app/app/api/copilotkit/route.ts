import {
  CopilotRuntime,
  ExperimentalEmptyAdapter,
  copilotRuntimeNextJSAppRouterEndpoint,
} from "@copilotkit/runtime";
import { HttpAgent } from "@ag-ui/client";
import { NextRequest, NextResponse } from "next/server";
import { COOKIE } from "@/lib/tapis";

// 1. You can use any service adapter here for multi-agent support. We use
//    the empty adapter since we're only using one agent.
const serviceAdapter = new ExperimentalEmptyAdapter();

// 2. Build a Next.js API route that handles the CopilotKit runtime requests.
//    This route is excluded from the proxy.ts auth gate (see matcher there)
//    because it's an API endpoint, not a page — an unauthenticated caller
//    should get a 401, not a redirect to an HTML login page. So the auth
//    check happens here instead, and doubles as how we get the Tapis token
//    to forward to the FastAPI agent.
export const POST = async (req: NextRequest) => {
  const tapisToken = req.cookies.get(COOKIE.accessToken)?.value;
  if (!tapisToken) {
    return NextResponse.json({ error: "not_authenticated" }, { status: 401 });
  }

  // Built per-request (not module-level) so the X-Tapis-Token header is
  // never shared across different users' concurrent requests.
  //
  // `agents` must be a plain object, not a factory function: the runtime's
  // service-adapter handling does `Object.keys(agents)` to detect whether any
  // agent is registered, and `Object.keys(aFunction)` is [] — which makes it
  // think there are none and throw "No default agent provided".
  const runtime = new CopilotRuntime({
    agents: {
      // Our FastAPI endpoint URL. Forward the Tapis token (from the httpOnly
      // cookie read above) as X-Tapis-Token so the agent acts as this user.
      my_agent: new HttpAgent({
        url: process.env.FASTAPI_URL ?? "http://127.0.0.1:8000",
        headers: { "X-Tapis-Token": tapisToken },
      }),
    },
  });

  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime,
    serviceAdapter,
    endpoint: "/api/copilotkit",
  });

  return handleRequest(req);
};
