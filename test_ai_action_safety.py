import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def assigned_set(filename, variable):
    tree = ast.parse((ROOT / filename).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == variable for t in node.targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"{variable} not found")


class AIActionSafetyTests(unittest.TestCase):
    def test_sensitive_main_actions_require_confirmation(self):
        actions = assigned_set('main.py', 'MAIN_AI_CONFIRM')
        for name in ('delete_member_bot', 'delete_file', 'add_force_subscribe', 'remove_force_subscribe', 'set_auto_delete', 'set_welcome', 'broadcast', 'ban_user', 'unban_user'):
            self.assertIn(name, actions)

    def test_sensitive_member_actions_require_confirmation(self):
        actions = assigned_set('network.py', 'NETWORK_AI_CONFIRM')
        for name in ('delete_file', 'add_force_join', 'remove_force_join', 'set_auto_delete', 'set_welcome', 'set_channel', 'broadcast', 'ban_user', 'unban_user'):
            self.assertIn(name, actions)

    def test_all_ai_sources_parse(self):
        for name in ('main.py', 'network.py', 'ai_assistant.py'):
            ast.parse((ROOT / name).read_text())


if __name__ == '__main__':
    unittest.main()
