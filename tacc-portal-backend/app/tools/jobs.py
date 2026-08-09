import logging
from dataclasses import asdict, dataclass
from typing import Any

from pydantic_ai import ApprovalRequired, ModelRetry, RunContext

from app.deps import AgentDeps
from app.tapis_client import TapisClientError, summarize_tapis_error
from app.tools.files import list_files, read_file

logger = logging.getLogger(__name__)

# Hard cap on how many jobs list_jobs will fetch from Tapis when compensating
# for the lack of a server-side status filter (see list_jobs docstring).
_MAX_JOB_LIST_FETCH = 200


def _job_to_status(job: Any, fallback_uuid: str = "") -> "JobStatus":
    """Project a Tapis ``Job`` object (as returned by getJob/submitJob) to a
    :class:`JobStatus`. Read every field with ``TapisResult.get``, since the
    ``Job`` schema marks nothing required.
    """
    return JobStatus(
        uuid=job.get("uuid", fallback_uuid),
        name=job.get("name", ""),
        status=job.get("status", ""),
        created=job.get("created", ""),
        ended=job.get("ended"),
        last_message=job.get("lastMessage"),
        remote_job_id=job.get("remoteJobId"),
    )


def _build_submit_body(spec: "JobSpec") -> dict[str, Any]:
    """Translate a :class:`JobSpec` into a Tapis ``ReqSubmitJob`` body.

    Optional fields are only included when set, so Tapis falls back to the
    app/system defaults for anything the spec left blank rather than being
    handed empty overrides. ``spec.parameters`` (a name->value dict) maps to
    ``parameterSet.appArgs`` — the app-argument channel; container args and
    scheduler options are not expressible through the current flat
    ``JobSpec`` and would need a richer spec shape to support.
    """
    body: dict[str, Any] = {
        "name": spec.name,
        "appId": spec.app_id,
        "appVersion": spec.app_version,
        "execSystemId": spec.system_id,
        "nodeCount": spec.node_count,
        "coresPerNode": spec.cores_per_node,
        "maxMinutes": spec.max_minutes,
    }
    if spec.queue:
        body["execSystemLogicalQueue"] = spec.queue
    if spec.archive_system_id:
        body["archiveSystemId"] = spec.archive_system_id
    if spec.parameters:
        body["parameterSet"] = {
            "appArgs": [
                {"name": name, "arg": value} for name, value in spec.parameters.items()
            ]
        }
    if spec.file_inputs:
        body["fileInputs"] = [
            {
                "name": fi.get("name", ""),
                "sourceUrl": fi.get("source_url", ""),
                "targetPath": fi.get("target_path", ""),
            }
            for fi in spec.file_inputs
        ]
    return body


@dataclass
class JobSpec:
    app_id: str
    app_version: str
    system_id: str
    name: str
    node_count: int
    cores_per_node: int
    max_minutes: int
    queue: str | None
    parameters: dict[str, str]
    file_inputs: list[dict[str, str]]
    archive_system_id: str | None


@dataclass
class JobStatus:
    uuid: str
    name: str
    status: str
    created: str
    ended: str | None
    last_message: str | None
    remote_job_id: str | None


async def list_apps(ctx: RunContext[AgentDeps], system_id: str | None = None) -> list[dict[str, str]]:
    try:
        apps = ctx.deps.tapis.apps.getApps(
            listType="ALL", select="id,version,description,jobAttributes"
        )
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    results: list[dict[str, str]] = []
    for app in apps:
        if system_id is not None:
            job_attributes = app.get("jobAttributes")
            exec_system_id = job_attributes.get("execSystemId") if job_attributes else None
            if exec_system_id != system_id:
                continue
        results.append(
            {
                "id": app.get("id", ""),
                "version": app.get("version", ""),
                "description": app.get("description") or "",
            }
        )
    return results


