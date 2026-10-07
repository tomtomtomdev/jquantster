"""run.sh setup behaviour, in a temp copy of the repo with a stub `uv` (no network)."""
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
JQ_KEY = "jq-key$with&odd'chars"
ED_KEY = "ed key=with$odd&chars"

STUB_UV = """#!/bin/sh
# Stub uv: `uv run [--quiet] python …` runs Python, everything else is logged and succeeds.
case "$1" in
  run)
    shift
    [ "$1" = --quiet ] && shift
    if [ "$1" = python ]; then shift; exec "{py}" "$@"; fi ;;
esac
echo "uv $*" >> uv.log
"""


def test_run_sh_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "run.sh")], check=True)


@pytest.fixture
def repo(tmp_path):
    shutil.copy(ROOT / "run.sh", tmp_path)
    shutil.copy(ROOT / ".env.example", tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(STUB_UV.format(py=sys.executable))
    uv.chmod(0o755)
    return tmp_path


def env_for(repo, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("JQUANTS_", "EDINET_"))}
    env["PATH"] = f"{repo / 'bin'}:{env['PATH']}"
    env.update(extra)
    return env


def run(repo, *args, answers=None, **extra):
    """Runs run.sh. With `answers` (list of (prompt text, reply)), it runs on a
    pseudo-terminal and each reply is typed once its prompt appears."""
    cmd = ["bash", str(repo / "run.sh"), *args]
    if answers is None:
        r = subprocess.run(cmd, env=env_for(repo, **extra), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr
    master, slave = os.openpty()
    proc = subprocess.Popen(cmd, env=env_for(repo, **extra), stdin=slave, stdout=slave,
                            stderr=slave, close_fds=True)
    os.close(slave)
    out, pending, deadline = "", list(answers), time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO: the child closed the terminal
                    break
                if not chunk:
                    break
                out += chunk.decode(errors="replace")
            elif proc.poll() is not None:
                break
            if pending and pending[0][0] in out.rsplit("\n", 1)[-1]:
                os.write(master, (pending.pop(0)[1] + "\n").encode())
        else:
            proc.kill()
            raise AssertionError(f"run.sh hung; output so far:\n{out}")
        return proc.wait(timeout=10), out
    finally:
        os.close(master)


def env_lines(repo):
    return (repo / ".env").read_text().splitlines()


ED_PROMPT = "EDINET API key"


def test_non_interactive_setup_saves_both_keys(repo):
    code, out = run(repo, "--sync-only", JQUANTS_API_KEY=JQ_KEY, JQUANTS_PLAN="light",
                    EDINET_API_KEY=ED_KEY)
    assert code == 0, out
    lines = env_lines(repo)
    assert f"JQUANTS_API_KEY={JQ_KEY}" in lines
    assert "JQUANTS_PLAN=light" in lines
    assert f"EDINET_API_KEY={ED_KEY}" in lines
    assert "# EDINET_API_KEY=" not in lines  # the template line is replaced, not duplicated
    assert "Paste" not in out
    assert "jquantster sync" in (repo / "uv.log").read_text()


def test_non_interactive_without_edinet_key_never_prompts(repo):
    code, out = run(repo, "--sync-only", JQUANTS_API_KEY=JQ_KEY)
    assert code == 0, out
    # Neither a key nor a remembered decline: a later terminal run may still ask.
    assert not any(l.startswith(("EDINET_API_KEY=", "EDINET_SKIP=")) for l in env_lines(repo))
    assert ED_PROMPT not in out


def test_existing_setup_takes_edinet_key_from_env(repo):
    run(repo, "--sync-only", JQUANTS_API_KEY=JQ_KEY)
    code, out = run(repo, "--sync-only", EDINET_API_KEY=ED_KEY)
    assert code == 0, out
    assert f"EDINET_API_KEY={ED_KEY}" in env_lines(repo)
    assert f"JQUANTS_API_KEY={JQ_KEY}" in env_lines(repo)


def test_terminal_prompt_saves_edinet_key(repo):
    run(repo, "--sync-only", JQUANTS_API_KEY=JQ_KEY)
    code, out = run(repo, "--sync-only", answers=[(ED_PROMPT, ED_KEY)])
    assert code == 0, out
    assert ED_PROMPT in out and ED_KEY not in out  # hidden input
    assert f"EDINET_API_KEY={ED_KEY}" in env_lines(repo)
    code, out = run(repo, "--sync-only", answers=[])
    assert code == 0 and ED_PROMPT not in out


def test_terminal_skip_is_remembered(repo):
    run(repo, "--sync-only", JQUANTS_API_KEY=JQ_KEY)
    code, out = run(repo, "--sync-only", answers=[(ED_PROMPT, "")])
    assert code == 0, out
    assert ED_PROMPT in out
    assert "EDINET_SKIP=1" in env_lines(repo)
    code, out = run(repo, "--sync-only", answers=[])
    assert code == 0 and ED_PROMPT not in out, out


def test_first_run_in_terminal_asks_for_both(repo):
    code, out = run(repo, "--sync-only", answers=[
        ("J-Quants API key", JQ_KEY), ("Plan", "light"), (ED_PROMPT, ED_KEY)])
    assert code == 0, out
    lines = env_lines(repo)
    assert f"JQUANTS_API_KEY={JQ_KEY}" in lines
    assert "JQUANTS_PLAN=light" in lines
    assert f"EDINET_API_KEY={ED_KEY}" in lines
