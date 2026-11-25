"""Offline full snapshot. Run inside the quiesced deployment's helper container."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile


def locations(project, volumes):
    return {
        "workspaces": project / "workspaces", "runner": project / "deploy/loopforge/.runner",
        "env": project / "deploy/loopforge/.env", "native-host": Path(os.environ.get("VERNAL_BACKUP_NATIVE_MOUNT") or project / "deploy/loopforge/.data"),
        **{name: volumes / name for name in ("mysql-data", "platform-data", "gitea-data", "gitea-runner-data")},
    }


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def backup(targets, output):
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        base = Path(tmp)
        manifest = {}
        for name, path in targets.items():
            part = base / (name + ".tar")
            with tarfile.open(part, "w") as writer:
                if path.exists():
                    writer.add(path, arcname="data")
            manifest[name] = {"sha256": checksum(part), "present": path.exists()}
        (base / "manifest.json").write_text(json.dumps(manifest))
        archive = base / "backup.gz"
        with tarfile.open(archive, "w:gz") as writer:
            for p in sorted(base.iterdir()):
                if p != archive:
                    writer.add(p, arcname=p.name)
        archive.chmod(0o600)
        os.replace(archive, output)


def checkpoint(path, state):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as file:
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(file.name, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def recover(targets, journal):
    if not journal.exists():
        return
    state = json.loads(journal.read_text())
    for name, part in reversed(list(state["parts"].items())):
        dest = targets[name]
        directory = dest if part["volume"] else dest.parent
        old = directory / part["old"]
        if not old.exists():
            continue
        if not state["committed"]:
            if part["phase"] == "installing":
                for entry in part["install"]:
                    remove(directory / entry)
                part["phase"] = "rolling_back"
                checkpoint(journal, state)
            for original in old.iterdir():
                original.rename(directory / original.name)
        shutil.rmtree(old)
    journal.unlink()


def restore(targets, archive):
    journal = targets["env"].parent / ".restore-transaction.json"
    recover(targets, journal)
    # Validate and stage every component before the first destination changes.
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        with tarfile.open(archive, "r:gz") as reader:
            members = reader.getmembers()
            expected = {"manifest.json", *(name + ".tar" for name in targets)}
            if {m.name for m in members} != expected or len(members) != len(expected) or any(not m.isfile() for m in members):
                raise ValueError("无效的备份结构")
            reader.extractall(base, filter="data")
        manifest = json.loads((base / "manifest.json").read_text())
        staged = {}
        for name in targets:
            part = base / (name + ".tar")
            if checksum(part) != manifest[name]["sha256"]:
                raise ValueError("备份校验失败：" + name)
            stage = base / name
            stage.mkdir()
            with tarfile.open(part) as reader:
                def safe_member(info, destination):
                    clean = tarfile.data_filter(info, destination)
                    if clean.name != "data" and not clean.name.startswith("data/"):
                        raise ValueError("无效的备份成员")
                    return clean.replace(uid=info.uid, gid=info.gid, mode=info.mode & 0o777)
                reader.extractall(stage, filter=safe_member, numeric_owner=True)
            staged[name] = stage / "data"
            if staged[name].exists() != manifest[name]["present"]:
                raise ValueError("备份清单不一致：" + name)
        state = {"committed": False, "parts": {}}
        try:
            for name, dest in targets.items():
                volume = name not in ("workspaces","runner","env","native-host") or (name == "native-host" and bool(os.getenv("VERNAL_BACKUP_NATIVE_MOUNT")))
                source = staged[name]
                if volume and not dest.exists():
                    if not source.exists():
                        continue
                    raise ValueError("缺少目标 Docker 卷挂载：" + name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                directory = dest if volume else dest.parent
                old = Path(tempfile.mkdtemp(prefix=".vernal-rollback-", dir=directory))
                pairs = [(item,dest/item.name) for item in source.iterdir()] if (volume and source.exists()) else ([] if volume else [(source,dest)])
                part = {"volume":volume, "old":old.name, "install":[target.name for _,target in pairs], "phase":"moving"}
                state["parts"][name] = part
                checkpoint(journal,state)
                entries = list(dest.iterdir()) if volume else [dest]
                for entry in entries:
                    if entry != old and (entry.exists() or entry.is_symlink()):
                        entry.rename(old / entry.name)
                part["phase"] = "installing"
                checkpoint(journal,state)
                for item, target in pairs:
                    if item.exists() or item.is_symlink():
                        if item.is_dir() and not item.is_symlink():
                            shutil.copytree(item, target, symlinks=True)
                        else:
                            shutil.copy2(item, target, follow_symlinks=False)
                        if os.geteuid() == 0:
                            descendants = [item, *(item.rglob("*") if item.is_dir() and not item.is_symlink() else ())]
                            for original in descendants:
                                st = original.lstat()
                                os.chown(target / original.relative_to(item), st.st_uid, st.st_gid, follow_symlinks=False)
            state["committed"] = True
            checkpoint(journal,state)
        finally:
            recover(targets,journal)


if __name__ == "__main__":
    os.umask(0o077)
    operation = {"--backup": backup, "--restore": restore}[sys.argv[1]]
    operation(locations(Path("/project"), Path("/volumes")), Path(sys.argv[2]))
