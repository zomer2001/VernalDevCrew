import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def test_bundle_cold_install_restart_and_platform_guard(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source with spaces/checkout"
    deploy = source / "deploy/loopforge"
    deploy.mkdir(parents=True)
    for name in ("vernal.sh", "pyproject.toml", "Dockerfile"):
        shutil.copy2(root / name, source / name)
    (source / "autodev/assets").mkdir(parents=True)
    shutil.copy2(root / "autodev/__init__.py", source / "autodev/__init__.py")
    shutil.copytree(
        root / "autodev/quality",
        source / "autodev/quality",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(root / "autodev/assets/semgrep", source / "autodev/assets/semgrep")
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
    sys.stdin.buffer.read()
    (base / "sandbox-label").write_text(a[a.index("--label")+1].split("=",1)[1])
elif a[0] == "run":
    if os.getenv("FAIL_SANDBOX_PROBE") == "1":
        print("sandbox probe failed", file=sys.stderr)
        sys.exit(17)
    assert "--pull=never" in a
    assert "--network" in a and a[a.index("--network") + 1] == "none"
    assert "--entrypoint" not in a and a[-3:-1] == ["sh", "-ec"]
    assert "dsh --version" in a[-1] and "opencode --version" in a[-1] and "vernal-quality --help" in a[-1]
    print("dsh 0.2.2\nopencode 0.1.0")
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

    pack_res = launch(source, "--pack")
    assert "公用沙箱构建失败" not in pack_res.stderr
    assert (deploy / ".runner/pool/image-ready").is_file()
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
    target = tmp_path / "offline dir with spaces"
    with tarfile.open(source / "dist/vernal-offline-arm64.tar.gz") as bundle:
        entries = bundle.getnames()
        assert "vernal-offline/images.tar" in entries
        assert all(Path(e).name not in {".env", ".data", ".runner", "private-helper.sh"} for e in entries)
        bundle.extractall(target, filter="data")
    target /= "vernal-offline"
    assert not (target / "autodev").exists()
    calls.write_text("")
    (tmp_path / "loaded").unlink()
    assert launch(target, check=False, PLATFORM="linux/amd64").returncode != 0
    assert not (tmp_path / "loaded").exists()
    offline_first = launch(target)
    offline_second = launch(target)
    assert "公用沙箱构建失败" not in offline_first.stderr
    assert "公用沙箱构建失败" not in offline_second.stderr
    history = calls.read_text().splitlines()
    assert sum(line.startswith("image load ") for line in history) == 1
    assert not any(line.startswith(("pull ", "build ")) for line in history)
    probe_runs = [
        line for line in history
        if line.startswith("run ") and "dsh --version" in line and "opencode --version" in line and "vernal-quality --help" in line
    ]
    assert len(probe_runs) >= 2
    image_ready = target / "deploy/loopforge/.runner/pool/image-ready"
    assert image_ready.is_file() and "dsh 0.2.2" in image_ready.read_text()
    starts = [line for line in history if " up -d" in line]
    assert any(line.endswith("vernal") for line in starts)
    assert all("--no-build" in line and "--pull never" in line for line in starts)

    # 无 autodev 源码且目录名含空格时，单独执行 VERNAL_OFFLINE=1 build-sandbox.sh 必须探测成功并落地 image-ready；真实探测失败则原样报错
    image_ready.unlink()
    calls.write_text("")
    standalone_ok = subprocess.run(
        [target / "deploy/loopforge/build-sandbox.sh"],
        cwd=target,
        env={**env, "VERNAL_OFFLINE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert standalone_ok.returncode == 0, standalone_ok.stderr
    assert any(line.startswith("run ") and "vernal-quality --help" in line for line in calls.read_text().splitlines())
    assert image_ready.is_file() and image_ready.read_text().strip()

    standalone_fail = subprocess.run(
        [target / "deploy/loopforge/build-sandbox.sh"],
        cwd=target,
        env={**env, "VERNAL_OFFLINE": "1", "FAIL_SANDBOX_PROBE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert standalone_fail.returncode == 17
    assert not image_ready.exists()

    # 源码目录缺失 autodev/quality（quality_fp 为空）且路径含空格时，非离线构建与探测同样不得触发 pipefail 误退出
    bare_src = tmp_path / "bare dir with spaces/checkout"
    (bare_src / "deploy/loopforge").mkdir(parents=True)
    shutil.copy2(root / "deploy/loopforge/build-sandbox.sh", bare_src / "deploy/loopforge/build-sandbox.sh")
    shutil.copy2(root / "deploy/loopforge/Dockerfile.sandbox", bare_src / "deploy/loopforge/Dockerfile.sandbox")
    (bare_src / "deploy/loopforge/build-sandbox.sh").chmod(0o755)
    bare_ok = subprocess.run(
        [bare_src / "deploy/loopforge/build-sandbox.sh"],
        cwd=bare_src,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert bare_ok.returncode == 0, bare_ok.stderr
    assert (bare_src / "deploy/loopforge/.runner/pool/image-ready").is_file()
