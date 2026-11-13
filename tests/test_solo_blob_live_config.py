"""Real riding snapshots and normal ticks consume atomically saved settings."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from windows_bot_config import bot_config_path, save_bot_config


class LiveConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='zoffline live bots ')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = bot_config_path(self.directory)
        root = Path(__file__).resolve().parents[1]
        with patch.object(sys, 'path', [str(root / 'protobuf')] + sys.path):
            import solo_blob
        self.module = solo_blob
        self.clock = 1000.0
        timer = patch.object(solo_blob.time, 'monotonic', side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        self.registry = {8000001: object()}
        dictionary = json.loads((root / 'data' / 'game_dictionary.txt').read_text(encoding='utf-8'),
            object_hook=lambda data: {int(key) if key.lstrip('-').isdigit() else key: value
                                      for key, value in data.items()})
        self.manager = solo_blob.SoloBlobManager(str(self.path.parent), lambda: [], dictionary,
            lambda: 1000000, lambda: self.registry,
            lambda riders: setattr(self, 'registry', riders))
        self.position = 0
        self.step(3)

    def step(self, count=1, player_id=1):
        for _ in range(count):
            self.clock += 0.51
            self.position += 500
            state = self.module.udp_node_msgs_pb2.PlayerState(id=player_id,
                x=self.position, z=100, y_altitude=10, roadTime=100000 + self.position,
                roadPosition=10000000, speed=36000000, power=150, worldTime=1000000)
            self.manager.on_state(player_id, state)
            self.manager.tick()

    def save(self, **values):
        return save_bot_config(self.directory, values)

    def published(self):
        return {key: rider for key, rider in self.registry.items()
                if key >= self.module.RIDER_ID_BASE}

    def enable(self, count=3):
        self.save(enabled=True, riders=count)
        self.step()
        self.assertEqual(len(self.published()), count)
        return self.manager.blobs[1]

    def test_lan_group_waits_for_remote_anchor_instead_of_using_solo_profile_or_other_rider(self):
        profile = self.path.parent / '47' / 'profile.bin'
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b'solo profile fixture')
        save_bot_config(self.directory, {'enabled': True, 'riders': 3}, player_id=2)
        self.step(3, player_id=1)
        self.assertEqual(list(self.manager.blobs), [2])
        self.assertFalse(self.published())
        self.step(3, player_id=2)
        blob = self.manager.blobs[2]
        self.assertEqual(len(self.published()), 3)
        self.assertTrue(blob.trail.ready())
        self.assertTrue(all(entry['proto'].id == 2 for entry in blob.trail.entries))
        self.assertIn(8000001, self.registry)

    def test_lan_live_anchor_switch_uses_target_trail_and_removes_old_roster(self):
        self.step(3, player_id=2)
        save_bot_config(self.directory, {'enabled': True, 'riders': 3}, player_id=2)
        self.step(player_id=1)
        old = self.manager.blobs[2]
        old_ids = set(self.published())
        target_trail = self.manager._recent_trails[1]['trail']
        save_bot_config(self.directory, {'riders': 5, 'custom_names': ['跟随新目标']}, player_id=1)
        self.step(player_id=2)
        self.assertEqual(list(self.manager.blobs), [1])
        self.assertEqual(len(self.published()), 5)
        self.assertTrue(old_ids.isdisjoint(self.published()))
        updated = self.manager.blobs[1]
        self.assertIs(updated.trail, target_trail)
        self.assertIsNot(updated.trail, old.trail)
        self.assertTrue(all(entry['proto'].id == 1 for entry in updated.trail.entries))
        self.assertEqual(updated.riders[0].profile.last_name, '跟随新目标')
        self.assertIn(8000001, self.registry)

    def test_lan_remote_anchor_can_be_disabled_and_reenabled_without_local_riding(self):
        save_bot_config(self.directory, {'enabled': True, 'riders': 4}, player_id=2)
        self.step(3, player_id=2)
        trail = self.manager.blobs[2].trail
        self.assertEqual(len(self.published()), 4)
        save_bot_config(self.directory, {'enabled': False}, player_id=2)
        self.step(player_id=2)
        self.assertFalse(self.manager.blobs)
        self.assertEqual(set(self.registry), {8000001})
        save_bot_config(self.directory, {'enabled': True}, player_id=2)
        self.step(player_id=2)
        self.assertIs(self.manager.blobs[2].trail, trail)
        self.assertEqual(len(self.published()), 4)

    def test_riding_enable_disable_and_reenable_use_recent_trail_without_warmup(self):
        blob = self.enable()
        trail = blob.trail
        self.assertTrue(trail.ready())
        self.save(enabled=False)
        self.step()
        self.assertFalse(self.manager.blobs)
        self.assertFalse(self.published())
        self.assertIn(8000001, self.registry)
        self.step(3)
        self.save(enabled=True)
        self.step()
        self.assertIs(self.manager.blobs[1].trail, trail)
        self.assertEqual(len(self.published()), 3)

    def test_rename_and_resize_replace_profile_ids_and_clear_old_registry_entries(self):
        blob = self.enable()
        original_ids = set(self.published())
        trail = blob.trail
        original_samples = len(trail.entries)
        self.save(riders=5, custom_names=['小明', '', 'Team Rider'], scatter=False)
        self.step()
        updated = self.manager.blobs[1]
        self.assertIs(updated.trail, trail)
        self.assertGreater(len(trail.entries), original_samples)
        self.assertEqual(len(self.published()), 5)
        self.assertTrue(original_ids.isdisjoint(self.published()))
        self.assertEqual(updated.riders[0].profile.last_name, '小明')
        self.assertEqual(updated.riders[2].profile.last_name, 'Team Rider')
        self.assertTrue(all(r.route.states[0].roadTime for r in self.published().values()))

    def test_layout_and_timing_updates_keep_ids_equipment_and_continuous_motion(self):
        blob = self.enable()
        profiles = {r.id: r.profile.SerializeToString() for r in blob.riders}
        trail = blob.trail
        blob.grade_now = 0.02
        self.save(scatter=False, row_gap=2.4, shuffle_interval=9, pull_interval=15,
                  rotate=False, bot_gestures=False)
        self.step()
        updated = self.manager.blobs[1]
        self.assertEqual({r.id: r.profile.SerializeToString() for r in updated.riders}, profiles)
        self.assertIs(updated.trail, trail)
        self.assertFalse(updated.config['scatter'])
        self.assertFalse(updated.config['rotate'])
        self.assertFalse(updated.config['bot_gestures'])
        self.assertEqual(updated.config['row_gap'], 2.4)
        self.assertLess(updated.next_shuffle, self.clock + 20)
        self.assertGreater(updated.grade_now, 0)

    def test_same_values_do_not_rebuild_or_reset_the_roster(self):
        blob = self.enable()
        riders = list(blob.riders)
        next_pull = blob.next_pull
        config = json.loads(self.path.read_text(encoding='ascii'))
        self.save(**config)
        self.step()
        self.assertIs(self.manager.blobs[1], blob)
        self.assertEqual(self.manager.blobs[1].riders, riders)
        self.assertEqual(blob.next_pull, next_pull)

    def test_rapid_same_size_atomic_replacements_apply_the_final_name(self):
        blob = self.enable()
        trail = blob.trail
        first = self.save(custom_names=['Name A'])
        self.save(custom_names=['Name B'])
        final = self.save(custom_names=['Name C'])
        self.assertEqual(len(json.dumps(first)), len(json.dumps(final)))
        self.step()
        self.assertEqual(self.manager.blobs[1].riders[0].profile.last_name, 'Name C')
        self.assertIs(self.manager.blobs[1].trail, trail)
        self.assertEqual(len(self.published()), 3)

    def test_bad_json_and_failed_construction_keep_the_existing_roster_until_fixed(self):
        blob = self.enable()
        ids = set(self.published())
        good = self.path.read_bytes()
        self.path.write_text('{bad json', encoding='ascii')
        with self.assertLogs('solo_blob', level='WARNING'):
            self.step()
        self.assertIs(self.manager.blobs[1], blob)
        self.assertEqual(set(self.published()), ids)
        raw = json.loads(good)
        raw['pull_interval'] = 'bad interval'
        self.path.write_text(json.dumps(raw), encoding='ascii')
        with self.assertLogs('solo_blob', level='WARNING'):
            self.step()
        self.assertIs(self.manager.blobs[1], blob)
        self.assertEqual(set(self.published()), ids)
        self.path.write_bytes(good)
        self.save(custom_names=['Fixed'])
        self.step()
        self.assertEqual(self.manager.blobs[1].riders[0].profile.last_name, 'Fixed')
        self.assertEqual(len(self.published()), 3)

    def test_invalid_dormant_numeric_setting_is_rejected_with_an_expired_trail(self):
        blob = self.enable()
        ids = set(self.published())
        self.clock += self.module.TRAIL_STALE_SECONDS + 1
        self.manager.tick()
        self.assertFalse(blob.trail.ready())
        self.assertFalse(self.published())
        raw = json.loads(self.path.read_text(encoding='ascii'))
        raw['max_drop'] = 'bad drop'
        self.path.write_text(json.dumps(raw), encoding='ascii')
        self.clock += 0.51
        with self.assertLogs('solo_blob', level='WARNING') as messages:
            self.manager.tick()
        self.assertIn('max_drop', '\n'.join(messages.output))
        self.assertIs(self.manager.blobs[1], blob)
        self.step(2)
        self.assertEqual(set(self.published()), ids)
        self.assertEqual(blob.config['max_drop'], self.module.DEFAULT_CONFIG['max_drop'])

    def test_invalid_delayed_turn_setting_is_rejected_before_the_turn_begins(self):
        blob = self.enable()
        ids = set(self.published())
        raw = json.loads(self.path.read_text(encoding='ascii'))
        raw.update(turn_interval=0.1, lead_time='invalid')
        self.path.write_text(json.dumps(raw), encoding='ascii')
        with self.assertLogs('solo_blob', level='WARNING') as messages:
            self.step()
        self.assertIn('lead_time', '\n'.join(messages.output))
        self.assertIs(self.manager.blobs[1], blob)
        self.step(3)
        self.assertEqual(set(self.published()), ids)
        self.assertIsNone(blob.mode)

    def test_explicit_disable_ignores_unused_invalid_engine_settings(self):
        blob = self.enable()
        trail = blob.trail
        good = json.loads(self.path.read_text(encoding='ascii'))
        raw = dict(good, enabled=False, max_drop='bad', riders='bad', player_id='bad')
        self.path.write_text(json.dumps(raw), encoding='ascii')
        self.step()
        self.assertFalse(self.manager.blobs)
        self.assertEqual(set(self.registry), {8000001})
        raw['enabled'] = True
        self.path.write_text(json.dumps(raw), encoding='ascii')
        with self.assertLogs('solo_blob', level='WARNING'):
            self.step()
        self.assertFalse(self.manager.blobs)
        self.assertEqual(set(self.registry), {8000001})
        self.path.write_text(json.dumps(good), encoding='ascii')
        self.step()
        self.assertIs(self.manager.blobs[1].trail, trail)
        self.assertEqual(len(self.published()), 3)

    def test_nonfinite_manual_settings_keep_the_active_roster(self):
        blob = self.enable()
        ids = set(self.published())
        good = json.loads(self.path.read_text(encoding='ascii'))
        for field in ('max_drop', 'wander_gap', 'lead_time', 'latency_comp'):
            for value in (float('nan'), float('inf'), float('-inf'), 'NaN', 'Infinity', '-Infinity'):
                with self.subTest(field=field, value=value):
                    self.path.write_text(json.dumps(dict(good, **{field: value})), encoding='ascii')
                    with self.assertLogs('solo_blob', level='WARNING') as messages:
                        self.step()
                    self.assertIn(field, '\n'.join(messages.output))
                    self.assertIs(self.manager.blobs[1], blob)
                    self.assertEqual(set(self.published()), ids)

    def test_manual_integer_strings_must_match_the_engine_integer_conversion(self):
        blob = self.enable()
        ids = set(self.published())
        good = json.loads(self.path.read_text(encoding='ascii'))
        for field, value in (('riders', '3.0'), ('columns', '2.0'),
                             ('lead_spread', '1.0'), ('player_id', '1.0')):
            with self.subTest(field=field):
                self.path.write_text(json.dumps(dict(good, **{field: value})), encoding='ascii')
                with self.assertLogs('solo_blob', level='WARNING') as messages:
                    self.step()
                self.assertIn(field, '\n'.join(messages.output))
                self.assertIs(self.manager.blobs[1], blob)
                self.step()
                self.assertEqual(set(self.published()), ids)

    def test_finite_advanced_manual_values_and_auto_latency_remain_supported(self):
        self.enable()
        ids = set(self.published())
        manual = {'row_gap': 12, 'scatter_width': 8, 'shuffle_interval': 0,
                  'pull_interval': '120.5', 'columns': 2.5, 'lead_spread': 1.5,
                  'road_axis_side': 0.5, 'latency_comp': 'auto',
                  'extension': {'text': 'unchanged'}}
        raw = json.loads(self.path.read_text(encoding='ascii'))
        self.path.write_text(json.dumps(dict(raw, **manual)), encoding='ascii')
        self.step(2)
        self.assertEqual(set(self.published()), ids)
        for field, value in manual.items():
            self.assertEqual(self.manager.blobs[1].config[field], value)

    def test_one_permission_error_retries_the_same_saved_file_on_the_next_tick(self):
        blob = self.enable()
        ids = set(self.published())
        old_signature = self.manager.config_mtime
        self.save(custom_names=['Recovered'])
        saved_signature = self.manager._config_mtime()
        with patch.object(self.module, 'open', side_effect=PermissionError('temporarily busy'),
                          create=True) as config_open, self.assertLogs('solo_blob', level='WARNING'):
            self.step()
        config_open.assert_called_once()
        self.assertIs(self.manager.blobs[1], blob)
        self.assertEqual(set(self.published()), ids)
        self.assertEqual(self.manager.config_mtime, old_signature)
        self.step()
        self.assertEqual(self.manager._config_mtime(), saved_signature)
        self.assertEqual(self.manager.config_mtime, saved_signature)
        self.assertEqual(self.manager.blobs[1].riders[0].profile.last_name, 'Recovered')
        self.assertTrue(ids.isdisjoint(self.published()))
        self.assertEqual(len(self.published()), 3)

    def test_persistent_permission_error_keeps_retrying_without_repeating_the_warning(self):
        blob = self.enable()
        ids = set(self.published())
        self.save(custom_names=['Recovered'])
        with patch.object(self.module, 'open', side_effect=PermissionError('temporarily busy'),
                          create=True) as config_open, self.assertLogs('solo_blob', level='WARNING') as messages:
            self.step(3)
        self.assertEqual(config_open.call_count, 3)
        self.assertEqual(len(messages.output), 1)
        self.assertIs(self.manager.blobs[1], blob)
        self.assertEqual(set(self.published()), ids)
        self.step()
        self.assertEqual(self.manager.blobs[1].riders[0].profile.last_name, 'Recovered')
        self.assertEqual(len(self.published()), 3)

    def test_deleted_config_removes_riders_while_preserving_foreign_registry(self):
        self.enable()
        self.path.unlink()
        self.step()
        self.assertFalse(self.published())
        self.assertEqual(set(self.registry), {8000001})

    def test_state_cache_is_bounded_expires_and_does_not_push_active_states_twice(self):
        blob = self.enable()
        with patch.object(blob.trail, 'push', wraps=blob.trail.push) as push:
            self.step()
        push.assert_called_once()
        with patch.object(self.module, 'MAX_RECENT_TRAIL_SAMPLES', 4):
            self.step(6)
        self.assertLessEqual(len(blob.trail.entries), 4)
        self.save(enabled=False)
        self.step()
        for player in range(2, self.module.MAX_RECENT_PLAYERS + 10):
            self.step(player_id=player)
        self.assertLessEqual(len(self.manager._recent_trails), self.module.MAX_RECENT_PLAYERS)
        self.clock += self.module.TRAIL_STALE_SECONDS + 1
        self.manager.tick()
        self.assertFalse(self.manager._recent_trails)
        self.save(enabled=True)
        self.clock += 0.51
        self.manager.tick()
        self.assertFalse(self.published())


if __name__ == '__main__':
    unittest.main()
