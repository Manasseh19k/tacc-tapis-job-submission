"use client";

import { useInterrupt } from "@copilotkit/react-core/v2";

/**
 * Mirrors the backend's `JobSpec` dataclass (app/tools/jobs.py). Field names
 * are kept snake_case on purpose: `interrupt.metadata` is a passthrough
 * `Record<string, any>` on the wire, not a modeled/camelCased field, so these
 * keys are exactly what `dataclasses.asdict(spec)` produces.
 */
interface JobSpec {
  app_id: string;
  app_version: string;
  system_id: string;
  name: string;
  node_count: number;
  cores_per_node: number;
  max_minutes: number;
  queue: string | null;
  parameters: Record<string, string>;
  file_inputs: Record<string, string>[];
  archive_system_id: string | null;
}

/** Structural check, this is how to tell a submit_job interrupt apart from
 * any other approval-gated tool, without parsing
 * the human-readable interrupt message. */
function isJobSpec(metadata: unknown): metadata is JobSpec {
  const m = metadata as Record<string, unknown> | undefined;
  return (
    !!m &&
    typeof m.app_id === "string" &&
    typeof m.system_id === "string" &&
    typeof m.node_count === "number"
  );
}

const row = "flex items-baseline justify-between gap-4";
const label = "text-zinc-500 dark:text-zinc-400";
const value = "text-right font-medium text-black dark:text-zinc-50";

/**
 * Gen-UI approval card for the submit_job interrupt. Mounted once, anywhere
 * under <CopilotKit>; renders itself inline in the chat via useInterrupt's
 * default renderInChat behavior.
 */
export function JobApprovalCard() {
  useInterrupt({
    enabled: (event) => isJobSpec(event.value?.metadata),
    render: ({ resolve, cancel, interrupt }) => {
      const spec = interrupt?.metadata as JobSpec | undefined;
      if (!spec) return <></>;

      const parameterEntries = Object.entries(spec.parameters ?? {});

      return (
        <div className="my-2 w-full max-w-md rounded-2xl border border-black/[.08] bg-white p-5 dark:border-white/[.145] dark:bg-zinc-950">
          <h3 className="text-base font-semibold text-black dark:text-zinc-50">
            Review job submission
          </h3>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            Nothing runs until you approve this exact spec.
          </p>

          <dl className="mt-4 space-y-1.5 text-sm">
            <div className={row}>
              <dt className={label}>Application</dt>
              <dd className={value}>
                {spec.app_id} · {spec.app_version}
              </dd>
            </div>
            <div className={row}>
              <dt className={label}>System</dt>
              <dd className={value}>{spec.system_id}</dd>
            </div>
            <div className={row}>
              <dt className={label}>Job name</dt>
              <dd className={value}>{spec.name}</dd>
            </div>
            <div className={row}>
              <dt className={label}>Resources</dt>
              <dd className={value}>
                {spec.node_count} node{spec.node_count === 1 ? "" : "s"} ×{" "}
                {spec.cores_per_node} core{spec.cores_per_node === 1 ? "" : "s"}
              </dd>
            </div>
            <div className={row}>
              <dt className={label}>Wall time</dt>
              <dd className={value}>{spec.max_minutes} min</dd>
            </div>
            {spec.queue && (
              <div className={row}>
                <dt className={label}>Queue</dt>
                <dd className={value}>{spec.queue}</dd>
              </div>
            )}
            {spec.archive_system_id && (
              <div className={row}>
                <dt className={label}>Archive system</dt>
                <dd className={value}>{spec.archive_system_id}</dd>
              </div>
            )}
          </dl>

          {parameterEntries.length > 0 && (
            <div className="mt-4 border-t border-black/[.08] pt-3 dark:border-white/[.145]">
              <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                Parameters
              </p>
              <dl className="mt-1.5 space-y-1 text-sm">
                {parameterEntries.map(([key, val]) => (
                  <div key={key} className={row}>
                    <dt className={label}>{key}</dt>
                    <dd className={value}>{val}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}

          {spec.file_inputs?.length > 0 && (
            <div className="mt-4 border-t border-black/[.08] pt-3 dark:border-white/[.145]">
              <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                File inputs
              </p>
              <ul className="mt-1.5 space-y-1 text-sm text-zinc-600 dark:text-zinc-400">
                {spec.file_inputs.map((mapping, i) => (
                  <li key={i} className="truncate">
                    {Object.values(mapping).join(" → ")}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="mt-5 flex gap-3">
            <button
              onClick={() => resolve({ approved: true })}
              className="inline-flex h-10 flex-1 items-center justify-center rounded-full bg-black px-5 text-sm font-medium text-white transition-colors hover:bg-black/80 dark:bg-white dark:text-black dark:hover:bg-white/80"
            >
              Run Job
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
