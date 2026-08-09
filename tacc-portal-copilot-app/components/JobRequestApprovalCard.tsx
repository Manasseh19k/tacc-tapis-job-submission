"use client";

import { useInterrupt } from "@copilotkit/react-core/v2";

/**
 * Claims the interrupt raised by `submit_job_request` (app/tools/jobs.py),
 * whose metadata is `{ job_request: <raw Tapis ReqSubmitJob object> }`. This
 * is the raw-JSON escape hatch beside the structured JobApprovalCard; it shows
 * the exact JSON that will be POSTed so the user can approve or reject what
 * they (possibly) hand-edited.
 */
interface JobRequestMeta {
  job_request: Record<string, unknown>;
}

/** Structural discriminator. `job_request` (an object) is present here and on
 * neither JobSpec (which has top-level snake_case fields) nor the cancel
 * target (which has `job_uuid`), so the three cards never collide. */
function isJobRequest(metadata: unknown): metadata is JobRequestMeta {
  const m = metadata as Record<string, unknown> | undefined;
  return (
    !!m &&
    typeof m.job_request === "object" &&
    m.job_request !== null &&
    !Array.isArray(m.job_request)
  );
}

export function JobRequestApprovalCard() {
  useInterrupt({
    enabled: (event) => isJobRequest(event.value?.metadata),
    render: ({ resolve, cancel, interrupt }) => {
      const meta = interrupt?.metadata as JobRequestMeta | undefined;
      if (!meta) return <></>;

      const json = JSON.stringify(meta.job_request, null, 2);
      const appId = String(
        (meta.job_request as Record<string, unknown>).appId ?? "job",
      );

      return (
        <div className="my-2 w-full max-w-md rounded-2xl border border-black/[.08] bg-white p-5 dark:border-white/[.145] dark:bg-zinc-950">
          <h3 className="text-base font-semibold text-black dark:text-zinc-50">
            Review job request
          </h3>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            This exact JSON will be sent to the Tapis Jobs API for{" "}
            <span className="font-medium text-black dark:text-zinc-50">
              {appId}
            </span>
            .
          </p>

          <pre className="mt-3 max-h-72 overflow-auto rounded-lg bg-zinc-100 p-3 text-xs leading-relaxed text-zinc-800 dark:bg-zinc-900 dark:text-zinc-200">
            {json}
          </pre>

          <div className="mt-5 flex gap-3">
            <button
              onClick={() => resolve({ approved: true })}
              className="inline-flex h-10 flex-1 items-center justify-center rounded-full bg-black px-5 text-sm font-medium text-white transition-colors hover:bg-black/80 dark:bg-white dark:text-black dark:hover:bg-white/80"
            >
              Submit Job
            </button>
            <button
              onClick={() => cancel()}
              className="inline-flex h-10 flex-1 items-center justify-center rounded-full border border-black/[.08] px-5 text-sm font-medium transition-colors hover:bg-black/[.04] dark:border-white/[.145] dark:hover:bg-[#1a1a1a]"
            >
              Cancel
            </button>
          </div>
        </div>
      );
    },
  });

  return null;
}
