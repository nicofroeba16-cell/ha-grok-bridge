"""Local safety and contract checks; no GitHub or Home Assistant connection is used."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).parents[2] / "addon" / "file_bridge.py"
SPEC = importlib.util.spec_from_file_location("file_bridge", MODULE)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FileContractTests(unittest.TestCase):
    def test_allows_declared_configuration_paths(self) -> None:
        self.assertTrue(bridge.allowed_path("configuration.yaml"))
        self.assertTrue(bridge.allowed_path("dashboards/zuhause.yaml"))
        self.assertTrue(bridge.allowed_path("themes/apple.yaml"))
        self.assertTrue(bridge.allowed_path("www/apple-optik.js"))

    def test_rejects_secrets_and_runtime_data(self) -> None:
        for path in ("secrets.yaml", ".cloud/remote_private.pem", ".storage/core.config", "home-assistant_v2.db", "home-assistant_v2.db-wal", "home-assistant_v2.db-shm"):
            self.assertFalse(bridge.allowed_path(path))

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(ValueError): bridge.safe_rel("../../secrets.yaml")

    def test_rejects_unknown_action(self) -> None:
        with self.assertRaises(ValueError): bridge.validate_request({"action": "shell"}, {"main_branch": "main"})

    def test_requires_full_sha_for_deploy(self) -> None:
        with self.assertRaises(ValueError): bridge.validate_request({"action": "deploy", "target_commit": "abc"}, {"main_branch": "main"})

    @patch.object(bridge, "git")
    def test_accepts_valid_deploy_scope(self, mocked_git) -> None:
        request={"action":"deploy","target_commit":"a"*40,"scope":["dashboards","themes"]}
        action,scopes=bridge.validate_request(request,{"main_branch":"main"})
        self.assertEqual("deploy",action); self.assertEqual({"dashboards","themes"},scopes)
        mocked_git.assert_called_once_with(["merge-base","--is-ancestor","a"*40,"origin/main"],cwd=bridge.WORK)

    def test_scope_must_be_a_list(self) -> None:
        with self.assertRaises(ValueError): bridge.validate_request({"action":"deploy","target_commit":"a"*40,"scope":"themes"},{"main_branch":"main"})

    def test_supervisor_check_requires_token(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SUPERVISOR_TOKEN"):
                bridge.supervisor_core_check()

    @patch.object(bridge.urllib.request, "urlopen")
    @patch.dict("os.environ", {"SUPERVISOR_TOKEN":"test-token"}, clear=True)
    def test_supervisor_check_accepts_ok_response(self, urlopen) -> None:
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"result":"ok"}'
        urlopen.return_value=Response()
        bridge.supervisor_core_check()
        request=urlopen.call_args.args[0]
        self.assertEqual("POST",request.get_method())
        self.assertEqual("Bearer test-token",request.get_header("Authorization"))

    @patch.object(bridge.urllib.request, "urlopen")
    @patch.dict("os.environ", {"SUPERVISOR_TOKEN":"test-token"}, clear=True)
    def test_supervisor_check_rejects_failed_response(self, urlopen) -> None:
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"result":"error","message":"bad config"}'
        urlopen.return_value=Response()
        with self.assertRaisesRegex(RuntimeError,"Supervisor core check failed"):
            bridge.supervisor_core_check()

    def test_failed_request_is_recorded_and_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            last_request=Path(tmp)/"last_request_id"
            with patch.object(bridge,"ensure_repo"), patch.object(bridge,"read_ref_file",return_value={"id":"req-1","action":"deploy"}), patch.object(bridge,"validate_request",side_effect=ValueError("bad request")), patch.object(bridge,"publish_status") as publish, patch.object(bridge,"LAST_REQUEST",last_request):
                bridge.process({"control_branch":"bridge-control"}); bridge.process({"control_branch":"bridge-control"})
            self.assertEqual("req-1\n",last_request.read_text()); self.assertEqual(1,publish.call_count); self.assertFalse(publish.call_args.args[1]["ok"])

    def test_removes_obsolete_allowed_files_only_in_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); destination=root/"config"; source=root/"candidate"
            (destination/"dashboards").mkdir(parents=True); (destination/"themes").mkdir(parents=True); (source/"dashboards").mkdir(parents=True); (source/"themes").mkdir(parents=True)
            (destination/"dashboards"/"old.yaml").write_text("old"); (destination/"themes"/"keep.yaml").write_text("keep"); (source/"dashboards"/"new.yaml").write_text("new")
            removed=bridge.remove_allowed_not_in_source(destination,source,{"dashboards"})
            self.assertEqual(["dashboards/old.yaml"],removed); self.assertFalse((destination/"dashboards"/"old.yaml").exists()); self.assertTrue((destination/"themes"/"keep.yaml").exists())

    def test_failed_rollback_restores_pre_rollback_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); config=root/"config"; backups=root/"backups"; config.mkdir(); backups.mkdir(); (config/"configuration.yaml").write_text("current")
            target_backup=backups/"target"; target_backup.mkdir(); (target_backup/"configuration.yaml").write_text("old")
            previous={"commit":"a"*40,"backup":str(target_backup),"files":["configuration.yaml"]}; last_good=root/"last_known_good.json"; last_good.write_text(json.dumps(previous))
            with patch.object(bridge,"CONFIG",config), patch.object(bridge,"BACKUPS",backups), patch.object(bridge,"LAST_GOOD",last_good), patch.object(bridge,"supervisor_core_check",side_effect=RuntimeError("check failed")):
                with self.assertRaisesRegex(RuntimeError,"pre-rollback state restored"): bridge.rollback()
            self.assertEqual("current",(config/"configuration.yaml").read_text())
            safety_backups=[p for p in backups.iterdir() if p.is_dir() and p.name!="target"]
            self.assertEqual(1,len(safety_backups))


if __name__ == "__main__": unittest.main()
