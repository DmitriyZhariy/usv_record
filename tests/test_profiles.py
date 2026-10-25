"""Профили и нижние слои без оборудования; PyAV и subprocess подменяются."""
import contextlib
import importlib.util
import io
import json
import queue
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import record_profiles as profiles
import record_preflight as preflight
import record_dual_session as dual

DEVICES = ('/dev/video2', '/dev/video4')


def formats_text(modes=None):
    lines = ["[0]: 'MJPG' (Motion-JPEG)"]
    for mode in profiles.MODES if modes is None else modes:
        lines += [f"    Size: Discrete {mode['width']}x{mode['height']}",
                  f"        Interval: Discrete 0.017s ({mode['fps']:.3f} fps)"]
    lines += ["[1]: 'YUYV' (YUYV 4:2:2)",
              '    Size: Discrete 1920x1080',
              '        Interval: Discrete 0.200s (5.000 fps)']
    return '\n'.join(lines)


class Runner:
    def __init__(self, texts=None, dynamic=1):
        self.texts = texts or {}
        self.dynamic = dynamic
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        device = argv[argv.index('-d') + 1]
        if '--list-formats-ext' in argv:
            answer = self.texts.get(device, formats_text())
            if isinstance(answer, Exception):
                raise answer
            return SimpleNamespace(returncode=0, stdout=answer, stderr='')
        if '--get-ctrl=exposure_dynamic_framerate' in argv:
            return SimpleNamespace(returncode=0,
                stdout=f'exposure_dynamic_framerate: {self.dynamic}\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')


class ProfileTests(unittest.TestCase):
    def test_exactly_seven_modes(self):
        self.assertEqual([m['mode'] for m in profiles.MODES], [
            '1920x1080@60', '1280x720@60', '1280x720@30',
            '800x600@60', '800x600@30', '640x480@60', '640x480@30'])

    def test_each_mode_options_and_metadata(self):
        for mode in profiles.MODES:
            with self.subTest(mode=mode['mode']):
                profile = profiles.make_profile(mode['mode'], 1, 20)
                self.assertEqual(profiles.resolve_mode(
                    mode['width'], mode['height'], mode['fps']), mode)
                self.assertEqual(profiles.capture_options(mode), {
                    'input_format':'mjpeg',
                    'video_size':f"{mode['width']}x{mode['height']}",
                    'framerate':f"{mode['fps']:g}"})
                fields = profiles.recorder_fields(profile)
                self.assertEqual(fields['requested_mode']['fps'], mode['fps'])
                self.assertEqual(fields['profile']['duration_seconds'], 20)

    def test_invalid_mode_types_are_profile_errors(self):
        for value in ([], {}, True, None, 60, 'bad'):
            with self.subTest(value=value), self.assertRaises(profiles.ProfileError):
                profiles.make_profile(value, 1, 10)

    def test_dynamic_must_be_integer(self):
        for value in (True, False, 1.0, '1', -1, 2, None):
            with self.subTest(value=value), self.assertRaises(profiles.ProfileError):
                profiles.make_profile(profiles.DEFAULT_MODE, value, 10)

    def test_invalid_duration(self):
        for value in (True, False, '10', None, -1, float('nan'), float('inf'), 10**1000):
            with self.subTest(type=type(value).__name__), self.assertRaises(profiles.ProfileError):
                profiles.make_profile(profiles.DEFAULT_MODE, 0, value)

    def test_ui_durations_and_arbitrary_cli_duration(self):
        self.assertEqual(profiles.UI_DURATIONS, (0,10,30,300,600,1800,3600))
        for value in (0, 20, 1.5):
            self.assertEqual(profiles.make_profile(
                profiles.DEFAULT_MODE,0,value)['duration_seconds'], value)

    def test_unknown_missing_keys_and_non_object(self):
        for payload in (None, [], {}, {'extra':1}):
            with self.subTest(payload=payload), self.assertRaises(profiles.ProfileError):
                profiles.validate_profile(payload)

    def test_valid_json_profile(self):
        value = {'mode':profiles.DEFAULT_MODE,
                 'exposure_dynamic_framerate':1,'duration_seconds':600}
        self.assertEqual(profiles.validate_profile(value), value)

    def test_no_silent_fps_normalization(self):
        for args in ((1280,720,59.8),(True,720,60),(1280,720,True),
                     (1280,720,float('nan')),(1280,720,float('inf')),
                     (640,480,5),(800,600,20),(1280,720,10**1000)):
            with self.subTest(args=args), self.assertRaises(profiles.ProfileError):
                profiles.resolve_mode(*args)

    def test_codec_and_dimensions(self):
        mode=profiles.require_mode(profiles.DEFAULT_MODE)
        self.assertTrue(profiles.mode_matches(mode,'mjpeg',1280,720))
        for codec,w,h in (('rawvideo',1280,720),('mjpeg',640,480)):
            self.assertFalse(profiles.mode_matches(mode,codec,w,h))

    def test_measured_rate(self):
        self.assertEqual(profiles.measured_rate(1_000_000_000,2_000_000_000,31),30)
        for first,last,n in ((None,None,0),(10,10,1),(10,10,2),(20,10,2)):
            self.assertIsNone(profiles.measured_rate(first,last,n))


