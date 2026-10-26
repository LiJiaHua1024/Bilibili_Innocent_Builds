from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/nightly_release.py"
SPEC = importlib.util.spec_from_file_location("nightly_release", MODULE_PATH)
n = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = n
SPEC.loader.exec_module(n)

OLD = "a" * 40
NEW = "b" * 40
OTHER = "c" * 40
WORKFLOW = "d" * 40
RUN = 123
NOW = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)  # Oct 26 in Shanghai.
BRANCH = "feat/npatch-compat"
SECOND = "experiment/audio-enhance"


def asset(entry, identifier=1):
    return {"id": identifier, "name": entry.asset_name, "digest": "sha256:" + entry.sha256,
            "size": entry.size, "state": "uploaded"}


def fixture_entry(branch=BRANCH, sha=OLD, data=b"old APK"):
    return n.Entry(branch, sha, n.apk_name(branch, sha), hashlib.sha256(data).hexdigest(), len(data))


def release(entries, identifier=10, tag="nightly-20261025", run_id=100, draft=False):
    state = {"schema": 1, "source_repository": n.SOURCE, "run_id": run_id,
             "workflow_sha": WORKFLOW, "entries": [e.serial() for e in entries]}
    return {"id": identifier, "tag_name": tag, "name": "Nightly Build 20261025",
            "draft": draft, "prerelease": True, "published_at": None if draft else "2026-10-25T08:00:00Z",
            "body": n.WARNING + n.state_comment(state), "assets": [asset(e, i + 1) for i, e in enumerate(entries)],
            "html_url": f"https://github.com/{n.REPOSITORY}/releases/tag/{tag}"}


def transition(branch=BRANCH, before=OLD, after=NEW):
    return {"branch": branch, "before": before, "sha": after,
            "changes": {"status": "ahead", "subjects": ["fix: improve compatibility"],
                        "url": f"https://github.com/{n.SOURCE}/compare/{before}...{after}", "total": 1}}


def fixture_plan(base=None, updates=None):
    return {"schema": 1, "repository": n.REPOSITORY, "source_repository": n.SOURCE,
            "base_id": base["id"] if base else None, "base_tag": base["tag_name"] if base else None,
            "base_fingerprint": n.fingerprint(base),
            "base_entries": [dict(e, asset_id=i + 1) for i, e in enumerate(n.state_from(base["body"])["entries"])] if base else [],
            "updates": updates if updates is not None else [transition(before=OLD if base else None)],
            "unchanged_requested": []}


