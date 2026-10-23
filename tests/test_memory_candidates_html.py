import html
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))

from baton_memory.candidates import CandidateError, parse_memory_candidates


HTML_BLOCKS = (
    '<pre data-memory-candidates="true">%s</pre>',
    '<script type="application/json" data-memory-candidates="true">%s</script>',
)


class MemoryCandidatesHtmlTests(unittest.TestCase):
    def candidate(self, text):
        return {"kind": "convention", "text": text, "source": "executor",
                "anchors": ["src/runner.py#execute"]}

    def parse_block(self, block):
        return parse_memory_candidates("<h2>记忆候选</h2>\n" + block)

    def test_r002_shaped_quoted_array(self):
        payload = (
            '[{&quot;kind&quot;: &quot;convention&quot;, &quot;text&quot;: '
            '&quot;harness/runner.py#execute 与生成的 Codex 用量包装器均用 stdin=subprocess.DEVNULL 启动子进程；'
            '生成的 Hermes/Codex exec 转发和裸 baton shim 同样隔离上游 stdin。&quot;, '
            '&quot;source&quot;: &quot;executor&quot;, &quot;anchors&quot;: '
            '[&quot;harness/runner.py#execute&quot;, &quot;harness/runner.py#install_usage_wrappers&quot;]}, '
            '{&quot;kind&quot;: &quot;command&quot;, &quot;text&quot;: '
            '&quot;README.md 的评估安装步骤在 ~/baton-test/.venv 中执行 pip install -r requirements-eval.txt；'
            'requirements-eval.txt 固定 swebench==4.1.0。&quot;, &quot;source&quot;: &quot;executor&quot;, '
            '&quot;anchors&quot;: [&quot;README.md&quot;, &quot;requirements-eval.txt&quot;]}]'
        )
        expected = [
            {"kind": "convention", "text":
             "harness/runner.py#execute 与生成的 Codex 用量包装器均用 stdin=subprocess.DEVNULL 启动子进程；"
             "生成的 Hermes/Codex exec 转发和裸 baton shim 同样隔离上游 stdin。",
             "source": "executor", "anchors": ["harness/runner.py#execute",
                                                "harness/runner.py#install_usage_wrappers"]},
            {"kind": "command", "text":
             "README.md 的评估安装步骤在 ~/baton-test/.venv 中执行 pip install -r requirements-eval.txt；"
             "requirements-eval.txt 固定 swebench==4.1.0。",
             "source": "executor", "anchors": ["README.md", "requirements-eval.txt"]},
        ]
        for wrapper in HTML_BLOCKS:
            with self.subTest(wrapper=wrapper):
                self.assertEqual(self.parse_block(wrapper % payload), expected)

    def test_entities_are_decoded_even_when_json_is_already_valid(self):
        expected = [self.candidate("a < b & c > d")]
        payload = html.escape(json.dumps(expected, ensure_ascii=False), quote=False)
        self.assertNotEqual(json.loads(payload), expected)
        for wrapper in HTML_BLOCKS:
            with self.subTest(wrapper=wrapper):
                self.assertEqual(self.parse_block(wrapper % payload), expected)

    def test_entities_are_decoded_once(self):
        expected = [self.candidate('保留 &amp; &lt; &quot;，含 "引号" 和 \'单引号\'')]
        payload = html.escape(json.dumps(expected, ensure_ascii=False))
        for wrapper in HTML_BLOCKS:
            with self.subTest(wrapper=wrapper):
                self.assertEqual(self.parse_block(wrapper % payload), expected)

    def test_unescaped_html_json_still_parses(self):
        expected = [self.candidate("未转义的原有写法")]
        for wrapper in HTML_BLOCKS:
            with self.subTest(wrapper=wrapper):
                self.assertEqual(self.parse_block(wrapper % json.dumps(expected)), expected)

    def test_empty_arrays_and_candidates_objects_still_parse(self):
        expected = [self.candidate("兼容对象包装")]
        for wrapper in HTML_BLOCKS:
            for value, result in (([], []), ({"candidates": expected}, expected)):
                with self.subTest(wrapper=wrapper, value=value):
                    payload = html.escape(json.dumps(value, ensure_ascii=False))
                    self.assertEqual(self.parse_block(wrapper % payload), result)

    def test_fenced_blocks_preserve_entities(self):
        expected = [self.candidate("a &lt; b &amp; c &quot;原文&quot;")]
        for language in ("json", "memory-candidates"):
            with self.subTest(language=language):
                block = "```%s\n%s\n```" % (language, json.dumps(expected))
                self.assertEqual(self.parse_block(block), expected)

    def test_bare_json_preserves_entities(self):
        expected = [self.candidate("保留 &lt; &amp; &quot;")]
        self.assertEqual(self.parse_block(json.dumps(expected)), expected)

    def test_surrounding_prose_fallback_decodes_html_entities(self):
        expected = [self.candidate("a < b & c")]
        payload = "候选如下：\n" + html.escape(json.dumps(expected)) + "\n结束。"
        for wrapper in HTML_BLOCKS:
            with self.subTest(wrapper=wrapper):
                self.assertEqual(self.parse_block(wrapper % payload), expected)

    def test_malformed_json_raises_candidate_error(self):
        broken = json.dumps([self.candidate("损坏的 JSON")])[:-1]
        wrappers = HTML_BLOCKS + ("```json\n%s\n```", "```memory-candidates\n%s\n```", "%s")
        for wrapper in wrappers:
            for payload in (broken, html.escape(broken)):
                with self.subTest(wrapper=wrapper, payload=payload):
                    with self.assertRaisesRegex(CandidateError, "记忆候选 JSON 无法解析"):
                        self.parse_block(wrapper % payload)


if __name__ == "__main__":
    unittest.main()