class ModePreflightTests(unittest.TestCase):
    def test_all_modes_supported_by_both(self):
        report=preflight.probe_modes(DEVICES,runner=Runner())
        self.assertTrue(report['ok'])
        self.assertTrue(all(item['supported'] for item in report['modes'].values()))

    def test_yuyv_does_not_confirm_mjpg(self):
        text="[0]: 'YUYV'\n Size: Discrete 1280x720\n Interval: Discrete 0.017s (60.000 fps)"
        report=preflight.probe_modes(DEVICES,runner=Runner({DEVICES[1]:text}))
        item=report['modes'][profiles.DEFAULT_MODE]
        self.assertFalse(item['supported'])
        self.assertIn(DEVICES[1],';'.join(item['reasons']))

    def test_driver_rounding_tolerance(self):
        text=formats_text().replace('(60.000 fps)','(59.940 fps)')
        report=preflight.probe_modes(DEVICES,runner=Runner(dict.fromkeys(DEVICES,text)))
        self.assertTrue(report['modes'][profiles.DEFAULT_MODE]['supported'])

    def test_unparseable_missing_command_and_timeout(self):
        import subprocess
        for answer in ('garbage',FileNotFoundError(),subprocess.TimeoutExpired('v4l2-ctl',5)):
            with self.subTest(answer=type(answer).__name__):
                report=preflight.probe_modes(DEVICES,runner=Runner({DEVICES[1]:answer}))
                self.assertFalse(report['ok'])
                self.assertFalse(report['modes'][profiles.DEFAULT_MODE]['supported'])

    def test_stepwise_size_is_parser_limitation(self):
        text=("[0]: 'MJPG'\n Size: Stepwise 48x32 - 1920x1080 with step 2/2\n"
              ' Interval: Stepwise 0.017s - 1.000s with step 0.001s')
        report=preflight.probe_modes(DEVICES,runner=Runner(dict.fromkeys(DEVICES,text)))
        reason=';'.join(report['modes'][profiles.DEFAULT_MODE]['reasons'])
        self.assertIn('Stepwise/Continuous',reason)
        self.assertIn('не подтверждена',reason)

    def test_stepwise_interval_is_not_confirmation(self):
        text="[0]: 'MJPG'\n Size: Discrete 1280x720\n Interval: Continuous 0.017s - 1.000s"
        report=preflight.probe_modes(DEVICES,runner=Runner(dict.fromkeys(DEVICES,text)))
        self.assertFalse(report['modes'][profiles.DEFAULT_MODE]['supported'])

    def test_empty_devices_are_not_success(self):
        with self.assertRaises(preflight.PreflightError):
            preflight.probe_modes([],runner=Runner())

    def test_generator_devices_checked_on_both(self):
        report=preflight.probe_modes(iter(DEVICES),runner=Runner({DEVICES[1]:'bad'}))
        self.assertFalse(report['modes'][profiles.DEFAULT_MODE]['supported'])

    def test_dynamic_one_sets_both_then_reads_both(self):
        runner=Runner()
        report=preflight.prepare_exposure(iter(DEVICES),1,runner=runner)
        self.assertEqual(len(report['cameras']),2)
        self.assertEqual([argv[-1] for argv in runner.calls],[
            '--set-ctrl=exposure_dynamic_framerate=1',
            '--set-ctrl=exposure_dynamic_framerate=1',
            '--get-ctrl=exposure_dynamic_framerate',
            '--get-ctrl=exposure_dynamic_framerate'])

    def test_dynamic_float_never_reaches_subprocess(self):
        runner=Runner()
        for method,kw in ((preflight.set_exposure,{'value':1.0}),
                          (preflight.verify_exposure,{'expected':1.0})):
            with self.assertRaises(preflight.PreflightError):
                method(DEVICES[0],runner=runner,**kw)
        self.assertEqual(runner.calls,[])

    def test_mismatched_dynamic_is_failure(self):
        with self.assertRaises(preflight.PreflightError):
            preflight.prepare_exposure(DEVICES,1,runner=Runner(dynamic=0))

    def test_preflight_order_including_default_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            events=[]
            runner=Runner()
            def usage(path):
                events.append('disk')
                return SimpleNamespace(total=1000*preflight.MIB, used=500*preflight.MIB, free=500*preflight.MIB)
            def run(argv,timeout):
                events.append(argv[-1])
                return runner(argv,timeout)
            with mock.patch.object(preflight,'find_device_holders',side_effect=lambda *a,**k: events.append('holders') or []):
                report=preflight.run_preflight(temp,DEVICES,min_free_mib=100,
                    critical_free_mib=10,exposure_dynamic_framerate=1,runner=run,usage=usage)
            self.assertEqual(events, ['disk','holders','holders',
                '--list-formats-ext','--list-formats-ext',
                '--set-ctrl=exposure_dynamic_framerate=1',
                '--set-ctrl=exposure_dynamic_framerate=1',
                '--get-ctrl=exposure_dynamic_framerate',
                '--get-ctrl=exposure_dynamic_framerate'])
            self.assertIsNotNone(report['mode_support'])

    def test_unsupported_mode_blocks_before_set(self):
        with tempfile.TemporaryDirectory() as temp:
            runner=Runner({DEVICES[1]:'bad'})
            with self.assertRaises(preflight.PreflightError):
                preflight.run_preflight(temp,DEVICES,min_free_mib=100,
                    critical_free_mib=10,runner=runner,
                    usage=lambda p:SimpleNamespace(total=1000*preflight.MIB, used=500*preflight.MIB, free=500*preflight.MIB),
                    proc_root=Path(temp)/'no-proc')
            self.assertFalse(any('--set-ctrl' in arg for call in runner.calls for arg in call))


