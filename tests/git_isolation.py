"""Keep the developer's Git configuration away from the tests and from every process they start.

``test_harness`` and ``run_shards`` import this first, so the single-process run and every shard are
isolated. On import it points HOME, XDG_CONFIG_HOME and GIT_CONFIG_GLOBAL at an empty private folder,
sets GIT_CONFIG_NOSYSTEM=1 and drops every other GIT_* variable. Git before 2.32 ignores
GIT_CONFIG_GLOBAL and reads $HOME/.gitconfig and $XDG_CONFIG_HOME/git/config: the same private file and
an absent one. HOME, XDG_CONFIG_HOME and the GIT_CONFIG_* selectors pass the Harness environment
filter, so Harness's own Git calls and ``python -m harness`` children are isolated as well.
"""
import atexit
import contextlib
import os
import shutil
import tempfile
from pathlib import Path

ORIGINAL_HOME = os.environ.get("HOME")
FOLDER = Path(tempfile.mkdtemp(prefix="harness-test-git-"))
HOME = FOLDER / "home"
XDG_CONFIG_HOME = FOLDER / "xdg"
GLOBAL_CONFIG = HOME / ".gitconfig"
# The variables that choose which configuration files Git reads; the Harness construction keeps them too.
SELECTORS = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")

HOME.mkdir()
XDG_CONFIG_HOME.mkdir()
GLOBAL_CONFIG.write_text("")
_CREATOR = os.getpid()


@atexit.register
def _remove():
    if os.getpid() == _CREATOR:  # a forked child exiting must not take the folder from its parent
        shutil.rmtree(FOLDER, ignore_errors=True)


for _key in [key for key in os.environ if key.startswith("GIT_")]:
    del os.environ[_key]
os.environ.update(HOME=str(HOME), XDG_CONFIG_HOME=str(XDG_CONFIG_HOME), GIT_CONFIG_GLOBAL=str(GLOBAL_CONFIG),
                  GIT_CONFIG_NOSYSTEM="1")


@contextlib.contextmanager
def user_config(text):
    """Use ``text`` as the user's global Git configuration for the duration; emptied afterwards."""
    GLOBAL_CONFIG.write_text(text)
    try:
        yield GLOBAL_CONFIG
    finally:
        GLOBAL_CONFIG.write_text("")


def clean_environment():
    """``os.environ`` without the GIT_* variables a test added, keeping the configuration selectors."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_") or key in SELECTORS}


def fixture_git(repo, *args, input=None):
    """A test fixture's Git call through the Harness construction, with no declared effect."""
    from harness import git  # run_shards runs as a script, without the repository root on sys.path
    return git.git(repo, *args, input=input)
