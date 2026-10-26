"""Build plans and verified APK-only nightly snapshots. No source code is executed here."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from urllib.parse import quote
import zipfile

REPOSITORY = "LiJiaHua1024/Bilibili_Innocent_Builds"
SOURCE = "LiJiaHua1024/Bilibili_Innocent_Lab"
CERTIFICATE = "598356b3e03f67bba9087c9704ea26fae033b231d4d6a9a2d63706ecfa0254e3"
PACKAGE = "com.Bilibili_Innocent_Lab.xposedmodule"
TAG = re.compile(r"nightly-(\d{8})(?:\.([1-9]\d*))?\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
ASSET = re.compile(r"BIL-(?:EXTREMELY-UNSTABLE-)?(.+)-([0-9a-f]{12})\.apk\Z")
STATE = re.compile(r"<!-- bib-nightly-state-v1\n(.*?)\n-->", re.S)
MAX_APK = 1024 * 1024 * 1024
WARNING = (
    "**极度不稳定的实验构建，比 Canary 更不稳定。**\n\n"
    "仅供急于尝鲜，或验证某个特定 Hotfix。不建议日常使用，可能存在崩溃、无法启动、Hook 失效、功能不完整及各种不适配。"
    "CI 通过不代表完成实机回归，更不代表目标问题已经修复。\n"
)


class ReleaseError(RuntimeError):
    pass


class APIError(ReleaseError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def checked(command: list[str], *, timeout: int = 120) -> str:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseError(f"Command failed: {command[0]}: {error}") from error
    if result.returncode:
        raise ReleaseError(result.stderr.decode("utf-8", "replace")[-2000:])
    return result.stdout.decode("utf-8", "replace")


class GitHub:
    def request(self, method: str, path: str, payload: dict | None = None):
        arguments = ["gh", "api", "--method", method, path]
        data = None
        if payload is not None:
            arguments += ["--input", "-"]
            data = json.dumps(payload, ensure_ascii=False).encode()
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = subprocess.run(arguments, input=data, capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as error:
                failure = APIError(str(error))
            else:
                if response.returncode == 0:
                    return json.loads(response.stdout) if response.stdout.strip() else None
                detail = response.stderr.decode("utf-8", "replace")
                match = re.search(r"HTTP (\d{3})", detail)
                failure = APIError(f"GitHub {method} {path}: {detail[-1500:]}", int(match[1]) if match else 0)
                if failure.status and failure.status < 500 and failure.status not in (429,):
                    raise failure
            if method == "GET" and attempt < 2:
                time.sleep(2 ** attempt)
        raise failure

    def items(self, path: str, field: str | None = None) -> list[dict]:
        result = []
        for page in range(1, 21):
            separator = "&" if "?" in path else "?"
            data = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            rows = data[field] if field else data
            if not isinstance(rows, list):
                raise ReleaseError(f"Expected a list from {path}")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise ReleaseError(f"Pagination budget exceeded: {path}")

    def download(self, path: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        command = ["gh", "api", path]
        # Release assets require binary negotiation; Actions ZIP endpoints reject it with HTTP 415.
        if "/releases/assets/" in path:
            command += ["-H", "Accept: application/octet-stream"]
        failure = ""
        for attempt in range(3):
            try:
                with temporary.open("wb") as stream:
                    result = subprocess.run(
                        command,
                        stdout=stream, stderr=subprocess.PIPE, timeout=300,
                    )
                if result.returncode == 0:
                    temporary.replace(target)
                    return
                failure = result.stderr.decode("utf-8", "replace")[-1000:]
                status = re.search(r"HTTP (\d{3})", failure)
                if status and 400 <= int(status[1]) < 500 and int(status[1]) != 429:
                    temporary.unlink(missing_ok=True)
                    raise ReleaseError(f"Download failed: {path}: {failure}")
            except (OSError, subprocess.TimeoutExpired) as error:
                failure = str(error)
            temporary.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise ReleaseError(f"Download failed: {path}: {failure}")

    def upload(self, tag: str, path: Path) -> None:
        checked(["gh", "release", "upload", tag, str(path), "--repo", REPOSITORY], timeout=600)


def validate_branch(branch: str) -> str:
    if not isinstance(branch, str) or not branch or len(branch) > 200 or branch == "main":
        raise ReleaseError(f"Invalid development branch: {branch!r}")
    result = subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"], capture_output=True)
    if result.returncode:
        raise ReleaseError(f"Invalid branch name: {branch!r}")
    return branch


def slug(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", branch)[:80]


def apk_name(branch: str, sha: str) -> str:
    if not SHA.fullmatch(sha):
        raise ReleaseError("Invalid source SHA")
    return f"BIL-{slug(validate_branch(branch))}-{sha[:12]}.apk"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest_value(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or not HASH.fullmatch(value[7:]):
        raise ReleaseError("Missing or invalid GitHub SHA-256 digest")
    return value[7:]


def file_matches(asset: dict, path: Path) -> bool:
    return (asset.get("state") == "uploaded" and asset.get("size") == path.stat().st_size
            and digest_value(asset.get("digest")) == digest(path))


def parse_tag(value: str) -> re.Match:
    match = TAG.fullmatch(value)
    if not match:
        raise ReleaseError(f"Invalid nightly tag: {value}")
    try:
        datetime.strptime(match[1], "%Y%m%d")
    except ValueError as error:
        raise ReleaseError("Invalid nightly date") from error
    return match


def next_tag(existing: list[str], now: datetime) -> tuple[str, str]:
    date = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    numbers = [int(match[2] or 1) for name in existing
               if (match := TAG.fullmatch(name)) and match[1] == date]
    number = max(numbers, default=0) + 1
    suffix = f".{number}" if number > 1 else ""
    return f"nightly-{date}{suffix}", f"Nightly Build {date}{suffix}"


def state_from(body: str) -> dict | None:
    matches = STATE.findall(body or "")
    if not matches:
        return None
    if len(matches) != 1 or len(matches[0]) > 200_000:
        raise ReleaseError("Invalid or duplicate nightly release state")
    try:
        state = json.loads(matches[0])
    except (TypeError, ValueError) as error:
        raise ReleaseError("Invalid nightly release state JSON") from error
    if not isinstance(state, dict) or state.get("schema") != 1 or state.get("source_repository") != SOURCE:
        raise ReleaseError("Unsupported nightly state or source repository")
    return state


def state_comment(state: dict) -> str:
    data = json.dumps(state, ensure_ascii=False, sort_keys=True)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"<!-- bib-nightly-state-v1\n{data}\n-->"


def published_nightly(releases: list[dict]) -> dict | None:
    candidates = [r for r in releases if not r.get("draft") and r.get("prerelease")
                  and r.get("published_at") and TAG.fullmatch(r.get("tag_name", ""))]
    return max(candidates, key=lambda r: (r["published_at"], r["id"]), default=None)


def fingerprint(release: dict | None) -> str | None:
    if release is None:
        return None
    identity = {key: release.get(key) for key in ("id", "tag_name", "body")}
    identity["assets"] = sorted(
        [{key: asset.get(key) for key in ("id", "name", "size", "digest")} for asset in release.get("assets", [])],
        key=lambda a: a["name"],
    )
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


@dataclass
class Entry:
    branch: str
    source_sha: str
    asset_name: str
    sha256: str
    size: int
    run_url: str | None = None
    asset_id: int | None = None

    def validate(self) -> Entry:
        validate_branch(self.branch)
        if not SHA.fullmatch(self.source_sha) or not HASH.fullmatch(self.sha256):
            raise ReleaseError("Invalid APK source or digest")
        expected = apk_name(self.branch, self.source_sha)
        if self.asset_name not in (expected, expected.replace("BIL-", "BIL-EXTREMELY-UNSTABLE-", 1)):
            raise ReleaseError("APK filename does not match its source")
        if type(self.size) is not int or not 0 < self.size <= MAX_APK:
            raise ReleaseError("APK size is outside the allowed range")
        if self.run_url is not None and not re.fullmatch(
            rf"https://github\.com/{re.escape(REPOSITORY)}/actions/runs/[1-9]\d*", self.run_url
        ):
            raise ReleaseError("Invalid build run URL")
        return self

    def serial(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != "asset_id"}

    @classmethod
    def read(cls, data: dict) -> Entry:
        try:
            return cls(**data).validate()
        except (TypeError, KeyError) as error:
            raise ReleaseError("Malformed APK entry") from error


def resolve_base(api: GitHub, release: dict | None, branches: list[str]) -> list[Entry]:
    if release is None:
        return []
    assets = release.get("assets", [])
    if not assets or any(not ASSET.fullmatch(a.get("name", "")) for a in assets):
        raise ReleaseError("The previous Nightly must contain APK assets only")
    if len({a["name"] for a in assets}) != len(assets):
        raise ReleaseError("Duplicate release asset name")
    state = state_from(release.get("body", ""))
    entries = []
    if state is not None:
        data = state.get("entries")
        if not isinstance(data, list) or len(data) != len(assets):
            raise ReleaseError("Nightly state does not cover every APK")
        entries = [Entry.read(item) for item in data]
    else:
        # Bootstrap the manually published first Nightly without adding a metadata attachment.
        rows = {}
        for line in release.get("body", "").splitlines():
            match = re.match(r"\|\s*`([^`]+)`\s*\|", line)
            if match:
                rows[validate_branch(match[1])] = line
        known = set(branches) | set(rows)
        for asset in assets:
            match = ASSET.fullmatch(asset["name"])
            names = [name for name in known if slug(name) == match[1]]
            if len(names) != 1:
                raise ReleaseError(f"Cannot uniquely identify the branch for {asset['name']}")
            branch = names[0]
            commit = api.request("GET", f"repos/{SOURCE}/commits/{match[2]}")
            sha = commit.get("sha", "")
            if not sha.startswith(match[2]):
                raise ReleaseError("Legacy asset source SHA mismatch")
            run = re.search(rf"https://github\.com/{re.escape(REPOSITORY)}/actions/runs/[1-9]\d*", rows.get(branch, ""))
            entries.append(Entry(branch, sha, asset["name"], digest_value(asset.get("digest")),
                                 asset["size"], run[0] if run else None).validate())
    index = {asset["name"]: asset for asset in assets}
    if len({entry.branch for entry in entries}) != len(entries):
        raise ReleaseError("Previous Nightly has duplicate branch entries")
    for entry in entries:
        asset = index.get(entry.asset_name)
        if (asset is None or asset.get("state") != "uploaded" or asset["size"] != entry.size
                or digest_value(asset.get("digest")) != entry.sha256):
            raise ReleaseError("Previous Nightly APK metadata mismatch")
        entry.asset_id = asset["id"]
    return entries


def change_details(api: GitHub, before: str | None, after: str) -> dict:
    if before:
        url = f"https://github.com/{SOURCE}/compare/{before}...{after}"
        try:
            data = api.request("GET", f"repos/{SOURCE}/compare/{before}...{after}?per_page=100")
            commits = data.get("commits", [])
            return {"status": data.get("status", "unknown"), "url": url,
                    "subjects": [item["commit"]["message"].splitlines()[0] for item in commits[:20]],
                    "total": data.get("total_commits", len(commits))}
        except APIError:
            return {"status": "unavailable", "url": url, "subjects": [], "total": 0}
    commit = api.request("GET", f"repos/{SOURCE}/commits/{after}")
    return {"status": "new", "url": f"https://github.com/{SOURCE}/commit/{after}",
            "subjects": [commit["commit"]["message"].splitlines()[0]], "total": 1}


def create_plan(api: GitHub, selection: str) -> dict:
    rows = api.items(f"repos/{SOURCE}/branches")
    heads = {row["name"]: row["commit"]["sha"] for row in rows if row["name"] != "main"}
    requested = list(dict.fromkeys(part.strip() for part in re.split(r"[,\n]", selection) if part.strip()))
    if requested == ["all"]:
        requested = sorted(heads)
    if not requested or len(requested) > 32:
        raise ReleaseError("Select between 1 and 32 development branches")
    for branch in requested:
        validate_branch(branch)
        if branch not in heads or not SHA.fullmatch(heads[branch]):
            raise ReleaseError(f"Source branch not found or invalid: {branch}")
    releases = api.items(f"repos/{REPOSITORY}/releases")
    base = published_nightly(releases)
    base_entries = resolve_base(api, base, list(heads))
    previous = {entry.branch: entry for entry in base_entries}
    updates, unchanged = [], []
    for branch in requested:
        old = previous.get(branch)
        if old and old.source_sha == heads[branch]:
            unchanged.append(branch)
            continue
        updates.append({"branch": branch, "sha": heads[branch], "before": old.source_sha if old else None,
                        "changes": change_details(api, old.source_sha if old else None, heads[branch])})
    all_branches = set(previous) | set(requested)
    if len({slug(branch) for branch in all_branches}) != len(all_branches):
        raise ReleaseError("Branch filenames would collide after normalization")
    return {"schema": 1, "source_repository": SOURCE, "repository": REPOSITORY,
            "base_id": base["id"] if base else None, "base_tag": base["tag_name"] if base else None,
            "base_fingerprint": fingerprint(base), "base_entries": [asdict(entry) for entry in base_entries],
            "updates": updates, "unchanged_requested": unchanged}


def validate_plan(plan: dict) -> None:
    if (plan.get("schema") != 1 or plan.get("repository") != REPOSITORY
            or plan.get("source_repository") != SOURCE):
        raise ReleaseError("Invalid release plan identity")
    old = [Entry.read(data) for data in plan["base_entries"]]
    if len({entry.branch for entry in old}) != len(old):
        raise ReleaseError("Duplicate base branch in release plan")
    updates = plan.get("updates")
    if not isinstance(updates, list) or len(updates) > 32:
        raise ReleaseError("Invalid update selection")
    if len({item["branch"] for item in updates}) != len(updates):
        raise ReleaseError("Duplicate update branch")
    before = {entry.branch: entry.source_sha for entry in old}
    for item in updates:
        validate_branch(item["branch"])
        if not SHA.fullmatch(item["sha"]) or item.get("before") != before.get(item["branch"]):
            raise ReleaseError("Invalid frozen source transition")
        if item["sha"] == item.get("before"):
            raise ReleaseError("Unchanged source must not be marked as updated")


def verify_apk(path: Path, apksigner: str, aapt: str) -> None:
    output = checked([apksigner, "verify", "--verbose", "--print-certs", str(path)])
    certificates = {value.lower() for value in re.findall(r"certificate SHA-256 digest: ([0-9a-fA-F]{64})", output)}
    if certificates != {CERTIFICATE} or not re.search(r"^Number of signers: 1\s*$", output, re.M):
        raise ReleaseError("APK is not signed solely by the fixed release certificate")
    badging = checked([aapt, "dump", "badging", str(path)])
    if not re.search(rf"^package: name='{re.escape(PACKAGE)}'", badging, re.M) or re.search(r"^application-debuggable", badging, re.M):
        raise ReleaseError("Unexpected or debuggable APK package")


def read_new_artifact(archive: Path, update: dict, run_id: int, workflow_sha: str, output: Path) -> Entry:
    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ReleaseError("Duplicate artifact ZIP member")
        for info in infos:
            if (info.filename.startswith(("/", "\\")) or ".." in info.filename.replace("\\", "/").split("/")
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise ReleaseError("Unsafe artifact ZIP member")
        required = {"app-release.apk", "build-info.txt", "SHA256SUMS"}
        if not required.issubset(names):
            raise ReleaseError("Incomplete signed artifact")
        if any(package.getinfo(name).file_size > limit for name, limit in
               (("build-info.txt", 32768), ("SHA256SUMS", 4096), ("app-release.apk", MAX_APK))):
            raise ReleaseError("Oversized signed artifact member")
        receipt = {}
        for line in package.read("build-info.txt").decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in receipt:
                raise ReleaseError("Malformed build receipt")
            receipt[key] = value
        expected = {"source_repository": SOURCE, "workflow_repository": REPOSITORY,
                    "branch": update["branch"], "source_sha": update["sha"], "workflow_sha": workflow_sha,
                    "certificate_sha256": CERTIFICATE,
                    "run_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"}
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ReleaseError("Signed artifact does not match the frozen source or workflow")
        checksum = package.read("SHA256SUMS").decode("utf-8").strip()
        match = re.fullmatch(r"([0-9a-f]{64})\s+app-release\.apk", checksum)
        if not match:
            raise ReleaseError("Invalid artifact APK checksum")
        output.parent.mkdir(parents=True, exist_ok=True)
        with package.open("app-release.apk") as source, output.open("wb") as target:
            shutil.copyfileobj(source, target)
    if digest(output) != match[1]:
        raise ReleaseError("Artifact APK checksum mismatch")
    return Entry(update["branch"], update["sha"], output.name, match[1], output.stat().st_size,
                 expected["run_url"]).validate()


def collect_snapshot(api: GitHub, plan: dict, directory: Path, run_id: int, attempt: int,
                     workflow_sha: str, verifier) -> tuple[list[Entry], dict[str, Path]]:
    validate_plan(plan)
    entries = {}
    for data in plan["base_entries"]:
        entry = Entry.read(data)
        entries[entry.branch] = entry
    paths = {}
    artifacts = api.items(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts", "artifacts")
    for update in plan["updates"]:
        pattern = re.compile(rf"UNSTABLE-EXPERIMENTAL-{re.escape(slug(update['branch']))}-{update['sha'][:12]}-{run_id}-(\d+)\Z")
        eligible = [(int(match[1]), a) for a in artifacts
                    if (match := pattern.fullmatch(a.get("name", ""))) and 0 < int(match[1]) <= attempt and not a.get("expired")]
        latest_attempt = max((number for number, _ in eligible), default=0)
        # Rerunning only a failed publishing job retains the earlier successful build artifacts.
        matches = [a for number, a in eligible if number == latest_attempt]
        if len(matches) != 1:
            raise ReleaseError(f"Missing or duplicate verified artifact: {update['branch']}")
        artifact = matches[0]
        archive = directory / "downloads" / f"artifact-{artifact['id']}.zip"
        api.download(f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip", archive)
        if digest(archive) != digest_value(artifact.get("digest")):
            raise ReleaseError("Artifact ZIP digest mismatch")
        output = directory / "assets" / apk_name(update["branch"], update["sha"])
        entry = read_new_artifact(archive, update, run_id, workflow_sha, output)
        verifier(output)
        entries[entry.branch] = entry
        paths[entry.branch] = output
    for branch, entry in entries.items():
        if branch in paths:
            continue
        if type(entry.asset_id) is not int or entry.asset_id <= 0:
            raise ReleaseError("Missing carried asset ID")
        output = directory / "assets" / apk_name(branch, entry.source_sha)
        api.download(f"repos/{REPOSITORY}/releases/assets/{entry.asset_id}", output)
        if output.stat().st_size != entry.size or digest(output) != entry.sha256:
            raise ReleaseError(f"Carried APK digest mismatch: {branch}")
        verifier(output)
        entry.asset_name = output.name
        paths[branch] = output
    if not entries or len({entry.asset_name for entry in entries.values()}) != len(entries):
        raise ReleaseError("Snapshot is empty or APK names collide")
    return sorted(entries.values(), key=lambda e: e.branch), paths


def display(value: str) -> str:
    return html.escape(value.replace("\n", " ").replace("\r", " "), quote=False).replace("|", "\\|")


def release_notes(plan: dict, entries: list[Entry], tag: str, run_id: int, workflow_sha: str) -> str:
    parse_tag(tag)
    updated = {item["branch"]: item for item in plan["updates"]}
    lines = [WARNING, "## 本次更新的分支", ""]
    for branch, update in updated.items():
        label = "更新" if update["before"] else "首次加入"
        transition = f"{update['before'][:12]} → {update['sha'][:12]}" if update["before"] else update["sha"][:12]
        lines.append(f"- **{label}** <code>{display(branch)}</code>：{transition}")
        changes = update["changes"]
        if changes["status"] in ("diverged", "behind"):
            lines.append("  - 分支历史存在分叉或回退，请查看源码对比；不能按线性增量理解。")
        if changes["status"] == "unavailable":
            lines.append("  - 提交对比暂不可用，本次仅确认源码版本发生变化。")
        for subject in changes["subjects"]:
            lines.append(f"  - {display(subject)}")
        if changes["total"] > len(changes["subjects"]):
            lines.append(f"  - 另有 {changes['total'] - len(changes['subjects'])} 条提交，见完整对比。")
        lines.append(f"  - [源码记录]({changes['url']})")
    lines += ["", "## 沿用上次产物的分支", ""]
    carried = [entry for entry in entries if entry.branch not in updated]
    if carried:
        lines.append(f"以下包沿用 [{plan['base_tag']}](https://github.com/{REPOSITORY}/releases/tag/{plan['base_tag']})，本次未重新构建：")
        lines += [f"- <code>{display(entry.branch)}</code>：{entry.source_sha[:12]}" for entry in carried]
    else:
        lines.append("无。本次所有附件均为新构建产物。")
    lines += ["", "## 下载", "", "这些 APK 来自独立分支，不是功能合集。请选择需要测试的分支；同包名、同签名的包会互相覆盖。", "",
              "| 分支 | 本次处理 | Release APK | 源码提交 | 构建来源 |", "| --- | --- | --- | --- | --- |"]
    for entry in entries:
        kind = "更新" if entry.branch in updated and updated[entry.branch]["before"] else "首次加入" if entry.branch in updated else "沿用"
        download = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{entry.asset_name}"
        commit = f"https://github.com/{SOURCE}/commit/{entry.source_sha}"
        origin = f"[CI]({entry.run_url})" if entry.run_url else f"[上一版](https://github.com/{REPOSITORY}/releases/tag/{plan['base_tag']})"
        lines.append(f"| <code>{display(entry.branch)}</code> | {kind} | [APK]({download}) | [{entry.source_sha[:12]}]({commit}) | {origin} |")
    lines += ["", "本次更新包通过 Release/R8、JVM 单测、完整 Lint 和固定签名检查；沿用包重新校验了哈希、签名与包属性，没有重新构建。未执行本轮实机回归，提交记录中的修复仍需实际验证。",
              "", "附件仅包含 Release APK。不同分支保留各自版本码，低版本码可能无法覆盖安装；本证书与上游不同，切回上游正式版不保证能直接覆盖，请先考虑配置备份和迁移。",
              "", "反馈请附分支、源码提交、宿主版本、设备/系统、注入框架与复现步骤，并说明原问题是否仍可复现、是否出现回归。成熟功能将争取合并上游，由上游正式发布。",
              "", "GitHub 自动生成的 Source code 压缩包属于 BIB 构建配置，模块源码请查看上表。", ""]
    state = {"schema": 1, "source_repository": SOURCE, "run_id": run_id, "workflow_sha": workflow_sha,
             "base_tag": plan["base_tag"], "updated_branches": list(updated), "entries": [entry.serial() for entry in entries]}
    return "\n".join(lines) + state_comment(state) + "\n"


def assert_base_current(plan: dict, releases: list[dict]) -> None:
    if fingerprint(published_nightly(releases)) != plan["base_fingerprint"]:
        raise ReleaseError("The previous Nightly changed during this build; dispatch a fresh release plan")


def assert_assets(release: dict, entries: list[Entry], paths: dict[str, Path]) -> None:
    assets = release.get("assets", [])
    if len(assets) != len(entries) or len({asset["name"] for asset in assets}) != len(assets):
        raise ReleaseError("Release attachment count mismatch")
    index = {asset["name"]: asset for asset in assets}
    for entry in entries:
        if entry.asset_name not in index or not file_matches(index[entry.asset_name], paths[entry.branch]):
            raise ReleaseError(f"Release APK mismatch: {entry.branch}")


def assert_tag(api: GitHub, tag: str, workflow_sha: str) -> None:
    ref = api.request("GET", f"repos/{REPOSITORY}/git/ref/tags/{tag}")
    target = ref.get("object", {})
    for _ in range(4):
        if target.get("type") == "commit" and target.get("sha") == workflow_sha:
            return
        if target.get("type") != "tag" or not SHA.fullmatch(target.get("sha", "")):
            break
        target = api.request("GET", f"repos/{REPOSITORY}/git/tags/{target['sha']}").get("object", {})
    raise ReleaseError("Nightly tag does not point to the expected workflow commit")


def publish_snapshot(api: GitHub, plan: dict, entries: list[Entry], paths: dict[str, Path], run_id: int,
                     workflow_sha: str, directory: Path, now: datetime, dry_run: bool = False) -> dict:
    validate_plan(plan)
    expected_sources = {data["branch"]: data["source_sha"] for data in plan["base_entries"]}
    expected_sources.update({item["branch"]: item["sha"] for item in plan["updates"]})
    if len({entry.branch for entry in entries}) != len(entries) or {entry.branch: entry.source_sha for entry in entries} != expected_sources:
        raise ReleaseError("Snapshot is missing an updated or carried branch")
    for entry in entries:
        entry.validate()
        path = paths[entry.branch]
        if path.name != entry.asset_name or path.stat().st_size != entry.size or digest(path) != entry.sha256:
            raise ReleaseError("Only the verified Release APKs can be published")
    history = api.items(f"repos/{REPOSITORY}/releases")
    owned = [release for release in history if (state := state_from(release.get("body", ""))) and state.get("run_id") == run_id]
    if len(owned) > 1:
        raise ReleaseError("More than one release belongs to this workflow run")
    candidate = owned[0] if owned else None
    if candidate:
        state = state_from(candidate["body"])
        expected = {entry.branch: entry.source_sha for entry in entries}
        actual = {item["branch"]: item["source_sha"] for item in state["entries"]}
        if expected != actual or state.get("workflow_sha") != workflow_sha:
            raise ReleaseError("This run already owns a different snapshot; dispatch a new run")
        if not candidate.get("draft"):
            if not candidate.get("prerelease"):
                raise ReleaseError("Existing snapshot is not a prerelease")
            assert_assets(candidate, entries, paths)
            return {"url": candidate["html_url"], "tag": candidate["tag_name"], "reused_run": True}
    assert_base_current(plan, history)
    if candidate:
        tag = candidate["tag_name"]
        match = parse_tag(tag)
        title = f"Nightly Build {match[1]}" + (f".{match[2]}" if match[2] else "")
    else:
        tags = api.items(f"repos/{REPOSITORY}/tags")
        existing = [release["tag_name"] for release in history] + [tag["name"] for tag in tags]
        tag, title = next_tag(existing, now)
    notes = release_notes(plan, entries, tag, run_id, workflow_sha)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "release-notes.md").write_text(notes, encoding="utf-8")
    if dry_run:
        return {"tag": tag, "title": title, "updated": [item["branch"] for item in plan["updates"]],
                "carried": [entry.branch for entry in entries if entry.branch not in {item["branch"] for item in plan["updates"]}],
                "apk_files": [str(paths[entry.branch]) for entry in entries], "dry_run": True}
    if candidate is None:
        try:
            api.request("POST", f"repos/{REPOSITORY}/git/refs", {"ref": f"refs/tags/{tag}", "sha": workflow_sha})
        except APIError:
            assert_tag(api, tag, workflow_sha)
        payload = {"tag_name": tag, "target_commitish": workflow_sha, "name": title,
                   "body": notes, "draft": True, "prerelease": True, "make_latest": "false"}
        try:
            candidate = api.request("POST", f"repos/{REPOSITORY}/releases", payload)
        except APIError:
            recovered = [r for r in api.items(f"repos/{REPOSITORY}/releases")
                         if r.get("draft") and (state := state_from(r.get("body", ""))) and state.get("run_id") == run_id]
            if len(recovered) != 1:
                raise ReleaseError("Draft creation outcome is unknown; no duplicate draft was created")
            candidate = recovered[0]
    release_path = f"repos/{REPOSITORY}/releases/{candidate['id']}"
    if (candidate.get("tag_name") != tag or candidate.get("name") != title or not candidate.get("draft")
            or not candidate.get("prerelease") or candidate.get("body") != notes):
        raise ReleaseError("Draft settings or source notes differ from the expected snapshot")
    assert_tag(api, tag, workflow_sha)
    for entry in entries:
        for attempt in range(3):
            current = api.request("GET", release_path)
            if not current.get("draft"):
                raise ReleaseError("Draft was published or modified by another actor")
            matches = [asset for asset in current.get("assets", []) if asset["name"] == entry.asset_name]
            if matches:
                if len(matches) == 1 and file_matches(matches[0], paths[entry.branch]):
                    break
                raise ReleaseError("An incomplete or different draft attachment needs manual inspection")
            try:
                api.upload(tag, paths[entry.branch])
            except ReleaseError:
                pass  # The response can be lost after GitHub stored the complete file.
            current = api.request("GET", release_path)
            matches = [asset for asset in current.get("assets", []) if asset["name"] == entry.asset_name]
            if len(matches) == 1 and file_matches(matches[0], paths[entry.branch]):
                break
            if matches:
                raise ReleaseError("An incomplete draft upload needs manual inspection")
        else:
            raise ReleaseError("Upload retry budget exhausted")
    current = api.request("GET", release_path)
    assert_assets(current, entries, paths)
    if current.get("body") != notes or not current.get("draft") or not current.get("prerelease"):
        raise ReleaseError("Draft changed before publication")
    assert_base_current(plan, api.items(f"repos/{REPOSITORY}/releases"))
    try:
        api.request("PATCH", release_path, {"draft": False, "prerelease": True, "make_latest": "false"})
    except APIError:
        pass  # Read back the known release instead of creating a second one.
    published = api.request("GET", release_path)
    if published.get("draft") or not published.get("prerelease") or published.get("body") != notes:
        raise ReleaseError("Publication was not confirmed; the next attempt can resume this draft")
    assert_assets(published, entries, paths)
    return {"tag": tag, "title": title, "url": published["html_url"],
            "updated": [item["branch"] for item in plan["updates"]], "apk_count": len(entries)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    planning.add_argument("--branches", required=True)
    planning.add_argument("--output", type=Path, required=True)
    planning.add_argument("--github-output", type=Path)
    publishing = commands.add_parser("publish")
    publishing.add_argument("--plan", type=Path, required=True)
    publishing.add_argument("--work-dir", type=Path, required=True)
    publishing.add_argument("--run-id", type=int, required=True)
    publishing.add_argument("--attempt", type=int, required=True)
    publishing.add_argument("--workflow-sha", required=True)
    publishing.add_argument("--apksigner", required=True)
    publishing.add_argument("--aapt", required=True)
    publishing.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    api = GitHub()
    if args.command == "plan":
        plan = create_plan(api, args.branches)
        args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        matrix = {"include": [{"branch": item["branch"], "sha": item["sha"]} for item in plan["updates"]]}
        has_updates = str(bool(plan["updates"])).lower()
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"matrix={json.dumps(matrix)}\nhas_updates={has_updates}\n")
        message = "本次更新分支：" + ", ".join(item["branch"] for item in plan["updates"]) if plan["updates"] else "所选分支源码未变，不重复构建或发布。"
        print(message)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write(message + "\n")
        return
    if args.run_id <= 0 or args.attempt <= 0 or not SHA.fullmatch(args.workflow_sha):
        raise ReleaseError("Invalid workflow run identity")
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    if not plan.get("updates"):
        raise ReleaseError("No updated branch was selected")
    entries, paths = collect_snapshot(api, plan, args.work_dir, args.run_id, args.attempt,
                                     args.workflow_sha, lambda path: verify_apk(path, args.apksigner, args.aapt))
    result = publish_snapshot(api, plan, entries, paths, args.run_id, args.workflow_sha,
                              args.work_dir, datetime.now(timezone.utc), args.dry_run)
    (args.work_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(f"\nNightly: {result.get('url', result['tag'])}\n")


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, KeyError, ValueError, OSError, zipfile.BadZipFile) as error:
        raise SystemExit(f"Nightly release stopped: {error}") from error