class WrapperProfileTests(unittest.TestCase):
    def test_every_mode_forwarded_to_both_recorders(self):
        for mode in profiles.MODES:
            for camera in dual.CAMERAS:
                with self.subTest(mode=mode['mode'],camera=camera['id']):
                    args=dual.build_parser().parse_args([
                        '--width',str(mode['width']),'--height',str(mode['height']),
                        '--fps',str(mode['fps']),'--seconds','20',
                        '--exposure-dynamic-framerate','1'])
                    cmd=dual.build_recorder_command('python','recorder.py',camera,
                        camera['default_device'],'out',args)
                    for key,value in (('--width',mode['width']),('--height',mode['height']),
                                      ('--fps',int(mode['fps'])),('--seconds',20),
                                      ('--exposure-dynamic-framerate',1)):
                        self.assertEqual(cmd[cmd.index(key)+1],str(value))

    def test_custom_v4l2_path_forwarded(self):
        options=dual.build_parser().parse_args(['--v4l2-ctl','/custom/v4l2-ctl'])
        for camera in dual.CAMERAS:
            cmd=dual.build_recorder_command('python','recorder.py',camera,
                camera['default_device'],'out',options)
            self.assertEqual(cmd[cmd.index('--v4l2-ctl')+1],'/custom/v4l2-ctl')

    def test_defaults_and_zero_duration(self):
        args=dual.build_parser().parse_args([])
        self.assertEqual((args.width,args.height,args.fps,args.seconds,
                          args.exposure_dynamic_framerate),(1280,720,60,35,0))
        self.assertEqual(dual.build_parser().parse_args(['--seconds','0']).seconds,0)

    def test_invalid_cli_profile_fails_before_lock(self):
        for argv in (['--fps','nan'],['--fps','inf'],['--fps','59.8'],
                     ['--seconds','nan'],['--seconds','inf'],
                     ['--width','640','--height','480','--fps','5']):
            with self.subTest(argv=argv), \
                 mock.patch.object(dual,'acquire_wrapper_lock') as lock, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dual.main(argv),1)
                lock.assert_not_called()

    def test_preflight_error_creates_no_session(self):
        with mock.patch.object(dual,'acquire_wrapper_lock',return_value=(io.StringIO(),None)), \
             mock.patch.object(dual.signal,'signal'), \
             mock.patch.object(dual.preflight,'run_preflight',side_effect=preflight.PreflightError('unsupported')), \
             mock.patch.object(dual,'create_session') as create, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dual.main([]),1)
            create.assert_not_called()


class Packet:
    def __init__(self,payload):
        self.payload=payload
        self.size=len(payload)
        self.is_keyframe=True
        self.is_corrupt=False
        self.pts=0
        self.dts=0
        self.duration=1
        self.time_base=Fraction(1,60)
    def __bytes__(self):
        return self.payload


