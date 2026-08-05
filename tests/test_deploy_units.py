from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "production" / "v1"


def test_processing_worker_is_restricted_to_bounded_fca_queue_command():
    service = (DEPLOY / "govscout-processing.service").read_text()

    assert "User=govscout" in service
    assert "EnvironmentFile=/etc/govscout/govscout.env" in service
    assert "process-fca-queue --limit 1" in service
    assert "TimeoutStartSec=10min" in service
    assert "TimeoutStopSec=30s" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/var/lib/govscout" in service
    assert " draft" not in service
    assert " send" not in service


def test_processing_timer_runs_persistently_without_overlapping_service_instances():
    timer = (DEPLOY / "govscout-processing.timer").read_text()

    assert "OnUnitInactiveSec=60s" in timer
    assert "Persistent=true" in timer
    assert "Unit=govscout-processing.service" in timer


def test_upgrade_runbook_stops_processing_and_backs_up_before_release_switch():
    runbook = (DEPLOY / "RUNBOOK.md").read_text()

    stop_timer = runbook.index("disable --now govscout-processing.timer")
    backup = runbook.index("cp --preserve=mode,ownership,timestamps")
    release_switch = runbook.index("ln -sfn /opt/govscout/releases/<version>")
    login_probe = runbook.index("https://leads.misegroup.co.uk/login >/dev/null")
    protected_probe = runbook.index("https://leads.misegroup.co.uk/today")
    listener_probe = runbook.index("ss -ltnp | grep 127.0.0.1:8766")
    enable_timer = runbook.index("enable --now govscout-processing.timer")
    timer_active = runbook.index("is-active govscout-processing.timer")
    timer_listed = runbook.index("list-timers govscout-processing.timer")

    assert (
        stop_timer
        < backup
        < release_switch
        < login_probe
        < protected_probe
        < listener_probe
        < enable_timer
        < timer_active
        < timer_listed
    )
