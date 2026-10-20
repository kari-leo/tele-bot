import json
import subprocess
import unittest
from unittest.mock import patch

from tele_bot.tools import OpenCLISearchTool


class OpenCLISearchToolTests(unittest.TestCase):
    def test_execute_returns_normalized_results(self) -> None:
        tool = OpenCLISearchTool(limit=2)

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "rank": 1,
                        "title": "Result One",
                        "url": "https://example.com/1",
                        "snippet": "first",
                        "displayUrl": "example.com/1",
                    },
                    {
                        "rank": 2,
                        "title": "Result Two",
                        "url": "https://example.com/2",
                        "snippet": "second",
                    },
                    {
                        "rank": 3,
                        "title": "Result Three",
                        "url": "https://example.com/3",
                        "snippet": "third",
                    },
                ]
            ),
            stderr="",
        )

        with patch("tele_bot.tools.opencli_search.subprocess.run", return_value=completed):
            result = tool.execute("test query")

        self.assertEqual(result["query"], "test query")
        self.assertEqual(result["engine"], "duckduckgo")
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "Result One")


if __name__ == "__main__":
    unittest.main()