class RecorderProfileTests(unittest.TestCase):
    def setUp(self):
        av=SimpleNamespace(Packet=Packet,__version__='test-stub',library_versions={},
            container=SimpleNamespace(OutputContainer=SimpleNamespace(add_stream_from_template=object())))
        spec=importlib.util.spec_from_file_location('recorder_profiles_test',ROOT/'record_camera_pyav_queue.py')
        with mock.patch.dict(sys.modules,{'av':av}):
            self.rec=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.rec)

    def test_each_mode_capture_and_metadata_with_stub_writer(self):
        for mode in profiles.MODES:
            with self.subTest(mode=mode['mode']),tempfile.TemporaryDirectory() as temp:
                rec=self.rec
                rec.stop_requested=False
                context=SimpleNamespace(name='mjpeg',width=mode['width'],height=mode['height'])
                stream=SimpleNamespace(codec_context=context,time_base=Fraction(1,60))
                def packets():
                    for _ in range(3):
                        yield Packet(b'test-jpeg-payload')
                    rec.stop_requested=True
                    yield Packet(b'stop-sentinel')
                camera=mock.MagicMock()
                camera.__enter__.return_value=camera
                camera.streams.video=[stream]
                camera.demux.side_effect=lambda stream:packets()
                def writer(buffer,done,failed,stream,folder,args,result):
                    while True:
                        try:
                            buffer.get()
                            result['written_packets']+=1
                        except queue.Empty:
                            if done.is_set(): break
                output=Path(temp)/'camera'
                args=['recorder','--output-dir',str(output),'--width',str(mode['width']),
                      '--height',str(mode['height']),'--fps',str(mode['fps']),
                      '--seconds','0','--exposure-dynamic-framerate','1']
                with mock.patch.object(sys,'argv',args), \
                     mock.patch.object(rec.signal,'signal'), \
                     mock.patch.object(rec.preflight,'verify_exposure',return_value={
                         'device':'/dev/video2','exposure_dynamic_framerate':1}) as exposure, \
                     mock.patch.object(rec.av,'open',create=True,return_value=camera) as opened, \
                     mock.patch.object(rec,'write_packets',side_effect=writer), \
                     mock.patch.object(rec,'verify',return_value=(3,[{
                         'payload_match':True,'pts_match':True,
                         'keyflags_match':True,'counts_match':True}])), \
                     mock.patch.object(rec.time,'monotonic_ns',side_effect=[
                         1_000_000_000,1_050_000_000,1_100_000_000,1_150_000_000]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(rec.main(),0)
                exposure.assert_called_once_with('/dev/video2',expected=1,v4l2_ctl='v4l2-ctl')
                self.assertEqual(opened.call_args.kwargs['options'],profiles.capture_options(mode))
                meta=json.loads((output/'session_metadata.json').read_text())
                self.assertEqual(meta['requested_mode']['fps'],mode['fps'])
                self.assertEqual(meta['profile']['mode'],mode['mode'])
                self.assertEqual(meta['profile']['exposure_dynamic_framerate'],1)
                self.assertEqual(meta['confirmed_camera_controls']['exposure_dynamic_framerate'],1)
                self.assertEqual(meta['negotiated_mode']['width'],mode['width'])
                self.assertIsNone(meta['negotiated_mode']['fps'])
                self.assertEqual(meta['measured_capture_fps'],20)
                self.assertFalse(meta['timestamp_is_exposure_time'])
                for key in ('timestamp_policy','source_timestamp_policy','queue_limits',
                            'queue_overflows','writer','accepted_packets','verification'):
                    self.assertIn(key,meta)

    def test_exposure_failure_creates_no_camera_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'camera'
            with mock.patch.object(sys,'argv',['recorder','--output-dir',str(folder)]), \
                 mock.patch.object(self.rec.signal,'signal'), \
                 mock.patch.object(self.rec.preflight,'verify_exposure',side_effect=preflight.PreflightError('mismatch')), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.rec.main(),1)
            self.assertFalse(folder.exists())

    def test_codec_mismatch_starts_no_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            camera=mock.MagicMock()
            camera.__enter__.return_value=camera
            camera.streams.video=[SimpleNamespace(codec_context=SimpleNamespace(
                name='rawvideo',width=1280,height=720))]
            folder=Path(temp)/'camera'
            with mock.patch.object(sys,'argv',['recorder','--output-dir',str(folder)]), \
                 mock.patch.object(self.rec.signal,'signal'), \
                 mock.patch.object(self.rec.preflight,'verify_exposure',return_value={}), \
                 mock.patch.object(self.rec.av,'open',create=True,return_value=camera), \
                 mock.patch.object(self.rec.threading,'Thread') as thread, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.rec.main(),1)
            thread.assert_not_called()
            self.assertEqual(json.loads((folder/'session_metadata.json').read_text())['status'],'failed')


if __name__=='__main__':
    unittest.main()
