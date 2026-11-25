"""Hardware-free regressions for recording, recovery and lifecycle failures."""
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from jetson.recorder import Recorder
from utility.egocentric_camera import EgocentricCameraCollector, FRAME_DTYPE
from utility.imu import IMUCollector, LOWSTATE_DTYPE
from utility.lidar import LidarCollector
from scripts.finalize_session import finalize, finalize_ego_cam, finalize_lidar

# No test may ask this computer's chrony or write the real clock state in /dev/shm (jetson/clock.py).
from jetson import clock as _clock_module
_REAL_CHRONY_SYNCED = _clock_module.chrony_synced        # tested on its own with a fake chronyc
_clock_tmp = tempfile.TemporaryDirectory()
_clock_patches = []


def setUpModule():
    from jetson import clock
    _clock_patches.extend([patch.object(clock, 'STATE_PATH', Path(_clock_tmp.name) / 'clock.json'),
                           patch.object(clock, 'chrony_synced', return_value=False)])
    for p in _clock_patches:
        p.start()


def tearDownModule():
    for p in _clock_patches:
        p.stop()
    _clock_tmp.cleanup()


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def recorder(self):
        recorder = Recorder(data_root=str(self.root), mock=True)
        recorder.ego = EgocentricCameraCollector(width=64, height=48, mock=True)
        return recorder

    def test_mock_roundtrip_and_repeated_sessions(self):
        recorder = self.recorder()
        with patch('jetson.recorder.START_DELAY_S', 0):
            for _ in range(2):
                path = recorder.start('task1')
                time.sleep(0.15)
                result = recorder.stop()
                self.assertGreater(result['samples']['imu'], 0)
                self.assertGreater(result['samples']['ego_frames'], 0)
                self.assertTrue(json.loads((path / 'session_meta.json').read_text())['clean_stop'])
                finalize(path, delete_raw=True)
                self.assertFalse((path / 'lowstate.bin').exists())
                with np.load(path / 'ego_cam/depth.npz') as data:
                    self.assertEqual(len(data['depth']), result['samples']['ego_frames'])
                    self.assertEqual(data['depth'].shape[1:], (48, 64))
                self.assertEqual(recorder.state, 'idle')

    def test_pause_keeps_no_data_and_is_left_out_of_elapsed(self):
        recorder = self.recorder()
        with patch('jetson.recorder.START_DELAY_S', 0):
            t0 = time.time()
            path = recorder.start('task1')
            time.sleep(0.4)
            recorder.pause()
            self.assertTrue(recorder.paused)
            frozen = recorder.elapsed
            time.sleep(0.5)
            self.assertAlmostEqual(recorder.elapsed, frozen, places=6)
            recorder.resume()
            self.assertFalse(recorder.paused)
            time.sleep(0.4)
            wall = time.time() - t0          # elapsed is taken when stop() begins
            result = recorder.stop()
        meta = json.loads((path / 'session_meta.json').read_text())
        self.assertEqual(len(meta['pauses']), 1)
        a, b = meta['pauses'][0]
        self.assertGreater(b - a, 0.45)
        self.assertLess(abs(meta['elapsed_seconds'] - (wall - (b - a))), 0.15)
        self.assertEqual(result['elapsed_seconds'], meta['elapsed_seconds'])
        streams = {
            'imu': np.fromfile(path / 'lowstate.bin', dtype=LOWSTATE_DTYPE)['timestamp'],
            'camera': np.fromfile(path / 'ego_cam/frames.bin', dtype=FRAME_DTYPE)['timestamp'],
            'lidar': np.fromfile(path / 'lidar_timestamps.bin', dtype='<f8'),
        }
        for name, ts in streams.items():
            with self.subTest(stream=name):
                self.assertTrue((ts < a).any() and (ts > b).any(), "data before and after the pause")
                self.assertFalse(((ts > a + 0.05) & (ts < b - 0.05)).any(), "nothing kept during the pause")
        # collector rates leave the pause out (mock camera: 30 fps)
        self.assertGreater(meta['collectors']['ego']['actual_fps'], 24)
        # the video plays at the camera rate, the pause cut out
        finalize(path, delete_raw=False)
        cap = cv2.VideoCapture(str(path / 'ego_cam/rgb.mp4'))
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        self.assertGreater(fps, 24)

    def test_pause_rules(self):
        from jetson.recorder import RecorderStateError
        recorder = self.recorder()
        with self.assertRaises(RecorderStateError):
            recorder.pause()                                  # idle
        with patch('jetson.recorder.START_DELAY_S', 5):
            recorder.start('task1')
            with self.assertRaises(RecorderStateError):
                recorder.pause()                              # data not kept yet
            recorder.stop()
        with patch('jetson.recorder.START_DELAY_S', 0):
            path = recorder.start('task1')
            time.sleep(0.2)
            with self.assertRaises(RecorderStateError):
                recorder.resume()                             # not paused
            recorder.pause()
            with self.assertRaises(RecorderStateError):
                recorder.pause()                              # already paused
            time.sleep(0.3)
            recorder.stop()                                   # stopping while paused is allowed
        meta = json.loads((path / 'session_meta.json').read_text())
        self.assertEqual(len(meta['pauses']), 1)
        self.assertTrue(meta['clean_stop'])
        self.assertLess(meta['elapsed_seconds'], 0.3)
        self.assertEqual(recorder.state, 'idle')
        self.assertFalse(recorder.paused)

    def test_missing_drivers_fail_instead_of_recording_synthetic_data(self):
        for module, name, cls in [('utility.imu', 'SDK_AVAILABLE', IMUCollector),
                                  ('utility.lidar', 'SDK_AVAILABLE', LidarCollector),
                                  ('utility.egocentric_camera', 'REALSENSE_AVAILABLE', EgocentricCameraCollector)]:
            with self.subTest(module=module), patch(f'{module}.{name}', False):
                with self.assertRaisesRegex(RuntimeError, 'mock mode must be explicit'):
                    cls().start(self.root)

    def test_bad_paths_rejected_before_creating_files(self):
        r = self.recorder()
        for kwargs in [{'session_date': '../outside'}, {'session_id': '/tmp/escape'},
                       {'session_date': '02_30_2026'}, {'session_id': '25_00_00'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                r.start('task1', **kwargs)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_session_collision_does_not_rename(self):
        r = self.recorder()
        args = dict(task='task1', session_date='10_08_2026', session_id='12_00_00')
        r.start(**args)
        r.stop()
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            r.start(**args)

    def test_metadata_failure_returns_to_idle(self):
        r = self.recorder()
        with patch.object(r, '_write_meta', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                r.start('task1')
        self.assertEqual(r.state, 'idle')

    def test_stop_error_is_not_clean_stop(self):
        r = self.recorder()
        path = r.start('task1')
        original = r.imu.stop
        def fail():
            original()
            raise OSError('disk full')
        with patch.object(r.imu, 'stop', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'did not save cleanly'):
                r.stop()
        self.assertFalse(json.loads((path / 'session_meta.json').read_text())['clean_stop'])

    def test_stop_waits_for_start_to_finish(self):
        r = self.recorder()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        original = r.ego.start
        def slow(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            original(*args)
        errors = []
        def start():
            try:
                r.start('task1')
            except Exception as exc:
                errors.append(exc)
        def stop():
            try:
                r.stop()
            except Exception as exc:
                errors.append(exc)
            stopped.set()
        with patch.object(r.ego, 'start', side_effect=slow):
            a = threading.Thread(target=start)
            a.start()
            self.assertTrue(entered.wait(2))
            b = threading.Thread(target=stop)
            b.start()
            self.assertFalse(stopped.wait(0.05))
            release.set()
            a.join(5)
            b.join(5)
        self.assertFalse(errors)
        self.assertTrue(stopped.is_set())
        self.assertFalse(any(c._running for c in (r.imu, r.ego, r.lidar)))

    def test_disk_thread_error_surfaces_and_changes_health(self):
        r = self.recorder()
        with patch('jetson.recorder.START_DELAY_S', 0):
            path = r.start('task1')
        r.ego._error = OSError('disk full')
        self.assertEqual(r.live_hz()['cam'], 0)
        with self.assertRaisesRegex(RuntimeError, 'disk full'):
            r.stop()
        self.assertFalse(json.loads((path / 'session_meta.json').read_text())['clean_stop'])

    def test_writer_exception_is_propagated_from_background_thread(self):
        camera = EgocentricCameraCollector(width=64, height=48, mock=True)
        camera.start(self.root, record_from=time.time() + 100)
        failed = threading.Event()
        def fail_write(data):
            failed.set()
            raise OSError("injected disk failure")
        with patch.object(camera._rgb_file, 'write', side_effect=fail_write):
            camera._queue.put((time.time(), np.zeros((48,64,3), np.uint8), np.zeros((48,64), np.uint16)))
            self.assertTrue(failed.wait(2))
            with self.assertRaisesRegex(RuntimeError, 'injected disk failure'):
                camera.stop()
        self.assertIsNone(camera._rgb_file)

    def test_camera_partial_start_closes_pipeline(self):
        camera = EgocentricCameraCollector(mock=True)
        with patch('utility.egocentric_camera.AppendFile', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                camera.start(self.root)
        self.assertFalse(camera._running)
        self.assertIsNone(camera._rgb_file)

    def camera_files(self):
        cam = self.root / 'ego_cam'
        cam.mkdir()
        (cam / 'intrinsics.json').write_text(json.dumps({'color': {'width': 64, 'height': 48}}))
        ok, jpg = cv2.imencode('.jpg', np.zeros((48, 64, 3), np.uint8))
        self.assertTrue(ok)
        jpeg = jpg.tobytes()
        (cam / 'rgb.mjpeg').write_bytes(jpeg * 3)
        (cam / 'depth.raw').write_bytes(bytes(64 * 48 * 2 * 3))
        frames = np.array([(1 + i/30, i*len(jpeg), len(jpeg)) for i in range(3)], dtype=FRAME_DTYPE)
        return cam, frames

    def test_recovery_uses_complete_prefix_not_count(self):
        cam, frames = self.camera_files()
        frames[1]['rgb_size'] = 999999
        frames.tofile(cam / 'frames.bin')
        finalize_ego_cam(self.root)
        self.assertEqual(len(np.load(cam / 'rgb_timestamps.npy')), 1)

    def test_invalid_jpeg_preserves_raw_and_existing_output(self):
        cam, frames = self.camera_files()
        frames.tofile(cam / 'frames.bin')
        (cam / 'rgb.mjpeg').write_bytes(bytes((cam / 'rgb.mjpeg').stat().st_size))
        (cam / 'rgb.mp4').write_bytes(b'previous final output')
        with self.assertRaises(ValueError):
            finalize(self.root, delete_raw=True)
        self.assertTrue((cam / 'frames.bin').exists())
        self.assertEqual((cam / 'rgb.mp4').read_bytes(), b'previous final output')

    def test_encoder_failure_preserves_raw(self):
        cam, frames = self.camera_files()
        frames.tofile(cam / 'frames.bin')
        with patch('scripts.finalize_session.cv2.VideoWriter') as writer:
            writer.return_value.isOpened.return_value = False
            with self.assertRaises(RuntimeError):
                finalize(self.root, delete_raw=True)
        self.assertTrue((cam / 'frames.bin').exists())

    def test_lidar_missing_middle_scan_is_not_silently_reindexed(self):
        (self.root / 'lidar').mkdir()
        np.array([1,2,3], '<f8').tofile(self.root / 'lidar_timestamps.bin')
        for i in [0,2]:
            np.save(self.root / 'lidar' / f'{i:06d}.npy', np.zeros((2,4), np.float32))
        with self.assertRaisesRegex(ValueError, 'Missing LiDAR scan'):
            finalize_lidar(self.root)

    def test_cloud_big_endian_and_padded_rows(self):
        fields = [SimpleNamespace(name=n, datatype=7, count=1, offset=i*4) for i,n in enumerate('xyz')]
        raw = np.array([1,2,3], '>f4').tobytes() + b'pad!'
        raw += np.array([4,5,6], '>f4').tobytes() + b'pad!'
        msg = SimpleNamespace(width=1, height=2, point_step=12, row_step=16,
                              fields=fields, is_bigendian=True, data=raw)
        parsed = LidarCollector(mock=True)._parse_cloud(msg)
        np.testing.assert_array_equal(parsed, [[1,2,3,0],[4,5,6,0]])


@unittest.skipUnless(importlib.util.find_spec('fastapi'), 'FastAPI is not installed in this interpreter')
class ApiTests(unittest.TestCase):
    def test_session_id_must_identify_one_session_not_its_parent(self):
        from jetson import export_api as api
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / 'task1/10_08_2026/12_00_00'
            (session / 'first_person').mkdir(parents=True)
            api.configure(tmp, lambda: None)
            for bad in ['task1', 'task1/10_08_2026', '.', '../']:
                with self.subTest(bad=bad), self.assertRaises(HTTPException):
                    api._session_dir(bad)
            self.assertEqual(api._session_dir('task1/10_08_2026/12_00_00'), session)

    def test_resync_rejects_active_recording_without_running_commands(self):
        from jetson import record_daemon as daemon
        from fastapi import HTTPException
        with patch.object(daemon, '_recorder', SimpleNamespace(state='recording')):
            with patch.object(daemon.subprocess, 'run') as run:
                with self.assertRaises(HTTPException) as caught:
                    daemon.resync_clock()
                self.assertEqual(caught.exception.status_code, 409)
                run.assert_not_called()

    def test_save_failure_returns_500_not_already_idle(self):
        from jetson import record_daemon as daemon
        from jetson.recorder import RecordingError
        from fastapi import HTTPException
        from unittest.mock import Mock
        recorder = Mock()
        recorder.stop.side_effect = RecordingError('disk full')
        with patch.object(daemon, '_recorder', recorder):
            with self.assertRaises(HTTPException) as caught:
                daemon.stop()
        self.assertEqual(caught.exception.status_code, 500)

    def test_pause_resume_endpoints_refuse_wrong_state(self):
        from jetson import record_daemon as daemon
        from jetson.recorder import RecorderStateError
        from fastapi import HTTPException
        from unittest.mock import Mock
        recorder = Mock()
        recorder.pause.side_effect = RecorderStateError('Not recording')
        recorder.resume.side_effect = RecorderStateError('Not paused')
        with patch.object(daemon, '_recorder', recorder):
            for call in (daemon.pause, daemon.resume):
                with self.subTest(call=call.__name__), self.assertRaises(HTTPException) as caught:
                    call()
                self.assertEqual(caught.exception.status_code, 409)

    def test_screen_pause_resume_events_and_paused_state(self):
        from jetson.screen_link import ScreenLink, format_status
        calls = []
        link = ScreenLink(on_start=lambda: calls.append('start'), on_stop=lambda: calls.append('stop'),
                          on_pause=lambda: calls.append('pause'), on_resume=lambda: calls.append('resume'))
        for line in ('$E,PAUSE', '$E,RESUME', '$E,STOP', '$E,START'):
            link._dispatch(line)
        self.assertEqual(calls, ['pause', 'resume', 'stop', 'start'])
        self.assertTrue(format_status('paused', 83, 40, 100, None, None).startswith('$S,4,83,'))

    def test_unsynchronised_chrony_does_not_report_success(self):
        from jetson import record_daemon as daemon
        raw = 'System time : 0.000 seconds slow of NTP time\nStratum : 1\nLeap status : Not synchronised\n'
        results = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=1), SimpleNamespace(stdout=raw)]
        with patch.object(daemon, '_recorder', SimpleNamespace(state='idle')):
            with patch.object(daemon.subprocess, 'run', side_effect=results):
                self.assertFalse(daemon.resync_clock()['synced'])


class KeyboardTests(unittest.TestCase):
    def load_keyboard(self):
        import sys
        sdk = {
            'unitree_sdk2py.core.channel': SimpleNamespace(ChannelFactoryInitialize=lambda *a: None),
            'unitree_sdk2py.go2.sport.sport_client': SimpleNamespace(SportClient=object),
        }
        spec = importlib.util.spec_from_file_location('keyboard_test', Path(__file__).parents[1] / 'scripts/keyboard_control.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, sdk):
            spec.loader.exec_module(module)
        return module

    def test_command_error_attempts_stop(self):
        from unittest.mock import Mock
        keyboard = self.load_keyboard()
        client = Mock()
        client.Move.side_effect = RuntimeError('RPC failed')
        with self.assertRaises(RuntimeError):
            keyboard.send_commands(client)
        client.StopMove.assert_called_once()
        self.assertTrue(keyboard.stop_flag.is_set())

    def test_raw_ctrl_c_exits_and_restores_terminal(self):
        from unittest.mock import Mock
        keyboard = self.load_keyboard()
        stream = Mock()
        stream.read.return_value = chr(3)
        stream.fileno.return_value = 0
        with patch.object(keyboard.sys, 'stdin', stream), patch.object(keyboard.tty, 'setraw'):
            with patch.object(keyboard.termios, 'tcgetattr', return_value='original'):
                with patch.object(keyboard.termios, 'tcsetattr') as restore:
                    keyboard.read_keys()
        self.assertTrue(keyboard.stop_flag.is_set())
        restore.assert_called_once_with(0, keyboard.termios.TCSADRAIN, 'original')


class MasterTests(unittest.TestCase):
    def test_start_goes_to_backend_only(self):
        from laptop import master_record as master
        endpoints = master.Endpoints('http://camera', 'http://jetson')
        with patch.object(master, '_post', return_value=(200, {'used_jetson': True})) as post:
            self.assertTrue(master.start_both('task1', 'test', endpoints))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], 'http://camera/start')


if __name__ == '__main__':
    unittest.main()


class ClockTests(unittest.TestCase):
    """jetson/clock.py: is the clock set, and correcting recordings made before it was."""

    def setUp(self):
        from jetson import clock
        self.clock = clock
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'data'
        self.patches = [patch.object(clock, 'STATE_PATH', Path(self.tmp.name) / 'clock.json'),
                        patch.object(clock, 'chrony_synced', return_value=False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def session(self, rel, clock_entry, start=1793300000.0):
        fp = self.root / rel / 'first_person'
        fp.mkdir(parents=True)
        (fp / 'session_meta.json').write_text(json.dumps({'task': 'walk', 'start_time': start, 'clock': clock_entry}))
        return fp

    def test_recording_stores_whether_the_clock_was_set(self):
        recorder = Recorder(data_root=str(self.root), mock=True)
        recorder.ego = EgocentricCameraCollector(width=64, height=48, mock=True)
        with patch('jetson.recorder.START_DELAY_S', 0):
            path = recorder.start('task1')
            self.assertFalse(json.loads((path / 'session_meta.json').read_text())['clock']['trusted'])
            time.sleep(0.1)
            recorder.stop()
        c = json.loads((path / 'session_meta.json').read_text())['clock']
        self.assertEqual(c['boot_id'], self.clock.boot_id())
        self.assertAlmostEqual(c['boot_epoch'], self.clock.boot_epoch(), delta=1.0)
        self.assertFalse(c['trusted'])

    def test_recordings_of_this_boot_get_the_offset_once_the_clock_is_set(self):
        c, here = self.clock, self.clock.boot_id()
        wrong = c.boot_epoch()
        early = self.session('walk/11_06_2026/10_00_00', {'boot_id': here, 'boot_epoch': wrong, 'trusted': False})
        other_boot = self.session('walk/11_06_2026/09_00_00', {'boot_id': 'another', 'boot_epoch': wrong, 'trusted': False})
        fine = self.session('walk/11_24_2026/10_00_00', {'boot_id': here, 'boot_epoch': wrong, 'trusted': True})
        recording = self.session('walk/11_06_2026/10_30_00', {'boot_id': here, 'boot_epoch': wrong, 'trusted': False})
        old = self.session('walk/10_01_2026/10_00_00', None)
        self.assertEqual(c.correct_sessions(self.root), [], 'nothing while the clock is not set')
        self.assertFalse(c.trusted())
        with patch.object(c, 'boot_epoch', return_value=wrong + 1468800.25):   # the laptop moved the clock 17 days on
            self.assertTrue(c.mark_trusted('laptop'))
            self.assertFalse(c.mark_trusted('laptop'))
            done = c.correct_sessions(self.root, skip=recording)
        self.assertTrue(c.trusted())
        self.assertEqual(done, ['walk/11_06_2026/10_00_00'])
        fix = json.loads((early / 'clock_correction.json').read_text())
        self.assertAlmostEqual(fix['offset_s'], 1468800.25, places=2)
        self.assertAlmostEqual(fix['start_time'], 1793300000.0 + 1468800.25, places=2)
        self.assertEqual(fix['source'], 'laptop')
        for fp in (other_boot, fine, recording, old):
            self.assertFalse((fp / 'clock_correction.json').exists())
        before = (early / 'clock_correction.json').read_text()
        self.assertEqual(c.correct_sessions(self.root), ['walk/11_06_2026/10_30_00'], 'the one recording then, after it stopped')
        self.assertEqual((early / 'clock_correction.json').read_text(), before, 'an existing correction is not rewritten')
        self.assertEqual(json.loads((early / 'session_meta.json').read_text())['start_time'], 1793300000.0,
                         'the recording itself is not changed')

    def test_time_endpoint_sets_the_clock_and_corrects(self):
        from jetson import export_api as api
        c = self.clock
        wrong = c.boot_epoch()
        early = self.session('walk/11_06_2026/10_00_00', {'boot_id': c.boot_id(), 'boot_epoch': wrong, 'trusted': False})
        api.configure(str(self.root), lambda: None)
        with patch.object(api.subprocess, 'run') as run, patch.object(c, 'boot_epoch', return_value=wrong + 600.0):
            out = api.set_time(api.TimeRequest(unix_time=time.time() + 600.0))
        self.assertEqual(out['status'], 'set')
        self.assertIn('date', run.call_args[0][0][2])
        self.assertEqual(out['corrected'], ['walk/11_06_2026/10_00_00'])
        self.assertAlmostEqual(json.loads((early / 'clock_correction.json').read_text())['offset_s'], 600.0, places=2)
        self.assertTrue(c.trusted())

    def test_time_endpoint_within_tolerance_still_counts_as_set(self):
        from jetson import export_api as api
        api.configure(str(self.root), lambda: None)
        with patch.object(api.subprocess, 'run') as run:
            out = api.set_time(api.TimeRequest(unix_time=time.time()))
        self.assertEqual(out['status'], 'unchanged')
        run.assert_not_called()
        self.assertTrue(self.clock.trusted())

    def test_chrony_counts_only_when_synchronised_to_a_real_source(self):
        c = self.clock
        try:
            for out, expect in (('Reference ID    : 0A64CF0C (10.100.207.12)\nLeap status     : Normal\n', True),
                                ('Reference ID    : 7F7F0101 ()\nLeap status     : Normal\n', False),
                                ('Reference ID    : 00000000 ()\nLeap status     : Not synchronised\n', False)):
                c._chrony_cache['t'] = -1e9
                with self.subTest(out=out), patch.object(c.subprocess, 'run', return_value=SimpleNamespace(stdout=out)):
                    self.assertEqual(_REAL_CHRONY_SYNCED(), expect)
            c._chrony_cache['t'] = -1e9
            with patch.object(c.subprocess, 'run', side_effect=FileNotFoundError):
                self.assertFalse(_REAL_CHRONY_SYNCED())
        finally:
            c._chrony_cache['t'] = -1e9

    def test_screen_clock_line(self):
        from jetson.screen_link import format_clock
        self.assertEqual(format_clock(False), '$C,0\n')
        self.assertEqual(format_clock(True), '$C,1\n')

    def test_a_failing_correction_never_breaks_stop(self):
        from jetson import record_daemon as daemon
        from unittest.mock import Mock
        recorder = Mock()
        recorder.stop.return_value = {'session_dir': 'x', 'elapsed_seconds': 1.0, 'samples': {}}
        with patch.object(daemon, '_recorder', recorder), patch.object(self.clock, 'correct_sessions', side_effect=OSError('disk')):
            self.assertEqual(daemon.stop()['status'], 'stopped')
