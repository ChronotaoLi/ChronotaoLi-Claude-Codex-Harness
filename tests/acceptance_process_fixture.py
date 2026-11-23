"""Run the public Harness module with an offline backend and test-only pause points."""
import json
import os
import runpy
import sys
import time
from pathlib import Path
from unittest.mock import patch

from harness import acceptance, cli, git
from harness.adapters import DemoBackend
from harness.run_store import RunStore


def wait_for_release(release):
    deadline = time.monotonic() + 30
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Fixture did not release {release}")
        time.sleep(0.01)


def git_child():
    marker, release, descriptor, *argv = sys.argv[2:]
    stat = os.fstat(int(descriptor))
    temporary = Path(marker + ".tmp")
    temporary.write_text(json.dumps([os.getpid(), stat.st_dev, stat.st_ino]))
    temporary.replace(marker)
    wait_for_release(Path(release))
    os.execvpe(argv[0], argv, os.environ)


def harness_process():
    control, boundary, *args = sys.argv[1:]
    control = Path(control)
    marker, release, calls = (control / name for name in ("ready", "release", "calls"))
    runtime = cli.Runtime
    commit = RunStore.commit
    launch = git._launch

    class Backend(DemoBackend):
        def invoke(self, **kwargs):
            with calls.open("a") as stream:
                stream.write(json.dumps([kwargs["role"], kwargs["kind"]]) + "\n")
            return super().invoke(**kwargs)

    def offline(*runtime_args, **kwargs):
        return runtime(*runtime_args, **kwargs, backend=Backend())

    def pause(label):
        if boundary == label:
            marker.touch()
            wait_for_release(release)

    def commit_at_boundary(store, **kwargs):
        pending = store.run.get("pending_acceptance", {})
        intent_save = "intent" in pending and not marker.exists()
        if intent_save:
            pause("before_intent")
        accepted_save = "pending_post_accept" in store.run and not marker.exists()
        if accepted_save:
            pause("before_acceptance")
        result = commit(store, **kwargs)
        if intent_save:
            pause("after_intent")
        return result

    def after(operation, label):
        def wrapped(*operation_args, **kwargs):
            result = operation(*operation_args, **kwargs)
            pause(label)
            return result
        return wrapped

    def launch_at_boundary(argv, env, *, effect, **kwargs):
        if (effect, boundary) in ((git.REFS, "hold_ref"), (git.INDEX, "hold_index")):
            descriptor, = kwargs["pass_fds"]
            argv = [sys.executable, __file__, "git_child", str(marker), str(release),
                    str(descriptor), *argv]
        return launch(argv, env, effect=effect, **kwargs)

    sys.argv = ["harness", *args]
    with (patch.object(cli, "Runtime", offline),
          patch.object(RunStore, "commit", commit_at_boundary),
          patch.object(git, "_launch", launch_at_boundary),
          patch.object(acceptance, "move_branch", after(acceptance.move_branch, "after_ref")),
          patch.object(acceptance, "sync_index", after(acceptance.sync_index, "after_index"))):
        runpy.run_module("harness", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    if sys.argv[1] == "git_child":
        git_child()
    else:
        harness_process()
