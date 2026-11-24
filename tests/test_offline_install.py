import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def test_bundle_cold_install_restart_and_platform_guard(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "checkout"
    deploy = source / "deploy/loopforge"
    deploy.mkdir(parents=True)
    for name in ("vernal.sh", "pyproject.toml", "Dockerfile"):
        shutil.copy2(root / name, source / name)
    (source / "autodev").mkdir()
    (source / "autodev/old.py").write_text("# previous source")
    for file in (root / "deploy/loopforge").iterdir():
        if file.suffix == ".sh" or file.name in ("Dockerfile.sandbox", "compose.yaml"):
            shutil.copy2(file, deploy)
    (deploy / "private-helper.sh").touch()
    (deploy / ".env").write_text("EXAMPLE_PRIVATE_KEY=private\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text(f"#!{sys.executable}\n" + r'''
import hashlib, json, os, shutil, sys, tarfile
from pathlib import Path
a = sys.argv[1:]
base = Path(os.environ["PROBE"])
state = base / "loaded"
with (base / "calls").open("a") as f: print(" ".join(a), file=f)
def image_id(name): return "sha256:" + hashlib.sha256(name.encode()).hexdigest()
if a[0] == "version": print(os.getenv("PLATFORM", "linux/arm64"))
elif a[:2] == ["image","inspect"]:
    if not state.exists(): sys.exit(1)
    if "{{.Os}}" in " ".join(a): print("linux/arm64", image_id(a[-1]))
    elif "{{.Id}}" in a:
        if a[-1] == "autodev-runtime-vernal-base": print((base / "base-tag").read_text())
        else: print(image_id(a[-1]))
    elif "{{json .Config.Cmd}}" in a: print('["vernal","serve","--port","8787"]')
    elif "vernal.sandbox.source" in " ".join(a) and (base / "sandbox-label").exists():
        print((base / "sandbox-label").read_text())
    elif (base / "gateway-labels").exists():
        for key, value in json.loads((base / "gateway-labels").read_text()).items():
            if key in " ".join(a): print(value)
elif a[0] == "build" and "--label" in a:
    (base / "sandbox-label").write_text(a[a.index("--label")+1].split("=",1)[1])
elif a[0] == "create":
    assert "--pull=never" in a
    if "-i" in a:
        assert a[a.index("--network")+1] == "none"
    print("fixture")
elif a[0] == "cp":
    source = base / "previous" / a[1].split("/app/", 1)[1]
    if source.is_dir(): shutil.copytree(source, a[2])
    else: shutil.copyfile(source, a[2])
elif a[0] == "start":
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        current = archive.getnames()
    assert "autodev/new.py" in current and "autodev/old.py" not in current
    assert not any(".env" in file or "__pycache__" in file for file in current)
elif a[0] == "inspect": print("0")
elif a[0] == "tag": (base / "base-tag").write_text(a[1])
elif a[0] == "commit":
    assert 'CMD ["vernal","serve","--port","8787"]' in a
    labels = dict(arg.removeprefix("LABEL ").split("=", 1) for arg in a if arg.startswith("LABEL "))
    (base / "gateway-labels").write_text(json.dumps(labels))
elif "--build" in a and not os.getenv("VERNAL_PYTHON_IMAGE"):
    print("failed to resolve source metadata for docker.io/library/python:3.12-slim: EOF")
    sys.exit(1)
elif "config" in a:
    print("mysql:8.4\nghcr.io/tencent/loopforge:0.4.0\ngitea/gitea:1.22\nautodev-runtime-vernal")
elif a[:2] == ["image","save"]: Path(a[a.index("-o")+1]).write_text("saved")
elif a[:2] == ["image","load"]:
    assert Path(a[a.index("-i")+1]).read_text() == "saved"
    state.touch()
elif "list" in a: print("1 vernal-local")
''')
    docker.chmod(0o755)
    curl = tools / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")
    curl.chmod(0o755)
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "PROBE": str(tmp_path),
           "VERNAL_BIND": "0.0.0.0"}
    (tmp_path / "loaded").touch()

    def launch(directory, *args, check=True, **overrides):
        return subprocess.run([directory / "vernal.sh", *args], cwd=directory,
                              env={**env, **overrides}, check=check, capture_output=True, text=True)

    launch(source, "--pack")
    assert (deploy / ".runner/python-image").read_text().strip() == "public.ecr.aws/docker/library/python:3.12-slim"
    calls = tmp_path / "calls"
    previous = tmp_path / "previous"
    shutil.copytree(source / "autodev", previous / "autodev")
    shutil.copy2(source / "pyproject.toml", previous)
    (previous / "deploy/loopforge").mkdir(parents=True)
    for script in ("start-runner.sh", "wait-and-bind-runner.sh"):
        shutil.copy2(deploy / script, previous / "deploy/loopforge")
    (source / "autodev/old.py").unlink()
    (source / "autodev/new.py").write_text("# updated source")
    calls.write_text("")
    launch(source)
    history = calls.read_text().splitlines()
    assert any(line.startswith("commit ") for line in history)
    assert not any("--build" in line or line.startswith("pull ") for line in history)
    assert any("--no-build --pull never vernal" in line for line in history)
    (source / "autodev/new.py").write_text("# second update")
    calls.write_text("")
    launch(source)
    history = calls.read_text().splitlines()
    assert any(line.startswith("commit ") for line in history)
    assert not any(line.startswith("cp ") or "--build" in line for line in history)
    (source / "pyproject.toml").write_text("# changed dependencies")
    calls.write_text("")
    launch(source)
    assert "--build vernal" in calls.read_text() and "commit " not in calls.read_text()
    target = tmp_path / "install"
    with tarfile.open(source / "dist/vernal-offline-arm64.tar.gz") as bundle:
        entries = bundle.getnames()
        assert "vernal-offline/images.tar" in entries
        assert all(Path(e).name not in {".env", ".data", ".runner", "private-helper.sh"} for e in entries)
        bundle.extractall(target, filter="data")
    target /= "vernal-offline"
    calls.write_text("")
    (tmp_path / "loaded").unlink()
    assert launch(target, check=False, PLATFORM="linux/amd64").returncode != 0
    assert not (tmp_path / "loaded").exists()
    launch(target)
    launch(target)
    history = calls.read_text().splitlines()
    assert sum(line.startswith("image load ") for line in history) == 1
    assert not any(line.startswith(("pull ", "build ")) for line in history)
    starts = [line for line in history if " up -d" in line]
    assert any(line.endswith("vernal") for line in starts)
    assert all("--no-build" in line and "--pull never" in line for line in starts)
