"""CRON-1: in a restart-safe worker the recovery notice and the run's own result are two queue
rows. The notice used to take the execution id, and the result's INSERT OR IGNORE dropped it."""
from cron import executions, jobs, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig


def test_external_worker_delivers_recovery_notice_and_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    sent = []

    async def send(platform, pconfig, chat_id, text, **kwargs):
        sent.append(text)
        return {"success": True, "message_id": "receipt"}

    monkeypatch.setattr("tools.send_message_tool._send_to_platform", send)
    monkeypatch.setattr("cron.incidents.count_alerted_incidents", lambda job_id: 1)
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: (True, "raw", "the real output", None))
    job = jobs.create_job(prompt="fixture only", schedule="every 1h", deliver="telegram:fixture")
    jobs.update_job(job["id"], {"alert_after_consecutive_failures": 2, "failure_streak": 3})
    job = jobs.get_job(job["id"])
    execution = executions.create_execution(job["id"], source="fixture")
    job["execution_id"] = execution["id"]
    monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", execution["id"])
    from cron import delivery_queue
    original_wait = delivery_queue.enqueue_and_wait

    def drain_before_wait(execution_id, job, content, *, for_failure=False):
        delivery_queue.enqueue(execution_id, job, content, for_failure=for_failure)
        scheduler.drain_delivery_queue({}, None)
        return original_wait(execution_id, job, content, for_failure=for_failure)

    monkeypatch.setattr(delivery_queue, "enqueue_and_wait", drain_before_wait)
    scheduler.run_one_job(job)
    scheduler.drain_delivery_queue({}, None)
    assert len(sent) == 2, sent
    assert "recovered after 3 failed runs" in sent[0]
    assert "the real output" in sent[1]
    assert executions.latest_execution(job["id"])["delivery_outcome"] == "delivered"