class FakeGitHub:
    def __init__(self, releases=None):
        self.releases = deepcopy(releases or [])
        self.heads = {BRANCH: NEW, SECOND: OTHER}
        self.refs = []
        self.artifacts = []
        self.downloads = {}
        self.writes = []
        self.uploads = []
        self.lost_create = self.lost_upload = self.lost_publish = False
        self.fail_uploads = 0
        self.stale_after_upload = False

    def items(self, path, field=None):
        if path.endswith("/branches"):
            return [{"name": key, "commit": {"sha": value}} for key, value in self.heads.items()] + [{"name": "main", "commit": {"sha": "f" * 40}}]
        if path.endswith("/releases"):
            if self.stale_after_upload and self.uploads:
                self.releases[0]["body"] += "manually edited"
            return deepcopy(self.releases)
        if path.endswith("/tags"):
            return [{"name": ref["ref"].removeprefix("refs/tags/")} for ref in self.refs]
        if path.endswith("/artifacts"):
            return deepcopy(self.artifacts)
        raise AssertionError(path)

    def request(self, method, path, payload=None):
        if method != "GET":
            self.writes.append((method, path, deepcopy(payload)))
        if method == "GET" and "/compare/" in path:
            return {"status": "ahead", "commits": [{"commit": {"message": "fix: improve compatibility\n\nDetails"}}], "total_commits": 1}
        if method == "GET" and "/commits/" in path:
            value = path.rsplit("/", 1)[1]
            sha = next((sha for sha in (OLD, NEW, OTHER) if sha.startswith(value)), value)
            return {"sha": sha, "commit": {"message": "fix: new branch"}}
        if method == "POST" and path.endswith("/git/refs"):
            self.refs.append({"ref": payload["ref"], "object": {"sha": payload["sha"], "type": "commit"}})
            return deepcopy(self.refs[-1])
        if method == "GET" and "/git/ref/tags/" in path:
            tag = path.split("/git/ref/tags/", 1)[1]
            return deepcopy(next(ref for ref in self.refs if ref["ref"] == "refs/tags/" + tag))
        if method == "POST" and path.endswith("/releases"):
            result = dict(payload, id=1000, assets=[], published_at=None,
                          html_url=f"https://github.com/{n.REPOSITORY}/releases/tag/{payload['tag_name']}")
            self.releases.append(result)
            if self.lost_create:
                self.lost_create = False
                raise n.APIError("response lost")
            return deepcopy(result)
        if re_path := re.search(r"/releases/(\d+)$", path):
            current = next(r for r in self.releases if r["id"] == int(re_path[1]))
            if method == "GET":
                return deepcopy(current)
            if method == "PATCH":
                current.update(payload)
                current["published_at"] = "2026-10-26T00:00:00Z"
                if self.lost_publish:
                    self.lost_publish = False
                    raise n.APIError("response lost")
                return deepcopy(current)
        raise AssertionError((method, path, payload))

    def download(self, path, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.downloads[path])

    def upload(self, tag, path):
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise n.ReleaseError("temporary upload failure")
        current = next(r for r in self.releases if r["tag_name"] == tag)
        current["assets"].append({"id": 100 + len(current["assets"]), "name": path.name,
                                  "size": path.stat().st_size, "state": "uploaded", "digest": "sha256:" + n.digest(path)})
        self.uploads.append(path.name)
        if self.lost_upload:
            self.lost_upload = False
            raise n.ReleaseError("response lost")


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_paths(self, entries):
        paths = {}
        for entry in entries:
            path = self.directory / entry.asset_name
            path.write_bytes(b"new APK" if entry.source_sha == NEW else b"old APK")
            entry.sha256, entry.size = n.digest(path), path.stat().st_size
            paths[entry.branch] = path
        return paths

    def archive(self, update=None, receipt_changes=None, extra=None):
        update = update or transition()
        receipt = {"source_repository": n.SOURCE, "workflow_repository": n.REPOSITORY,
                   "branch": update["branch"], "source_sha": update["sha"], "workflow_sha": WORKFLOW,
                   "certificate_sha256": n.CERTIFICATE,
                   "run_url": f"https://github.com/{n.REPOSITORY}/actions/runs/{RUN}"}
        receipt.update(receipt_changes or {})
        path = self.directory / "artifact.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("app-release.apk", b"new APK")
            archive.writestr("build-info.txt", "\n".join(f"{key}={value}" for key, value in receipt.items()))
            archive.writestr("SHA256SUMS", hashlib.sha256(b"new APK").hexdigest() + "  app-release.apk\n")
            if extra:
                archive.writestr(*extra)
        return path

    def test_one_changed_branch_keeps_other_five(self):
        old = [fixture_entry()] + [fixture_entry(f"experiment/test-{i}", OTHER) for i in range(5)]
        api = FakeGitHub([release(old)])
        api.heads.update({entry.branch: entry.source_sha for entry in old[1:]})
        plan = n.create_plan(api, BRANCH)
        self.assertEqual([x["branch"] for x in plan["updates"]], [BRANCH])
        self.assertEqual(len(plan["base_entries"]), 6)
        self.assertEqual(plan["updates"][0]["sha"], NEW)

    def test_unchanged_selection_does_not_rebuild(self):
        api = FakeGitHub([release([fixture_entry()])])
        api.heads[BRANCH] = OLD
        plan = n.create_plan(api, BRANCH)
        self.assertEqual(plan["updates"], [])
        self.assertEqual(plan["unchanged_requested"], [BRANCH])

    def test_all_excludes_main_and_only_includes_changes(self):
        api = FakeGitHub([release([fixture_entry(SECOND, OTHER)])])
        plan = n.create_plan(api, "all")
        self.assertEqual([x["branch"] for x in plan["updates"]], [BRANCH])
        self.assertEqual(plan["unchanged_requested"], [SECOND])

    def test_multiple_selected_branches_are_deduplicated(self):
        plan = n.create_plan(FakeGitHub(), f"{BRANCH}, {SECOND}\n{BRANCH}")
        self.assertEqual(len(plan["updates"]), 2)

    def test_invalid_or_main_selection_is_rejected(self):
        for name in ("", "main", "missing", "../bad", "all,feat/npatch-compat"):
            with self.subTest(name=name), self.assertRaises(n.ReleaseError):
                n.create_plan(FakeGitHub(), name)

    def test_normalized_branch_collision_is_rejected(self):
        api = FakeGitHub()
        api.heads = {"feature/a_b": NEW, "feature_a/b": OTHER}
        with self.assertRaisesRegex(n.ReleaseError, "collide"):
            n.create_plan(api, "all")

    def test_manual_first_release_can_be_bootstrapped(self):
        old = release([fixture_entry()])
        old["body"] = f"| `{BRANCH}` | [CI](https://github.com/{n.REPOSITORY}/actions/runs/42) |"
        entries = n.resolve_base(FakeGitHub(), old, [BRANCH])
        self.assertEqual(entries[0].source_sha, OLD)
        self.assertEqual(entries[0].run_url, f"https://github.com/{n.REPOSITORY}/actions/runs/42")

    def test_deleted_branch_can_be_identified_from_manual_table(self):
        old = release([fixture_entry()])
        old["body"] = f"| `{BRANCH}` | legacy |"
        self.assertEqual(n.resolve_base(FakeGitHub(), old, [SECOND])[0].branch, BRANCH)

    def test_previous_release_rejects_non_apk_attachment(self):
        old = release([fixture_entry()])
        old["assets"].append({"name": "build-info.txt"})
        with self.assertRaises(n.ReleaseError):
            n.resolve_base(FakeGitHub(), old, [BRANCH])

    def test_previous_release_rejects_digest_tampering(self):
        old = release([fixture_entry()])
        old["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(n.ReleaseError):
            n.resolve_base(FakeGitHub(), old, [BRANCH])

    def test_daily_tag_uses_shanghai_time(self):
        self.assertEqual(n.next_tag([], NOW), ("nightly-20261026", "Nightly Build 20261026"))

    def test_daily_tag_skips_existing_releases_and_orphan_tags(self):
        self.assertEqual(n.next_tag(["nightly-20261026", "nightly-20261026.3", "nightly-20261025.7"], NOW),
                         ("nightly-20261026.4", "Nightly Build 20261026.4"))

    def test_invalid_tag_date_is_rejected(self):
        with self.assertRaises(n.ReleaseError):
            n.parse_tag("nightly-20260231")

    def test_apk_names_are_clean(self):
        self.assertEqual(n.apk_name(BRANCH, NEW), "BIL-feat_npatch-compat-bbbbbbbbbbbb.apk")

    def test_state_comment_roundtrip_escapes_html_terminators(self):
        state = {"schema": 1, "source_repository": n.SOURCE, "name": "x-->y<z"}
        encoded = n.state_comment(state)
        self.assertEqual(n.state_from(encoded), state)
        self.assertNotIn("x-->y", encoded)

    def test_duplicate_state_comments_are_rejected(self):
        comment = n.state_comment({"schema": 1, "source_repository": n.SOURCE})
        with self.assertRaises(n.ReleaseError):
            n.state_from(comment + comment)

    def test_wrong_state_repository_is_rejected(self):
        with self.assertRaises(n.ReleaseError):
            n.state_from(n.state_comment({"schema": 1, "source_repository": "other/repo"}))

    def test_artifact_extracts_only_apk(self):
        output = self.directory / n.apk_name(BRANCH, NEW)
        entry = n.read_new_artifact(self.archive(), transition(), RUN, WORKFLOW, output)
        self.assertEqual(entry.sha256, hashlib.sha256(b"new APK").hexdigest())
        self.assertEqual(output.read_bytes(), b"new APK")

    def test_artifact_rejects_wrong_source_workflow_or_certificate(self):
        for field, value in (("source_sha", OLD), ("workflow_sha", OLD), ("branch", SECOND),
                             ("source_repository", "other/repo"), ("certificate_sha256", "0" * 64),
                             ("run_url", "https://example.com/not-a-run")):
            with self.subTest(field=field), self.assertRaises(n.ReleaseError):
                n.read_new_artifact(self.archive(receipt_changes={field: value}), transition(), RUN, WORKFLOW, self.directory / "result.apk")

    def test_artifact_rejects_path_traversal(self):
        with self.assertRaisesRegex(n.ReleaseError, "Unsafe"):
            n.read_new_artifact(self.archive(extra=("../escape", b"x")), transition(), RUN, WORKFLOW, self.directory / "result.apk")

    def test_artifact_rejects_symlink(self):
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(n.ReleaseError, "Unsafe"):
            n.read_new_artifact(self.archive(extra=(info, b"target")), transition(), RUN, WORKFLOW, self.directory / "result.apk")

    def test_artifact_rejects_hash_mismatch(self):
        path = self.archive()
        with zipfile.ZipFile(path, "a") as archive:
            # Duplicate names must also fail rather than selecting the later payload.
            with __import__("warnings").catch_warnings():
                __import__("warnings").simplefilter("ignore")
                archive.writestr("app-release.apk", b"changed APK")
        with self.assertRaises(n.ReleaseError):
            n.read_new_artifact(path, transition(), RUN, WORKFLOW, self.directory / "result.apk")

    def test_certificate_parser_accepts_build_tools_37(self):
        outputs = [f"Number of signers: 1\nV3.0 Signer: certificate SHA-256 digest: {n.CERTIFICATE}\n",
                   f"package: name='{n.PACKAGE}' versionCode='21'\n"]
        with patch.object(n, "checked", side_effect=outputs):
            n.verify_apk(self.directory / "test.apk", "apksigner", "aapt")

    def test_certificate_parser_rejects_debuggable_or_wrong_signer(self):
        cases = [("0" * 64, ""), (n.CERTIFICATE, "application-debuggable\n")]
        for certificate, flags in cases:
            with self.subTest(flags=flags), patch.object(n, "checked", side_effect=[
                f"Number of signers: 1\nV2 Signer: certificate SHA-256 digest: {certificate}\n",
                f"package: name='{n.PACKAGE}'\n{flags}",
            ]), self.assertRaises(n.ReleaseError):
                n.verify_apk(self.directory / "test.apk", "apksigner", "aapt")

    def test_collect_replaces_one_apk_and_retains_others(self):
        old = [fixture_entry(), fixture_entry(SECOND, OTHER)]
        base = release(old)
        plan = fixture_plan(base)
        archive = self.archive()
        api = FakeGitHub([base])
        api.artifacts = [{"id": 9, "name": f"UNSTABLE-EXPERIMENTAL-{n.slug(BRANCH)}-{NEW[:12]}-{RUN}-1",
                          "digest": "sha256:" + n.digest(archive), "expired": False}]
        api.downloads[f"repos/{n.REPOSITORY}/actions/artifacts/9/zip"] = archive.read_bytes()
        api.downloads[f"repos/{n.REPOSITORY}/releases/assets/2"] = b"old APK"
        verified = []
        entries, paths = n.collect_snapshot(api, plan, self.directory / "work", RUN, 1, WORKFLOW, lambda p: verified.append(p.name))
        self.assertEqual({e.branch: e.source_sha for e in entries}, {BRANCH: NEW, SECOND: OTHER})
        self.assertEqual(len(verified), 2)
        self.assertEqual(len(paths), 2)
        self.assertFalse(api.writes)

    def test_future_attempt_artifact_stops_collection(self):
        api = FakeGitHub()
        api.artifacts = [{"name": f"UNSTABLE-EXPERIMENTAL-{n.slug(BRANCH)}-{NEW[:12]}-{RUN}-3"}]
        with self.assertRaises(n.ReleaseError):
            n.collect_snapshot(api, fixture_plan(), self.directory, RUN, 2, WORKFLOW, lambda p: None)

    def test_notes_distinguish_updated_and_carried(self):
        entries = [fixture_entry(sha=NEW), fixture_entry(SECOND, OTHER)]
        notes = n.release_notes(fixture_plan(release([fixture_entry(), entries[1]])), entries,
                                "nightly-20261026", RUN, WORKFLOW)
        self.assertTrue(notes.startswith(n.WARNING))
        self.assertIn(f"更新 <code>{BRANCH}</code>", notes)
        self.assertIn(f"[{OLD[:12]} → {NEW[:12]}]", notes)
        self.assertIn(f"| <code>{SECOND}</code> | 沿用 |", notes)
        self.assertEqual(n.state_from(notes)["updated_branches"], [BRANCH])
        self.assertIn("本次未重新构建", notes)
        self.assertNotIn("BIL-EXTREMELY-UNSTABLE-", notes)

    def test_commit_subjects_are_linked_without_dumping_into_notes(self):
        plan = fixture_plan()
        plan["updates"][0]["changes"]["subjects"] = ["<script>bad</script> | table"]
        notes = n.release_notes(plan, [fixture_entry(sha=NEW)], "nightly-20261026", RUN, WORKFLOW)
        self.assertNotIn("<script>", notes)
        self.assertNotIn("table", notes)
        self.assertIn(plan["updates"][0]["changes"]["url"], notes)

    def test_publish_dry_run_never_writes_to_github(self):
        entry = fixture_entry(sha=NEW)
        paths = self.make_paths([entry])
        api = FakeGitHub()
        result = n.publish_snapshot(api, fixture_plan(), [entry], paths, RUN, WORKFLOW, self.directory, NOW, True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["title"], "Nightly Build 20261026")
        self.assertFalse(api.writes)
        self.assertFalse(api.uploads)

    def test_publish_uploads_apks_only_and_marks_prerelease(self):
        entry = fixture_entry(sha=NEW)
        paths = self.make_paths([entry])
        api = FakeGitHub()
        result = n.publish_snapshot(api, fixture_plan(), [entry], paths, RUN, WORKFLOW, self.directory, NOW)
        self.assertEqual(result["apk_count"], 1)
        self.assertEqual(api.uploads, [entry.asset_name])
        self.assertTrue(api.releases[0]["prerelease"])
        self.assertFalse(api.releases[0]["draft"])
        self.assertEqual(api.releases[0]["make_latest"], "false")

    def test_lost_create_upload_and_publish_responses_are_recovered(self):
        entry = fixture_entry(sha=NEW)
        paths = self.make_paths([entry])
        api = FakeGitHub()
        api.lost_create = api.lost_upload = api.lost_publish = True
        n.publish_snapshot(api, fixture_plan(), [entry], paths, RUN, WORKFLOW, self.directory, NOW)
        self.assertEqual(len(api.releases), 1)
        self.assertEqual(len(api.refs), 1)
        self.assertEqual(len(api.uploads), 1)

    def test_upload_retry_can_succeed_on_last_attempt(self):
        entry = fixture_entry(sha=NEW)
        paths = self.make_paths([entry])
        api = FakeGitHub()
        api.fail_uploads = 2
        n.publish_snapshot(api, fixture_plan(), [entry], paths, RUN, WORKFLOW, self.directory, NOW)
        self.assertFalse(api.releases[0]["draft"])

    def test_retry_of_published_run_is_idempotent(self):
        entry = fixture_entry(sha=NEW)
        paths = self.make_paths([entry])
        api = FakeGitHub()
        plan = fixture_plan()
        first = n.publish_snapshot(api, plan, [entry], paths, RUN, WORKFLOW, self.directory, NOW)
        writes = len(api.writes)
        second = n.publish_snapshot(api, plan, [entry], paths, RUN, WORKFLOW, self.directory, NOW)
        self.assertEqual(first["url"], second["url"])
        self.assertTrue(second["reused_run"])
        self.assertEqual(writes, len(api.writes))

    def test_stale_base_stops_before_tag_creation(self):
        old = release([fixture_entry()])
        plan = fixture_plan(old)
        api = FakeGitHub([old])
        api.releases[0]["body"] += "manual edit"
        entry = fixture_entry(sha=NEW)
        with self.assertRaisesRegex(n.ReleaseError, "changed"):
            n.publish_snapshot(api, plan, [entry], self.make_paths([entry]), RUN, WORKFLOW, self.directory, NOW)
        self.assertFalse(api.writes)

    def test_stale_base_after_upload_is_not_published(self):
        old = release([fixture_entry()])
        api = FakeGitHub([old])
        api.stale_after_upload = True
        entry = fixture_entry(sha=NEW)
        with self.assertRaisesRegex(n.ReleaseError, "changed"):
            n.publish_snapshot(api, fixture_plan(old), [entry], self.make_paths([entry]), RUN, WORKFLOW, self.directory, NOW)
        self.assertTrue(api.releases[-1]["draft"])
        self.assertFalse(any(method == "PATCH" for method, _, _ in api.writes))

    def test_changed_snapshot_cannot_reuse_same_run(self):
        entry = fixture_entry(sha=NEW)
        paths = self.make_paths([entry])
        api = FakeGitHub()
        plan = fixture_plan()
        n.publish_snapshot(api, plan, [entry], paths, RUN, WORKFLOW, self.directory, NOW)
        entry.source_sha = OTHER
        with self.assertRaisesRegex(n.ReleaseError, "different snapshot"):
            new_plan = deepcopy(plan)
            new_plan["updates"][0]["sha"] = OTHER
            entry.asset_name = n.apk_name(BRANCH, OTHER)
            replacement = self.directory / entry.asset_name
            replacement.write_bytes(paths[BRANCH].read_bytes())
            n.publish_snapshot(api, new_plan, [entry], {BRANCH: replacement}, RUN, WORKFLOW, self.directory, NOW)

    def test_retry_publisher_reuses_prior_attempt_signed_artifact(self):
        archive = self.archive(update=transition(before=None))
        api = FakeGitHub()
        api.artifacts = [{"id": 9, "name": f"UNSTABLE-EXPERIMENTAL-{n.slug(BRANCH)}-{NEW[:12]}-{RUN}-1",
                          "digest": "sha256:" + n.digest(archive), "expired": False}]
        api.downloads[f"repos/{n.REPOSITORY}/actions/artifacts/9/zip"] = archive.read_bytes()
        entries, _ = n.collect_snapshot(api, fixture_plan(), self.directory / "retry", RUN, 2, WORKFLOW, lambda p: None)
        self.assertEqual(entries[0].source_sha, NEW)

    def test_publisher_refuses_to_drop_carried_branch(self):
        old = release([fixture_entry(), fixture_entry(SECOND, OTHER)])
        api = FakeGitHub([old])
        entry = fixture_entry(sha=NEW)
        with self.assertRaisesRegex(n.ReleaseError, "missing"):
            n.publish_snapshot(api, fixture_plan(old), [entry], self.make_paths([entry]), RUN, WORKFLOW, self.directory, NOW)
        self.assertFalse(api.writes)

    def test_publisher_refuses_unverified_local_bytes(self):
        entry = fixture_entry(sha=NEW)
        paths = self.make_paths([entry])
        paths[BRANCH].write_bytes(b"changed after verification")
        api = FakeGitHub()
        with self.assertRaisesRegex(n.ReleaseError, "verified Release APKs"):
            n.publish_snapshot(api, fixture_plan(), [entry], paths, RUN, WORKFLOW, self.directory, NOW)
        self.assertFalse(api.writes)

    def test_automatic_snapshot_contains_one_new_and_five_reused_apks(self):
        old = [fixture_entry()] + [fixture_entry(f"experiment/test-{i}", OTHER) for i in range(5)]
        base = release(old)
        updated = [fixture_entry(sha=NEW)] + old[1:]
        paths = self.make_paths(updated)
        api = FakeGitHub([base])
        result = n.publish_snapshot(api, fixture_plan(base), updated, paths, RUN, WORKFLOW, self.directory, NOW)
        self.assertEqual(result["updated"], [BRANCH])
        self.assertEqual(len(api.releases[-1]["assets"]), 6)
        self.assertEqual(n.state_from(api.releases[-1]["body"])["updated_branches"], [BRANCH])

    def test_tag_must_point_to_this_workflow_commit(self):
        api = FakeGitHub()
        api.refs = [{"ref": "refs/tags/nightly-20261026", "object": {"type": "commit", "sha": OLD}}]
        with self.assertRaisesRegex(n.ReleaseError, "expected workflow commit"):
            n.assert_tag(api, "nightly-20261026", WORKFLOW)

    def test_actions_zip_and_release_binary_use_different_media_types(self):
        data = b"\x00APK\xff\x01\x80"
        for path, binary_header in (("repos/x/y/actions/artifacts/9/zip", False), ("repos/x/y/releases/assets/9", True)):
            def transport(command, *, stdout, stderr, timeout):
                self.assertEqual("Accept: application/octet-stream" in command, binary_header)
                stdout.write(data)
                return subprocess.CompletedProcess(command, 0, stderr=b"")
            target = self.directory / ("release.apk" if binary_header else "artifact.zip")
            with self.subTest(path=path), patch.object(n.subprocess, "run", side_effect=transport):
                n.GitHub().download(path, target)
            self.assertEqual(target.read_bytes(), data)

    def test_download_keeps_http_error_and_removes_partial_file(self):
        response = subprocess.CompletedProcess([], 1, stderr=b"Unsupported Accept header (HTTP 415)")
        target = self.directory / "artifact.zip"
        with patch.object(n.subprocess, "run", return_value=response) as transport, self.assertRaisesRegex(n.ReleaseError, "HTTP 415"):
            n.GitHub().download("repos/x/y/actions/artifacts/9/zip", target)
        self.assertEqual(transport.call_count, 1)
        self.assertFalse(target.with_name("artifact.zip.part").exists())


if __name__ == "__main__":
    unittest.main()
