"""Exercise equipment permissions and bike progress through the real API handlers."""

import ast
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from flask import Flask, request
from protobuf import profile_pb2, user_storage_pb2


ROOT = Path(__file__).resolve().parents[1]
GD = json.loads((ROOT / 'data' / 'game_dictionary.txt').read_text(encoding='utf-8'))
SOURCE = ROOT / 'zwift_offline.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
HANDLERS = {'update_entitlements', 'update_bike_progress',
            'api_player_profile_user_game_storage_attributes'}


class EquipmentUnlockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='zoffline equipment ')
        self.addCleanup(self.temporary.cleanup)
        self.storage = Path(self.temporary.name)
        self.player = self.storage / '1'
        self.player.mkdir()
        self.marker = self.storage / 'unlock_all_equipment.txt'
        self.marker.touch()
        self.profile = profile_pb2.PlayerProfile(
            id=1, achievement_level=355, total_xp=2050, total_gold_drops=142285,
            run_achievement_level=200, total_run_experience_points=1000,
            total_distance_in_meters=35559)
        self.profile_file = self.player / 'profile.bin'
        self.profile_bytes = self.profile.SerializeToString()
        self.profile_file.write_bytes(self.profile_bytes)
        self.user_storage = user_storage_pb2.UserStorage()
        self.user_storage.attributes.add().game_settings.minimal_leaderboards = 2
        bike = self.user_storage.attributes.add().bike_progress
        bike.signature = '1029279076'
        bike.total_distance = 35559.75
        bike.level_elevation = 391.5
        bike.time = 123
        bike.bike_upgrades.add(level=1, price=25000, time=100)
        self.file = self.player / 'user_storage.bin'
        self.file.write_bytes(self.user_storage.SerializeToString())
        self.app = Flask(__name__)
        self.namespace = {
            'app': self.app, 'request': request, 'os': os, 'time': time,
            'current_user': SimpleNamespace(player_id=1),
            'jwt_to_session_cookie': lambda f: f, 'login_required': lambda f: f,
            'user_storage_pb2': user_storage_pb2, 'profile_pb2': profile_pb2,
            'STORAGE_DIR': str(self.storage), 'SCRIPT_DIR': str(ROOT),
            'UNLOCK_ALL_EQUIPMENT_FILE': str(self.marker), 'MAX_BIKE_LEVEL': 5,
            'GD': GD, 'json': json,
        }
        functions = [node for node in TREE.body
                     if isinstance(node, ast.FunctionDef) and node.name in HANDLERS]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), 'exec'),
             self.namespace)
        self.client = self.app.test_client()

    def tearDown(self):
        # Every API scenario must leave the complete rider profile unchanged.
        self.assertEqual(self.profile_file.read_bytes(), self.profile_bytes)

    def get_bikes(self):
        response = self.client.get('/api/player-profile/user-game-storage/attributes?n=24')
        self.assertEqual(response.status_code, 200)
        result = user_storage_pb2.UserStorage.FromString(response.data)
        self.assertTrue(all(a.HasField('bike_progress') for a in result.attributes))
        return {a.bike_progress.signature: a.bike_progress for a in result.attributes}

    def post(self, payload):
        response = self.client.post('/api/player-profile/user-game-storage/attributes',
                                    data=payload.SerializeToString())
        self.assertEqual(response.status_code, 202)
        return user_storage_pb2.UserStorage.FromString(self.file.read_bytes())

    def test_login_returns_every_frame_at_level_five_including_brompton(self):
        before = self.file.read_bytes()
        bikes = self.get_bikes()
        self.assertEqual(set(bikes), {str(s) for s in GD['bike_upgrade_frames']})
        self.assertTrue(all(b.level == 5 and b.percentage == 1 for b in bikes.values()))
        self.assertIn('3017804836', bikes)
        self.assertEqual(bikes['1029279076'].total_distance, 35559.75)
        self.assertEqual(bikes['1029279076'].level_elevation, 391.5)
        self.assertEqual(len(bikes['1029279076'].bike_upgrades), 1)
        self.assertEqual(self.file.read_bytes(), before)

    def test_new_player_without_storage_gets_maximum_bike_levels(self):
        self.file.unlink()
        self.assertEqual(len(self.get_bikes()), len(GD['bike_upgrade_frames']))
        self.assertFalse(self.file.exists())

    def test_settings_request_returns_only_requested_attributes(self):
        before = self.file.read_bytes()
        response = self.client.get('/api/player-profile/user-game-storage/attributes?n=22&n=29')
        result = user_storage_pb2.UserStorage.FromString(response.data)
        self.assertEqual(len(result.attributes), 1)
        self.assertEqual(result.attributes[0], self.user_storage.attributes[0])
        self.assertEqual(self.file.read_bytes(), before)

    def test_stale_client_save_cannot_downgrade_bikes_or_lose_statistics(self):
        update = user_storage_pb2.UserStorage()
        bike = update.attributes.add().bike_progress
        bike.signature = '1029279076'
        bike.level = 0
        bike.total_distance = 40000
        bike.time = 456
        # Frames added by a newer client are also upgraded on save.
        update.attributes.add().bike_progress.signature = '4294967295'
        saved = self.post(update)
        bikes = {a.bike_progress.signature: a.bike_progress for a in saved.attributes
                 if a.HasField('bike_progress')}
        self.assertEqual(len(bikes), len(GD['bike_upgrade_frames']) + 1)
        self.assertTrue(all(b.level == 5 for b in bikes.values()))
        self.assertEqual(bikes['1029279076'].total_distance, 40000)
        self.assertEqual(saved.attributes[0], self.user_storage.attributes[0])
        self.assertEqual(self.get_bikes()['4294967295'].level, 5)

    def test_unrelated_save_preserves_bike_data_and_other_player(self):
        other = self.storage / '2'
        other.mkdir()
        other_file = other / 'user_storage.bin'
        other_file.write_bytes(b'other player data')
        update = user_storage_pb2.UserStorage()
        update.attributes.add().bike_paint_color.signature = '3017804836'
        saved = self.post(update)
        self.assertEqual(other_file.read_bytes(), b'other player data')
        self.assertEqual(sum(a.HasField('bike_paint_color') for a in saved.attributes), 1)
        bikes = {a.bike_progress.signature: a.bike_progress for a in saved.attributes
                 if a.HasField('bike_progress')}
        self.assertEqual(bikes['1029279076'].total_distance, 35559.75)
        self.assertEqual(len(bikes['1029279076'].bike_upgrades), 1)

    def test_repeated_updates_preserve_timestamps_and_do_not_duplicate_frames(self):
        update = self.namespace['update_bike_progress']
        update(self.user_storage)
        before = self.user_storage.SerializeToString()
        update(self.user_storage)
        self.assertEqual(self.user_storage.SerializeToString(), before)

    def test_marker_removed_preserves_normal_bike_progress(self):
        self.marker.unlink()
        bikes = self.get_bikes()
        self.assertEqual(set(bikes), {'1029279076'})
        self.assertEqual(bikes['1029279076'].level, 0)
        saved = self.post(user_storage_pb2.UserStorage())
        self.assertEqual(saved, self.user_storage)

    def test_all_equipment_permissions_are_active_without_changing_rider_data(self):
        self.profile.entitlements.add(type=4, id=1, status=1)
        update = self.namespace['update_entitlements']
        update(self.profile)
        entitlements = {e.id: e for e in self.profile.entitlements if e.type == 4}
        expected = json.loads((ROOT / 'data' / 'entitlements.txt').read_text())['last']
        self.assertEqual(set(entitlements), set(range(1, expected + 1)))
        self.assertTrue(all(e.status == 2 for e in entitlements.values()))
        before = sorted(e.SerializeToString() for e in self.profile.entitlements)
        update(self.profile)
        self.assertEqual(sorted(e.SerializeToString() for e in self.profile.entitlements), before)
        self.profile.ClearField('entitlements')
        self.assertEqual(self.profile.SerializeToString(), self.profile_bytes)

    def test_complete_frame_catalog_preserves_bot_equipment_exclusions(self):
        self.assertEqual(len(GD['bike_upgrade_frames']), len(set(GD['bike_upgrade_frames'])))
        self.assertTrue(set(map(int, GD['bikeframes'])).issubset(GD['bike_upgrade_frames']))
        self.assertNotIn('3017804836', GD['bikeframes'])
        self.assertIn(3017804836, GD['bike_upgrade_frames'])


if __name__ == '__main__':
    unittest.main()
