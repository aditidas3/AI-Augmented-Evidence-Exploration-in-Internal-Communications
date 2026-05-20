"""
operator_wrapper.py — Decorator for automatic invocation logging.

Wraps any operator function so that start_invocation and finish_invocation
are called automatically. The operator only needs to call log_outcome()
for its internal events.

Usage:
    from orchestration_logging.operator_wrapper import with_invocation_logging

    @with_invocation_logging(logger, run_id, operator_name="ALIGN", stage_order=1)
    def run_align(invocation_id, inputs):
        logger.log_outcome(run_id, invocation_id, "candidate_set", "pool_size", metric_value_num=127)
        ...
        return result
"""

import functools
import time
import traceback
import logging

log = logging.getLogger(__name__)


def with_invocation_logging(
    logger,
    run_id:               str,
    operator_name:        str,
    stage_order:          int,
    config_hash:          str  = "default",
    parent_invocation_id: str  = None,
    attempt_no:           int  = 1,
):
    """
    Decorator factory.

    Input:
        logger               — OrchestrationLogger instance
        run_id               — current run
        operator_name        — name of the operator being wrapped
        stage_order          — position in pipeline
        config_hash          — operator config version
        parent_invocation_id — optional, for nested calls
        attempt_no           — retry count

    The decorated function receives invocation_id as its first argument
    so it can pass it to logger.log_outcome().

    Output (of the decorated function):
        Whatever the original function returned, unchanged.
        On exception: re-raises after logging failure.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            invocation_id = logger.start_invocation(
                run_id               = run_id,
                operator_name        = operator_name,
                stage_order          = stage_order,
                config_hash          = config_hash,
                parent_invocation_id = parent_invocation_id,
                attempt_no           = attempt_no,
            )

            t0 = time.monotonic()
            try:
                result = fn(invocation_id, *args, **kwargs)
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.finish_invocation(
                    invocation_id = invocation_id,
                    status        = "succeeded",
                    latency_ms    = latency_ms,
                )
                return result

            except Exception as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.log_outcome(
                    run_id        = run_id,
                    invocation_id = invocation_id,
                    outcome_kind  = "failure_event",
                    outcome_name  = f"{operator_name}_exception",
                    severity      = "critical",
                    payload       = {
                        "exception_type":    type(exc).__name__,
                        "exception_message": str(exc),
                        "traceback":         traceback.format_exc(),
                    },
                )
                logger.finish_invocation(
                    invocation_id = invocation_id,
                    status        = "failed",
                    latency_ms    = latency_ms,
                    error_code    = type(exc).__name__,
                    error_message = str(exc),
                )
                raise

        return wrapper
    return decorator
