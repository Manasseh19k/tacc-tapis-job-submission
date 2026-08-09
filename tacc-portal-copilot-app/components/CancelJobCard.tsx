"use client";

import { useInterrupt } from "@copilotkit/react-core/v2";

/**
 * Mirrors the metadata dict built in `cancel_job` (app/tools/jobs.py):
 * `{"job_uuid": ..., "name": ..., "status": ...}`. snake_case on purpose —
 * `interrupt.metadata` is a passthrough Record, not a camelCased model.
 */
interface CancelTarget {
  job_uuid: string;
  name: string;
  status: string;
}

/** Structural discriminator, same approach as JobApprovalCard: `job_uuid` is
 * present on cancel metadata and absent from JobSpec, so the two cards can
 * never both claim the same interrupt. */
function isCancelTarget(metadata: unknown): metadata is CancelTarget {
  const m = metadata as Record<string, unknown> | undefined;
  return !!m && typeof m.job_uuid === "string" && typeof m.status === "string";
}

const row = "flex items-baseline justify-between gap-4";
const label = "text-zinc-500 dark:text-zinc-400";
const value = "text-right font-medium text-black dark:text-zinc-50";

export function CancelJobCard() {
  useInterrupt({
    enabled: (event) => isCancelTarget(event.value?.metadata),
    render: ({ resolve, cancel, interrupt }) => {
      const target = interrupt?.metadata as CancelTarget | undefined;
      if (!target) return <></>;

      return (
        <div className="my-2 w-full max-w-md rounded-2xl border border-red-500/30 bg-white p-5 dark:bg-zinc-950">
          <h3 className="text-base font-semibold text-black dark:text-zinc-50">
            Cancel this job?
          </h3>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            This stops work already in progress and cannot be undone. Queue time
            already spent is not recovered.
          </p>

          <dl className="mt-4 space-y-1.5 text-sm">
            <div className={row}>
              <dt className={label}>Job</dt>
              <dd className={value}>{target.name || "(unnamed)"}</dd>
            </div>
            <div className={row}>
              <dt className={label}>Current status</dt>
              <dd className={value}>{target.status}</dd>
            </div>
            <div className={row}>
              <dt className={label}>UUID</dt>
              <dd className={`${value} font-mono text-xs`}>
                {target.job_uuid}
              </dd>
            </div>
          </dl>

          <div className="mt-5 flex gap-3">
            {/* Destructive action is the secondary button on purpose: the
                safe choice should be the one you hit by reflex. */}
            <button
              onClick={() => cancel()}
              className="inline-flex h-10 flex-1 items-center justify-center rounded-full bg-black px-5 text-sm font-medium text-white transition-colors hover:bg-black/80 dark:bg-white dark:text-black dark:hover:bg-white/80"
            >
              Keep running
            </button>
            <button
              onClick={() => resolve({ approved: true })}
              className="inline-flex h-10 flex-1 items-center justify-center rounded-full border border-red-500/40 px-5 text-sm font-medium text-red-600 transition-colors hover:bg-red-500/[.06] dark:text-red-400"
            >
              Cancel job
            </button>
          </div>
        </div>
      );
    },
  });

  return null;
}
