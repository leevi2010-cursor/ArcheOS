import json
from pathlib import Path

import unittest

from archeos.timeline import TimelineError, build_timelines, load_selection, render_markdown


class TimelineTests(unittest.TestCase):
  def test_selection_requires_three_to_five(self):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
      p = Path(d) / "s.json"; p.write_text(json.dumps({"objects": [{"object_id": "1", "label": "x"}]}))
      with self.assertRaises(TimelineError): load_selection(p)


  def test_build_and_resume_has_zero_repeat_calls(self):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
      tmp_path = Path(d)
    selections = tuple(type("S", (), {"object_id": str(i), "label": str(i), "atomic_information_ids": (f"a{i}",)})() for i in range(3))
    contexts = {str(i): {"atomic_information_ids": [f"a{i}"], "evidence_ids": [f"e{i}"]} for i in range(3)}
    calls = []
    def provider(ctx):
        calls.append(ctx["atomic_information_ids"])
        oid = ctx["atomic_information_ids"][0][1:]
        return {"object_id": oid, "what_it_is": "x", "timeline_entries": [], "current_state": {"state": "ok", "evidence_ids": [f"e{oid}"]}, "conflicts": [], "unknowns": [], "information_accounting": {f"a{oid}": "current_state"}, "coverage": {"complete": True}}
    result = build_timelines(selections, contexts, provider, tmp_path / "out")
    self.assertEqual(result["provider_calls"], 3)
    self.assertEqual(len(calls), 3)
    result = build_timelines(selections, contexts, provider, tmp_path / "out", resume=True)
    self.assertEqual(result["provider_calls"], 0)
    self.assertEqual(len(calls), 3)
    self.assertIn("当前状态", render_markdown(result["packages"][0]))
