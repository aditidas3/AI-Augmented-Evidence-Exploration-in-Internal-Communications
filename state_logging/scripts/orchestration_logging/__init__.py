"""
orchestration_logging — AEX adaptive orchestration logging layer.

Quick start:
    import orchestration_logging.db as db
    from orchestration_logging.service import OrchestrationLogger
    from orchestration_logging.operator_loggers import AlignLogger, TraceLogger

    db.init_pool()
    logger = OrchestrationLogger()
    logger.start()

    run_id = logger.create_run(intent_object={...}, corpus_snapshot_id="v1", config_hash="abc123")
    logger.start_run(run_id)
    ...
    logger.finish_run(run_id, "completed")
    logger.stop()
    db.close_pool()
"""
