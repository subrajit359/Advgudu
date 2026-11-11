import json
import unittest
from unittest.mock import patch

import ai_assistant


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeClient:
    content = '{}'
    last_payload = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).last_payload = json
        return FakeResponse(type(self).content)


class AIAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_is_reported(self):
        with patch.object(ai_assistant, 'AI_API_KEY', ''):
            with self.assertRaisesRegex(RuntimeError, 'AI_NOT_CONFIGURED'):
                await ai_assistant.create_ai_plan('hello', [], {}, 'user')

    async def test_allowed_action_is_returned(self):
        FakeClient.content = json.dumps({"reply": "I can show the stats.", "action": {"name": "get_stats", "args": {}}})
        with patch.object(ai_assistant, 'AI_API_KEY', 'test-key'), patch.object(ai_assistant.httpx, 'AsyncClient', FakeClient):
            plan = await ai_assistant.create_ai_plan('show stats', [], {"get_stats": "Show totals"}, 'owner')
        self.assertEqual(plan['action']['name'], 'get_stats')

    async def test_unknown_action_is_discarded(self):
        FakeClient.content = json.dumps({"reply": "Okay", "action": {"name": "delete_everything", "args": {}}})
        with patch.object(ai_assistant, 'AI_API_KEY', 'test-key'), patch.object(ai_assistant.httpx, 'AsyncClient', FakeClient):
            plan = await ai_assistant.create_ai_plan('do it', [], {"get_stats": "Show totals"}, 'user')
        self.assertIsNone(plan['action'])

    async def test_prompt_contains_private_detail_guard(self):
        FakeClient.content = json.dumps({"reply": "Hello", "action": None})
        with patch.object(ai_assistant, 'AI_API_KEY', 'test-key'), patch.object(ai_assistant.httpx, 'AsyncClient', FakeClient):
            await ai_assistant.create_ai_plan('hello', [], {}, 'user')
        system = FakeClient.last_payload['messages'][0]['content']
        self.assertIn('Do not disclose private implementation details', system)

    def test_rate_limit(self):
        data = {}
        self.assertFalse(ai_assistant.rate_limited(data, limit=2, window=60))
        self.assertFalse(ai_assistant.rate_limited(data, limit=2, window=60))
        self.assertTrue(ai_assistant.rate_limited(data, limit=2, window=60))


    def test_default_provider_configuration_is_groq(self):
        self.assertEqual(ai_assistant.AI_BASE_URL, "https://api.groq.com/openai/v1")
        self.assertEqual(ai_assistant.AI_MODEL, "openai/gpt-oss-120b")


if __name__ == '__main__':
    unittest.main()

# Long-session memory tests
class AISessionMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_is_returned_from_provider(self):
        FakeClient.content = json.dumps({"reply": "User prefers Hinglish and is discussing bot setup.", "action": None})
        with patch.object(ai_assistant, 'AI_API_KEY', 'test-key'), patch.object(ai_assistant.httpx, 'AsyncClient', FakeClient):
            summary = await ai_assistant.summarize_ai_history([
                {"role": "user", "content": "Hinglish mein bolo"},
                {"role": "assistant", "content": "Bilkul!"},
            ])
        self.assertIn("Hinglish", summary)

    async def test_plan_accepts_session_summary(self):
        FakeClient.content = json.dumps({"reply": "Bilkul, Hinglish mein baat karte hain.", "action": None})
        with patch.object(ai_assistant, 'AI_API_KEY', 'test-key'), patch.object(ai_assistant.httpx, 'AsyncClient', FakeClient):
            await ai_assistant.create_ai_plan(
                'Continue', [], {}, 'user',
                'User prefers Hinglish.'
            )
        system = FakeClient.last_payload['messages'][0]['content']
        self.assertIn('Session memory summary', system)
        self.assertIn('User prefers Hinglish', system)
