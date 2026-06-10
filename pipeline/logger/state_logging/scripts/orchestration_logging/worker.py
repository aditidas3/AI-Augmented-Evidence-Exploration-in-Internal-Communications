"""
worker.py — Background logger worker thread.

The main agent thread never writes to the database directly.
It puts events into the shared queue and returns immediately.
This worker runs in the background, batches events, and flushes
to PostgreSQL according to the flush policy.

Flush triggers:
    1. Severity is WARNING or CRITICAL  → flush immediately
    2. Operator finishes                → FLUSH signal sent by service.py
    3. Batch reaches BATCH_SIZE         → flush to cap RAM usage
    4. Run ends                         → DRAIN signal flushes everything
    5. Unhandled exception              → best-effort flush before dying
"""

import json
import logging
import queue
import threading
from typing import Any

import psycopg2.extras

from . import db

log = logging.getLogger(__name__)

# Maximum events to hold before forcing a flush
BATCH_SIZE = 50

# Sentinel signals placed on the queue by service.py
SIGNAL_FLUSH = "__FLUSH__"   # flush now (operator finished)
SIGNAL_DRAIN = "__DRAIN__"   # flush everything and stop (run ended)


class LoggerWorker(threading.Thread):
    """
    Background thread that owns all PostgreSQL writes.

    Input:  items placed on self.queue by OrchestrationLogger
    Output: bulk INSERTs into intermediate_outcome_event via bulk_log_outcomes()

    Other tables (operator_invocation, orchestration_state_snapshot,
    orchestration_decision) are written directly and synchronously by
    service.py because they are infrequent and must be committed before
    the next operator starts.
    """

    def __init__(self, event_queue: queue.Queue) -> None:
        super().__init__(name="aex-logger-worker", daemon=True)
        self.queue   = event_queue
        self._batch  = [] #: list[dict] = []
        self._stopped= threading.Event() #self._stop   = threading.Event()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        log.debug("LoggerWorker started.")
        while not self._stopped.is_set():  #updated to self._stopped from self._stop
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is SIGNAL_DRAIN:
                # Flush everything and exit
                self._flush()
                self.queue.task_done()
                break

            if item is SIGNAL_FLUSH:
                self._flush()
                self.queue.task_done()
                continue

            # Normal event dict
            self._batch.append(item)
            self.queue.task_done()

            # Check flush conditions
            severity = item.get("severity", "info")
            if severity in ("warning", "critical"):
                log.debug("Immediate flush triggered by severity=%s", severity)
                self._flush()
            elif len(self._batch) >= BATCH_SIZE:
                log.debug("Batch size flush triggered (%d events).", BATCH_SIZE)
                self._flush()

        log.debug("LoggerWorker stopped.")

    def stop(self) -> None:
        """Signal the worker to drain and stop. Blocks until done."""
        self.queue.put(SIGNAL_DRAIN)
        self.join(timeout=10)

    # ------------------------------------------------------------------
    # Flush
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """
        Input:  self._batch — list of event dicts accumulated since last flush
        Output: nothing — performs a single bulk INSERT then clears the batch
        """
        if not self._batch:
            return

        batch = self._batch
        self._batch = []

        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT bulk_log_outcomes(%s::jsonb)",
                    (json.dumps(batch),)
                )
            conn.commit()
            log.debug("Flushed %d events to DB.", len(batch))
        except Exception:
            conn.rollback()
            log.exception("Flush failed — %d events may be lost.", len(batch))
        finally:
            db.put_conn(conn)