async def describe_app(ctx: RunContext[AgentDeps], app_id: str, app_version: str) -> dict[str, object]:
    try:
        app = ctx.deps.tapis.apps.getApp(appId=app_id, appVersion=app_version)
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    job_attributes = app.get("jobAttributes")
    parameter_set = job_attributes.get("parameterSet") if job_attributes else None

    def _arg_specs(args: list | None) -> list[dict[str, object]]:
        return [
            {
                "name": a.get("name", ""),
                "description": a.get("description") or "",
                "input_mode": a.get("inputMode", ""),
                "default": a.get("arg"),
            }
            for a in (args or [])
        ]

    def _file_inputs(file_inputs: list | None) -> list[dict[str, object]]:
        return [
            {
                "name": f.get("name", ""),
                "description": f.get("description") or "",
                "input_mode": f.get("inputMode", ""),
                "default_source_url": f.get("sourceUrl"),
                "target_path": f.get("targetPath", ""),
            }
            for f in (file_inputs or [])
        ]

    return {
        "id": app.get("id", ""),
        "version": app.get("version", ""),
        "description": app.get("description") or "",
        "exec_system_id": job_attributes.get("execSystemId") if job_attributes else None,
        "queue": job_attributes.get("execSystemLogicalQueue") if job_attributes else None,
        "archive_system_id": job_attributes.get("archiveSystemId") if job_attributes else None,
        "node_count": job_attributes.get("nodeCount") if job_attributes else None,
        "cores_per_node": job_attributes.get("coresPerNode") if job_attributes else None,
        "max_minutes": job_attributes.get("maxMinutes") if job_attributes else None,
        "app_args": _arg_specs(parameter_set.get("appArgs") if parameter_set else None),
        "container_args": _arg_specs(parameter_set.get("containerArgs") if parameter_set else None),
        "scheduler_options": _arg_specs(parameter_set.get("schedulerOptions") if parameter_set else None),
        "env_variables": [
            {"key": kv.get("key", ""), "value": kv.get("value")}
            for kv in ((parameter_set.get("envVariables") if parameter_set else None) or [])
        ],
        "file_inputs": _file_inputs(job_attributes.get("fileInputs") if job_attributes else None),
    }


async def validate_job_spec(ctx: RunContext[AgentDeps], spec: JobSpec) -> list[str]:
    problems: list[str] = []

    try:
        app = await describe_app(ctx, spec.app_id, spec.app_version)
    except TapisClientError as exc:
        return [f"Could not load app {spec.app_id} v{spec.app_version}: {exc}"]

    for arg_group in ("app_args", "container_args", "scheduler_options"):
        for arg in app[arg_group]:
            if arg["input_mode"] == "REQUIRED" and arg["name"] not in spec.parameters:
                problems.append(f"Missing required parameter: {arg['name']}")

    spec_file_input_names = {fi.get("name") for fi in spec.file_inputs}
    for file_input in app["file_inputs"]:
        if file_input["input_mode"] == "REQUIRED" and file_input["name"] not in spec_file_input_names:
            problems.append(f"Missing required file input: {file_input['name']}")

    system = None
    try:
        system = ctx.deps.tapis.systems.getSystem(systemId=spec.system_id)
    except Exception as exc:
        problems.append(f"Could not load system {spec.system_id}: {summarize_tapis_error(exc)}")

    if system is not None:
        queue_name = spec.queue or system.get("batchDefaultLogicalQueue")
        queues = system.get("batchLogicalQueues") or []
        queue = next((q for q in queues if q.get("name") == queue_name), None)
        if queue_name and queue is None:
            problems.append(f"No such queue {queue_name!r} on system {spec.system_id}.")
        elif queue is not None:
            problems.extend(
                _queue_bounds_problem(
                    "node_count", spec.node_count, queue.get("minNodeCount"), queue.get("maxNodeCount")
                )
            )
            problems.extend(
                _queue_bounds_problem(
                    "cores_per_node", spec.cores_per_node, queue.get("minCoresPerNode"), queue.get("maxCoresPerNode")
                )
            )
            problems.extend(
                _queue_bounds_problem(
                    "max_minutes", spec.max_minutes, queue.get("minMinutes"), queue.get("maxMinutes")
                )
            )

    for file_input in spec.file_inputs:
        source = file_input.get("source_url") or ""
        if not source.startswith("tapis://"):
            continue
        source_system, _, source_path = source.removeprefix("tapis://").partition("/")
        try:
            entries = await list_files(ctx, system_id=source_system, path="/" + source_path)
        except TapisClientError as exc:
            problems.append(f"Could not verify input file {source}: {exc}")
            continue
        if not entries:
            problems.append(f"Input file not found: {source}")

    return problems


def _queue_bounds_problem(
    field: str, value: int, minimum: int | None, maximum: int | None
) -> list[str]:
    """One-item list with a plain-language problem if value is outside [minimum, maximum].

    Either bound may be absent (Tapis treats an unset min/max as unconstrained
    on that side), so each is only checked when present.
    """
    if minimum is not None and value < minimum:
        return [f"{field}={value} is below the queue's minimum of {minimum}."]
    if maximum is not None and value > maximum:
        return [f"{field}={value} exceeds the queue's maximum of {maximum}."]
    return []


