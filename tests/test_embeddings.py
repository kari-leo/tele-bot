import unittest
from unittest.mock import Mock

import httpx

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.llm.embeddings import (
    AliBailianEmbeddingClient,
    EmbeddingAuthenticationError,
)


def settings(**overrides):
    values = dict(
        api_key="chat", base_url="https://chat.test/v1", model="qwen-plus",
        reasoning_model="qwen", timeout_seconds=60, embedding_api_key="embed",
        embedding_base_url="https://embed.test/v1", embedding_dimensions=3,
        embedding_batch_size=2,
    )
    values.update(overrides)
    return AliBailianSettings(**values)


class EmbeddingClientTests(unittest.TestCase):
    def test_batches_and_preserves_response_order_and_usage(self) -> None:
        post = Mock()
        responses = []
        for count, tokens in ((2, 5), (1, 2)):
            response = Mock(spec=httpx.Response)
            response.status_code = 200
            response.is_success = True
            response.headers = {"x-request-id": f"req-{count}"}
            response.json.return_value = {
                "model": "text-embedding-v4",
                "data": [
                    {"index": index, "embedding": [float(index), 1.0, 2.0]}
                    for index in reversed(range(count))
                ],
                "usage": {"prompt_tokens": tokens},
            }
            responses.append(response)
        post.side_effect = responses
        batches = AliBailianEmbeddingClient(settings(), post=post).embed(["a", "b", "c"])
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].input_tokens, 5)
        self.assertEqual(batches[0].vectors[0], (0.0, 1.0, 2.0))
        self.assertEqual(post.call_args_list[0].kwargs["json"]["dimensions"], 3)

    def test_authentication_error_is_classified_without_retry(self) -> None:
        response = Mock(spec=httpx.Response)
        response.status_code = 401
        response.is_success = False
        post = Mock(return_value=response)
        with self.assertRaises(EmbeddingAuthenticationError):
            AliBailianEmbeddingClient(settings(), post=post).embed(["a"])
        self.assertEqual(post.call_count, 1)

    def test_rejects_empty_or_oversized_input_before_request(self) -> None:
        client = AliBailianEmbeddingClient(settings(), post=Mock(), max_input_chars=3)
        with self.assertRaises(ValueError):
            client.embed([" "])
        with self.assertRaises(ValueError):
            client.embed(["long"])


if __name__ == "__main__":
    unittest.main()
