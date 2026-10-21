"""Tag a request's log lines with its job, so they reach the job's log.

The LogTailer copies a Backend line into `/logs/jobs/<job_id>.log` and the
`logs.<job_id>` topic only when the line carries a `job_id`. A router that
serves job routes depends on `job_log_context`: a `{job_id}` path parameter
binds the job for the whole request, and a handler that finds its job later
(`/identify` creates it, a transcode task route looks it up) calls
`bind_log_context(job_id=...)` once it has it. The scope undoes either when
the request ends.
"""

from collections.abc import AsyncIterator

from fastapi import Request

from arm_common import bind_log_context, log_context_scope


async def job_log_context(request: Request) -> AsyncIterator[None]:
    with log_context_scope():
        job_id = request.path_params.get("job_id")
        if isinstance(job_id, str):
            bind_log_context(job_id=job_id)
        yield
