"""会话断点持久化（agent_session.json）：保存 / 损坏回退 / 新键补默认 / 断点环节推导。"""
from __future__ import annotations

import json


class TestSessionPersistence:
    """function_call_agent：_save_session / _load_session 原子落盘与恢复。"""

    def test_roundtrip_state_and_messages(self, tmp_path):
        from src.agent import function_call_agent as fca

        monkey = fca._SESSION_PATH
        fca._SESSION_PATH = tmp_path / "agent_session.json"
        try:
            st = fca._new_state()
            st["excel_path"] = "output/excel/x_pending_audit.xlsx"
            st["audit_pending"] = True
            st["passed_product_ids"] = [11, 22]
            msgs = [
                {"role": "system", "content": [{"text": "sys"}]},
                {"role": "user", "content": [{"text": "执行AI选品"}]},
            ]
            fca._save_session(msgs, st)
            m2, s2 = fca._load_session()
            assert m2 == msgs
            assert s2["excel_path"] == "output/excel/x_pending_audit.xlsx"
            assert s2["audit_pending"] is True
            assert s2["passed_product_ids"] == [11, 22]
        finally:
            fca._SESSION_PATH = monkey

    def test_new_keys_get_defaults(self, tmp_path):
        """旧版本断点文件缺新键时，用 _new_state() 默认值补齐（不 KeyError）。"""
        from src.agent import function_call_agent as fca

        monkey = fca._SESSION_PATH
        fca._SESSION_PATH = tmp_path / "agent_session.json"
        try:
            fca._SESSION_PATH.write_text(
                json.dumps({"saved_at": "2026-10-15T00:00:00",
                            "messages": [],
                            "state": {"excel_path": "a.xlsx"}}),
                encoding="utf-8",
            )
            _, s = fca._load_session()
            assert s["excel_path"] == "a.xlsx"
            assert s["image_audit_pending"] is False   # 新键补默认
            assert s["crawl_runs"] == []
        finally:
            fca._SESSION_PATH = monkey

    def test_corrupt_file_falls_back_to_new(self, tmp_path):
        from src.agent import function_call_agent as fca

        monkey = fca._SESSION_PATH
        fca._SESSION_PATH = tmp_path / "agent_session.json"
        try:
            fca._SESSION_PATH.write_text("{broken", encoding="utf-8")
            m, s = fca._load_session()
            assert m is None and s is None
        finally:
            fca._SESSION_PATH = monkey

    def test_missing_file_returns_none(self, tmp_path):
        from src.agent import function_call_agent as fca

        monkey = fca._SESSION_PATH
        fca._SESSION_PATH = tmp_path / "nope.json"
        try:
            m, s = fca._load_session()
            assert m is None and s is None
        finally:
            fca._SESSION_PATH = monkey

    def test_save_is_atomic_no_tmp_left(self, tmp_path):
        from src.agent import function_call_agent as fca

        monkey = fca._SESSION_PATH
        fca._SESSION_PATH = tmp_path / "agent_session.json"
        try:
            fca._save_session([], fca._new_state())
            assert fca._SESSION_PATH.exists()
            assert not fca._SESSION_PATH.with_suffix(".json.tmp").exists()
        finally:
            fca._SESSION_PATH = monkey


class TestResumeStage:
    """_print_resume：断点环节由 state 标志推导（图片>素材>选品>采集 优先级）。"""

    def _stage_of(self, state) -> str:
        from src.agent import function_call_agent as fca

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            fca._print_resume([{"role": "user", "content": [{"text": "x"}]}], state)
        for line in buf.getvalue().splitlines():
            if "断点环节" in line:
                return line
        raise AssertionError(buf.getvalue())

    def test_image_audit_pending(self):
        from src.agent import function_call_agent as fca

        st = fca._new_state()
        st["image_audit_pending"] = True
        st["image_excel_path"] = "i.xlsx"
        assert "i.xlsx" in self._stage_of(st)

    def test_material_audit_pending(self):
        from src.agent import function_call_agent as fca

        st = fca._new_state()
        st["material_audit_pending"] = True
        st["material_excel_path"] = "m.xlsx"
        assert "m.xlsx" in self._stage_of(st)

    def test_material_passed_ready_for_image(self):
        from src.agent import function_call_agent as fca

        st = fca._new_state()
        st["material_passed_ids"] = [1, 2, 3]
        assert "3 个 pass" in self._stage_of(st) and "生图" in self._stage_of(st)

    def test_fresh_session(self):
        from src.agent import function_call_agent as fca

        assert "全新开局" in self._stage_of(fca._new_state())
