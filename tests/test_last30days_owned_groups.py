"""Real harmless process controls: no provider/network operation."""

import time

import pytest

from test_last30days_supervision import provider_fixture


@pytest.mark.parametrize("wait_mode", ["wait", "poll"])
def test_completed_detached_parent_cannot_leave_same_group_child(tmp_path, wait_mode):
    marker = tmp_path / "escaped-after-reap"
    grandchild = (
        f"import time,pathlib; time.sleep(.9); pathlib.Path({str(marker)!r}).touch()"
    )
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{grandchild!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(.1); sys.exit(7)"
    wait = (
        "code=p.wait()"
        if wait_mode == "wait"
        else "code=None\nwhile code is None:\n code=p.poll(); time.sleep(.01)"
    )
    body = f"import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c',{parent!r}],start_new_session=True)\n{wait}\nassert code==7\ntime.sleep(.1)\nprint('{{}}')\n"
    provider, _ = provider_fixture(tmp_path, body)
    result = provider.get_research_for_symbol("TEST", "TEST stock")
    time.sleep(0.9)
    assert not marker.exists(), (
        "wait/poll reaped the only process-group identity anchor before cleanup"
    )
    diag = result.raw_meta["diagnostics"]
    assert diag["owned_group_cleanup"]["pre_reap_groups_signalled"] == 1
    assert (
        diag["owned_group_cleanup"]["independently_detached_descendants_verified"]
        is False
    )


def test_nonreaping_wait_observation_preserves_exit_status():
    import subprocess
    import sys
    from alphaos.research.last30days_wait import exited_without_reaping

    p = subprocess.Popen([sys.executable, "-c", "raise SystemExit(17)"])
    deadline = time.monotonic() + 2
    while not exited_without_reaping(p.pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert exited_without_reaping(p.pid)
    assert exited_without_reaping(p.pid)  # still ours and waitable
    assert p.wait() == 17
    with pytest.raises(ChildProcessError):
        exited_without_reaping(p.pid)


def test_waitid_does_not_claim_running_child_exited():
    import subprocess
    import sys
    from alphaos.research.last30days_wait import exited_without_reaping

    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(.2)"])
    try:
        assert exited_without_reaping(p.pid) is False
    finally:
        p.wait()
