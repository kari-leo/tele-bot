import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tele_bot.skills.loader import MAX_BODY_CHARS, MAX_PROMPT_CHARS, SkillLoader, build_skill_tool
from tele_bot.workflows.react_graph import build_react_graph


class SkillLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "global.md").write_text("---\nname: global\nglobal: true\n---\nALWAYS_OBEY", encoding="utf-8")
        (self.root / "alpha.md").write_text("---\nname: alpha\ndescription: Handle alpha tasks\ntool: alpha_tool\n---\nALPHA_BODY_SECRET", encoding="utf-8")
        (self.root / "beta.md").write_text("---\nname: beta\ndescription: Handle beta tasks\n---\nBETA_BODY_SECRET", encoding="utf-8")
        self.loader = SkillLoader(self.root)
        self.loader.available_tools = {"alpha_tool", "load_skill"}

    def test_system_prompt_contains_only_global_rules_and_catalog(self):
        prompt = self.loader.build_system_prompt()
        self.assertIn("ALWAYS_OBEY", prompt)
        self.assertIn("Handle alpha tasks", prompt)
        self.assertNotIn("ALPHA_BODY_SECRET", prompt)
        self.assertNotIn("BETA_BODY_SECRET", prompt)
        result = json.loads(build_skill_tool(self.loader).invoke({"name": "alpha"}))
        self.assertEqual(result["body"], "ALPHA_BODY_SECRET")

    def test_unknown_unavailable_and_oversized_are_explicit(self):
        self.assertEqual(self.loader.load_skill("../alpha")["status"], "unavailable")
        self.loader.available_tools = {"load_skill"}
        self.assertIn("unavailable", self.loader.catalog())
        self.assertEqual(self.loader.load_skill("alpha")["missing_tools"], ["alpha_tool"])
        (self.root / "huge.md").write_text("x" * (MAX_BODY_CHARS + 1), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "description"):
            SkillLoader(self.root).build_system_prompt()
        (self.root / "huge.md").write_text("---\ndescription: huge\n---\n" + "x" * (MAX_BODY_CHARS + 1), encoding="utf-8")
        self.assertEqual(SkillLoader(self.root).load_skill("huge")["status"], "unavailable")
        (self.root / "global.md").write_text("---\nglobal: true\n---\n" + "global\n" * MAX_PROMPT_CHARS, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "budget"):
            SkillLoader(self.root).build_system_prompt()

    def test_graph_loads_multiple_skills_only_for_current_request(self):
        outer = self

        class Model:
            calls = 0
            def invoke(self, messages, config=None):
                self.calls += 1
                prompt = str(messages[0].content)
                outer.assertIn("ALWAYS_OBEY", prompt)
                if self.calls == 1:
                    outer.assertNotIn("ALPHA_BODY_SECRET", prompt)
                    return AIMessage(content="", tool_calls=[
                        {"name": "load_skill", "args": {"name": name}, "id": name, "type": "tool_call"}
                        for name in ("alpha", "beta")])
                if self.calls == 2:
                    outer.assertEqual(prompt.count("ALPHA_BODY_SECRET"), 1)
                    outer.assertEqual(prompt.count("BETA_BODY_SECRET"), 1)
                    outer.assertFalse(any("BODY_SECRET" in str(m.content) for m in messages[1:]))
                else:
                    outer.assertFalse(any("BODY_SECRET" in str(m.content) for m in messages))
                return AIMessage(content="done")

        graph = build_react_graph(Model(), [build_skill_tool(self.loader)], skill_loader=self.loader,
                                  system_prompt=self.loader.build_system_prompt())
        config = {"configurable": {"thread_id": "skill-test"}}
        first = graph.invoke({"messages": [HumanMessage(content="alpha beta", id="first")], "iterations": 0}, config)
        self.assertTrue(any(isinstance(m, ToolMessage) and "BODY_SECRET" in m.content for m in first["messages"]))
        graph.invoke({"messages": [HumanMessage(content="hello", id="next")], "iterations": 0}, config)


if __name__ == "__main__":
    unittest.main()
