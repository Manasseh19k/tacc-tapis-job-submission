import { cookies } from "next/headers";
import { CopilotPopup } from "@copilotkit/react-core/v2";
import { JobApprovalCard } from "@/components/JobApprovalCard";
import { JobRequestApprovalCard } from "@/components/JobRequestApprovalCard";
import { CancelJobCard } from "@/components/CancelJobCard";
import { COOKIE, fetchUserInfo, getConfig } from "@/lib/tapis";

// This page is behind the Tapis auth gate (see proxy.ts), so by the time it
// renders the user has a valid token cookie. It looks up their profile to greet
// them and confirm the session works end-to-end.
export default async function Home() {
  const token = (await cookies()).get(COOKIE.accessToken)?.value;
  // console.log("Home page sees token cookie:", token);
  const user = token ? await fetchUserInfo(getConfig(), token) : null;
  const username =
    (user?.username as string | undefined) ??
    (user?.sub as string | undefined) ??
    "unknown user";

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-8 bg-zinc-50 p-8 dark:bg-black">
      <div className="w-full max-w-lg rounded-2xl border border-black/[.08] bg-white p-8 dark:border-white/[.145] dark:bg-zinc-950">
        <h1 className="text-2xl font-semibold text-black dark:text-zinc-50">
          TACC Tapis Portal
        </h1>
        <p className="mt-2 text-zinc-600 dark:text-zinc-400">
          Signed in as{" "}
          <span className="font-medium text-black dark:text-zinc-50">
            {username}
          </span>
          .
        </p>

        <p className="mt-6 text-sm text-zinc-500 dark:text-zinc-500">
          This session&apos;s Tapis token is stored in an httpOnly cookie and is
          available to server routes as the <code>X-Tapis-Token</code> header
          for listing files, submitting jobs, and checking job status.
        </p>

        <div className="mt-8 flex gap-3">
          <a
            href="/auth/logout"
            className="inline-flex h-10 items-center justify-center rounded-full border border-black/[.08] px-5 text-sm font-medium transition-colors hover:bg-black/[.04] dark:border-white/[.145] dark:hover:bg-[#1a1a1a]"
          >
            Log out
          </a>
        </div>
      </div>

      <JobApprovalCard />
      <JobRequestApprovalCard />
      <CancelJobCard />
      <CopilotPopup />
    </div>
  );
}