async def submit_job(ctx: RunContext[AgentDeps], spec: JobSpec) -> JobStatus:
    if not ctx.tool_call_approved:
        raise ApprovalRequired(metadata=asdict(spec))

    body = _build_submit_body(spec)
    try:
        job = ctx.deps.tapis.jobs.submitJob(**body)
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    status = _job_to_status(job)
    # Audit record: a human approved this specific run.
    logger.info(
        "job submitted by user=%s uuid=%s name=%s app=%s/%s",
        ctx.deps.user.username, status.uuid, status.name, spec.app_id, spec.app_version,
    )
    return status


async def build_job_request(ctx: RunContext[AgentDeps], spec: JobSpec) -> dict[str, Any]:
    return _build_submit_body(spec)


async def submit_job_request(
    ctx: RunContext[AgentDeps], job_request: dict[str, Any]
) -> JobStatus:
    missing = [f for f in ("appId", "appVersion", "name") if not job_request.get(f)]
    if missing:
        raise ModelRetry(
            f"The job request is missing required field(s): {', '.join(missing)}. "
            "Ask the user to add them to the JSON; do not guess the values."
        )

    if not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"job_request": job_request})

    try:
        job = ctx.deps.tapis.jobs.submitJob(**job_request)
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    status = _job_to_status(job)
    # Audit record: a human approved this specific raw request.
    logger.info(
        "raw job request submitted by user=%s uuid=%s name=%s",
        ctx.deps.user.username, status.uuid, status.name,
    )
    return status


async def get_job_status(ctx: RunContext[AgentDeps], job_uuid: str) -> JobStatus:
    try:
        job = ctx.deps.tapis.jobs.getJob(jobUuid=job_uuid)
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    return _job_to_status(job, fallback_uuid=job_uuid)


async def list_jobs(ctx: RunContext[AgentDeps], limit: int = 20, status: str | None = None) -> list[JobStatus]:
    fetch_limit = min(limit * 5, _MAX_JOB_LIST_FETCH) if status else limit
    try:
        jobs = ctx.deps.tapis.jobs.getJobList(limit=fetch_limit, orderBy="created(desc)")
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    results: list[JobStatus] = []
    for job in jobs:
        job_status = job.get("status", "")
        if status is not None and job_status != status:
            continue
        results.append(
            JobStatus(
                uuid=job.get("uuid", ""),
                name=job.get("name", ""),
                status=job_status,
                created=job.get("created", ""),
                ended=job.get("ended"),
                last_message=None,
                remote_job_id=None,
            )
        )
        if len(results) >= limit:
            break
    return results


async def get_job_output(ctx: RunContext[AgentDeps], job_uuid: str, path: str = "") -> str:
    try:
        job = ctx.deps.tapis.jobs.getJob(jobUuid=job_uuid)
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    archive_system_id = job.get("archiveSystemId")
    archive_dir = job.get("archiveSystemDir")
    if not archive_system_id or not archive_dir:
        return f"Job {job_uuid} has no archived output yet (status: {job.get('status', 'unknown')})."

    def _join(directory: str, name: str) -> str:
        return f"{directory.rstrip('/')}/{name}"

    if path:
        return await read_file(ctx, archive_system_id, _join(archive_dir, path))

    sections: list[str] = []
    for filename in ("tapisjob.out", "tapisjob.err"):
        try:
            content = await read_file(ctx, archive_system_id, _join(archive_dir, filename))
        except TapisClientError as exc:
            sections.append(f"--- {filename} ---\n(could not read: {exc})")
            continue
        sections.append(f"--- {filename} ---\n{content}")
    return "\n\n".join(sections)


async def cancel_job(ctx: RunContext[AgentDeps], job_uuid: str) -> JobStatus:
    if not ctx.tool_call_approved:
        current = await get_job_status(ctx, job_uuid)
        raise ApprovalRequired(
            metadata={"job_uuid": current.uuid, "name": current.name, "status": current.status}
        )

    try:
        ctx.deps.tapis.jobs.cancelJob(jobUuid=job_uuid)
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    # Audit record: a human approved cancelling this specific job.
    logger.info("job cancel approved by user=%s uuid=%s", ctx.deps.user.username, job_uuid)

    # cancelJob's response carries no job payload (just the status envelope),
    # so re-fetch to return the post-cancel state.
    return await get_job_status(ctx, job_uuid)
