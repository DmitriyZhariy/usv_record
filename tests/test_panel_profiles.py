"""Профили панели и HTTP без настоящих камер и рекордера."""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import control_panel as panel
import record_profiles as profiles
import record_preflight as preflight
import test_control_panel as fixtures


def selected(mode="800x600@30",dynamic=1,duration=20):
    return dict(mode=mode,exposure_dynamic_framerate=dynamic,duration_seconds=duration)


def report(supported=True):
    return {"ok":True,"devices":{},"modes":{
        mode["mode"]:{"supported":supported,"reasons":[] if supported else ["right: MJPG не заявлен"]}
        for mode in profiles.MODES}}


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controllers=[]
        self.servers=[]

    def tearDown(self):
        for server,thread in self.servers:
            server.shutdown();server.server_close();thread.join(2)
        for controller in self.controllers:
            controller.shutdown();controller.close()

    def controller(self,real=False,probe=None,exposure=None,preview_factory=None,**overrides):
        config=fixtures.make_config(self.temp.name,fake_cameras=not real,**overrides)
        runners=[]
        def factory(argv,line):
            runner=fixtures.FakeRunner(argv,line);runners.append(runner);return runner
        c=panel.PanelController(config,token="test-token",mode_probe=probe,
            prepare_exposure=exposure,preview_factory=preview_factory,runner_factory=factory,
            probe_lock=lambda path:{"busy":False,"error":None},find_leftovers=lambda:[],
            disk_usage=lambda path:fixtures.fake_disk())
        self.controllers.append(c);c.initialize();return c,runners

    def state(self,c,value):
        self.assertTrue(fixtures.wait_until(lambda:c.status().get("state")==value))

    def support_ready(self,c):
        self.assertTrue(fixtures.wait_until(lambda:c.status().get("profile_support",{}).get("state") in ("ok","failed","fake")))

    def server(self,c):
        server=panel.PanelHTTPServer(("127.0.0.1",0),c)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.servers.append((server,thread));return server.server_address[1]

    def request(self,port,path="/api/profile/apply",payload=None,token="test-token",headers=None,raw=None,method="POST"):
        body=json.dumps(selected() if payload is None else payload) if raw is None else raw
        hdr={"Content-Type":"application/json"}
        if token is not None:hdr["X-USV-Token"]=token
        hdr.update(headers or {})
        conn=http.client.HTTPConnection("127.0.0.1",port,timeout=2)
        try:
            conn.request(method,path,body=body if method=="POST" else None,headers=hdr)
            res=conn.getresponse();return res.status,json.loads(res.read())
        finally:conn.close()


class ControllerProfileTests(ProfileCase):
    def test_status_contract(self):
        c,_=self.controller();s=c.status()
        self.assertEqual(s["profile"],selected("1280x720@60",0,0))
        self.assertEqual(len(s["profile_options"]),7)
        self.assertEqual(s["profile_durations"],[0,10,30,300,600,1800,3600])
        self.assertTrue(s["profile_support"]["fake"]);self.assertFalse(s["profile_locked"])
        for key in ("preview","recording","disk","limits","result","log_tail"):self.assertIn(key,s)

    def test_apply_is_memory_only(self):
        c,_=self.controller()
        with mock.patch.object(c,"prepare_exposure") as exposure, \
             mock.patch.object(c,"preview_factory") as preview, \
             mock.patch.object(c,"runner_factory") as runner:
            self.assertEqual(c.apply_profile(selected())["profile"],selected())
            exposure.assert_not_called();preview.assert_not_called();runner.assert_not_called()
        self.assertEqual(c.status()["profile"],selected())
        self.assertEqual(c.status()["limits"]["record_seconds"],20)
        self.assertEqual(c.config.record_seconds,0)

    def test_invalid_profiles_leave_old_profile(self):
        c,_=self.controller();old=c.status()["profile"]
        bad=[[],{},None,dict(selected(),extra=1)]
        bad += [dict(selected(),mode=v) for v in ([],{},True,None,"wrong")]
        bad += [dict(selected(),exposure_dynamic_framerate=v) for v in (True,1.0,"1",2)]
        bad += [dict(selected(),duration_seconds=v) for v in (True,"20",-1,float("nan"),float("inf"))]
        for payload in bad:
            with self.subTest(payload=repr(payload)),self.assertRaises(panel.CommandError) as err:c.apply_profile(payload)
            self.assertEqual(err.exception.status,400);self.assertEqual(c.status()["profile"],old)

    def test_apply_blocked_in_all_non_idle_states(self):
        c,_=self.controller()
        for state in ("starting","preview","recording","stopping","error"):
            with c._lock:c._state=state
            with self.subTest(state=state),self.assertRaises(panel.CommandError) as err:c.apply_profile(selected())
            self.assertEqual(err.exception.status,409)
        with c._lock:c._state="idle"

    def test_shutdown_blocks_apply_and_check_and_starts(self):
        c,_=self.controller();c.shutdown()
        for action in (lambda:c.apply_profile(selected()),lambda:c.handle_command("profile/check"),
                       lambda:c.handle_command("preview/start"),lambda:c.handle_command("record/start")):
            with self.assertRaises(panel.CommandError) as err:action()
            self.assertEqual(err.exception.status,503)

    def test_stuck_preview_blocks_apply(self):
        c,_=self.controller();c._stuck_preview=fixtures.FakePreviewApp(alive_keys=["left"])
        c._stuck_preview_keys=["left"]
        with self.assertRaises(panel.CommandError) as err:c.apply_profile(selected())
        self.assertEqual(err.exception.status,409)
        c._stuck_preview=None;c._stuck_preview_keys=[]

    def test_duration_zero_and_custom_preserved(self):
        c,_=self.controller(record_seconds=35)
        self.assertEqual(c.status()["profile"]["duration_seconds"],35)
        for seconds in (0,20,1.5,3600):
            c.apply_profile(selected(duration=seconds))
            self.assertEqual(c.status()["profile"]["duration_seconds"],seconds)

    def test_profile_resets_on_restart(self):
        c,_=self.controller();c.apply_profile(selected());other,_=self.controller()
        self.assertEqual(other.status()["profile"],selected("1280x720@60",0,0))

    def test_all_modes_reach_wrapper(self):
        c,runners=self.controller(v4l2_ctl="/custom/v4l2-ctl")
        for mode in profiles.MODES:
            with self.subTest(mode=mode["mode"]):
                value=selected(mode["mode"]);c.apply_profile(value)
                c.handle_command("record/start");self.state(c,"recording");cmd=runners[-1].argv
                for key,val in (("--width",mode["width"]),("--height",mode["height"]),
                    ("--fps",int(mode["fps"])),("--seconds",20),("--exposure-dynamic-framerate",1),
                    ("--v4l2-ctl","/custom/v4l2-ctl")):
                    self.assertEqual(cmd[cmd.index(key)+1],str(val))
                self.assertEqual(c.status()["recording"]["profile"],value)
                c.handle_command("record/stop");self.state(c,"idle")

    def test_preview_mode_and_publish_fps_separate(self):
        seen=[]
        def factory(**kw):seen.append(kw);return fixtures.FakePreviewApp()
        c,_=self.controller(preview_factory=factory);c.apply_profile(selected("640x480@30"))
        c.handle_command("preview/start");self.state(c,"preview")
        self.assertEqual(seen[0]["capture_mode"]["mode"],"640x480@30")
        self.assertEqual(seen[0]["fps"],20)
        self.assertEqual(c.status()["preview"]["profile"],selected("640x480@30"))
        c.handle_command("preview/stop");self.state(c,"idle")

    def test_preview_to_record_keeps_profile_and_stops_preview_first(self):
        app=fixtures.FakePreviewApp();c,runners=self.controller(preview_factory=lambda **kw:app)
        c.apply_profile(selected());c.handle_command("preview/start");self.state(c,"preview")
        c.handle_command("record/start");self.state(c,"recording")
        self.assertTrue(app.stopped);self.assertEqual(c.status()["recording"]["profile"],selected())
        argv=runners[0].argv
        self.assertEqual(argv[argv.index("--fps")+1],"30")
        self.assertEqual(argv[argv.index("--exposure-dynamic-framerate")+1],"1")

    def test_fake_preview_never_runs_hardware_probe_or_exposure(self):
        probe=mock.Mock(side_effect=AssertionError("hardware probe"))
        exposure=mock.Mock(side_effect=AssertionError("hardware exposure"))
        c,_=self.controller(probe=probe,exposure=exposure);c.apply_profile(selected())
        c.handle_command("profile/check");c.handle_command("preview/start");self.state(c,"preview")
        probe.assert_not_called();exposure.assert_not_called()

    def test_profile_frozen_during_start(self):
        c,runners=self.controller();gate=threading.Event();entered=threading.Event();original=c._run_record_start
        def delayed():entered.set();gate.wait(2);original()
        c._run_record_start=delayed;c.apply_profile(selected());c.handle_command("record/start")
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(panel.CommandError):c.apply_profile(selected("640x480@60"))
            self.assertEqual(c._launch_profile,selected())
        finally:gate.set()
        self.state(c,"recording")
        self.assertEqual(runners[0].argv[runners[0].argv.index("--width")+1],"800")

    def test_profile_check_blocked_during_capture(self):
        c,_=self.controller();c.handle_command("preview/start");self.state(c,"preview")
        with self.assertRaises(panel.CommandError) as err:c.handle_command("profile/check")
        self.assertEqual(err.exception.status,409)

    def test_record_stop_does_not_cancel_preview_start(self):
        c,_=self.controller()
        with c._lock:c._state="starting";c._operation="preview"
        with self.assertRaises(panel.CommandError):c.handle_command("record/stop")
        self.assertFalse(c._stop_requested)
        with c._lock:c._state="idle";c._operation=None

    def test_last_record_profile_survives_new_selection(self):
        c,_=self.controller();c.apply_profile(selected());c.handle_command("record/start");self.state(c,"recording")
        c.handle_command("record/stop");self.state(c,"idle");c.apply_profile(selected("640x480@60",0,10))
        self.assertEqual(c.status()["recording"]["profile"],selected())
        self.assertEqual(c.status()["profile"]["mode"],"640x480@60")

    def test_camera_metadata_fields_are_preserved(self):
        c,_=self.controller()
        meta={"status":"verified","requested_mode":{"width":800,"height":600,"fps":30},
              "negotiated_mode":{"codec":"mjpeg","width":800,"height":600,"fps":None},
              "profile":selected(),"measured_capture_fps":17.25}
        with c._lock:c._metadata={"left":meta,"right":dict(meta)}
        c._publish()
        for camera in c.status()["recording"]["cameras"].values():
            self.assertEqual(camera["measured_capture_fps"],17.25)
            self.assertEqual(camera["requested_mode"],meta["requested_mode"])
            self.assertIsNone(camera["negotiated_mode"]["fps"])


class ProbeAndExposureTests(ProfileCase):
    def test_slow_probe_keeps_status_fast_and_blocks_start(self):
        entered=threading.Event();gate=threading.Event()
        def probe(devices):entered.set();gate.wait(2);return report()
        c,_=self.controller(real=True,probe=probe)
        try:
            self.assertTrue(entered.wait(1));start=time.monotonic()
            self.assertEqual(c.status()["profile_support"]["state"],"checking")
            self.assertLess(time.monotonic()-start,0.2)
            for command in ("preview/start","record/start"):
                with self.assertRaises(panel.CommandError):c.handle_command(command)
            with self.assertRaises(panel.CommandError):c.apply_profile(selected())
            c.handle_command("profile/check")
        finally:gate.set()
        self.support_ready(c);self.assertEqual(c.status()["profile_support"]["state"],"ok")

    def test_probe_failure_visible_and_retry_recovers(self):
        probe=mock.Mock(side_effect=RuntimeError("probe failed"));c,_=self.controller(real=True,probe=probe)
        self.support_ready(c);self.assertIn("probe failed",c.status()["profile_support"]["error"])
        with self.assertRaises(panel.CommandError):c.handle_command("record/start")
        probe.side_effect=None;probe.return_value=report()
        c.handle_command("profile/check");self.support_ready(c);c.apply_profile(selected())

    def test_unsupported_on_one_camera_blocks_apply_and_launch(self):
        c,_=self.controller(real=True,probe=lambda devices:report(False));self.support_ready(c)
        with self.assertRaises(panel.CommandError) as err:c.apply_profile(selected())
        self.assertIn("right",str(err.exception))
        for command in ("preview/start","record/start"):
            with self.assertRaises(panel.CommandError):c.handle_command(command)

    def test_real_preview_rechecks_and_prepares_before_open(self):
        events=[]
        def probe(devices):events.append("probe");return report()
        def exposure(devices,value,**kw):events.append(("exposure",list(devices),value,kw["v4l2_ctl"]))
        def factory(**kw):events.append("open");return fixtures.FakePreviewApp()
        c,_=self.controller(real=True,probe=probe,exposure=exposure,preview_factory=factory,v4l2_ctl="/custom/v4l2-ctl")
        self.support_ready(c);c.apply_profile(selected());events.clear()
        c.handle_command("preview/start");self.state(c,"preview")
        self.assertEqual(events,["probe",("exposure",["/dev/video2","/dev/video4"],1,"/custom/v4l2-ctl"),"open"])

    def test_exposure_failure_prevents_preview_open(self):
        factory=mock.Mock();c,_=self.controller(real=True,probe=lambda devices:report(),
            exposure=mock.Mock(side_effect=preflight.PreflightError("readback mismatch")),preview_factory=factory)
        self.support_ready(c);c.handle_command("preview/start");self.state(c,"error")
        factory.assert_not_called();self.assertIn("readback mismatch",c.status()["last_error"])

    def test_changed_support_prevents_preview_exposure_and_open(self):
        probe=mock.Mock(return_value=report());exposure=mock.Mock();factory=mock.Mock()
        c,_=self.controller(real=True,probe=probe,exposure=exposure,preview_factory=factory)
        self.support_ready(c);probe.return_value=report(False)
        c.handle_command("preview/start");self.state(c,"error")
        exposure.assert_not_called();factory.assert_not_called()

    def test_shutdown_during_probe_prevents_preview_open(self):
        gate=threading.Event();entered=threading.Event();calls=[]
        def probe(devices):
            if calls:entered.set();gate.wait(2)
            calls.append(1);return report()
        exposure=mock.Mock();factory=mock.Mock()
        c,_=self.controller(real=True,probe=probe,exposure=exposure,preview_factory=factory)
        self.support_ready(c);c.handle_command("preview/start");self.assertTrue(entered.wait(1))
        c.shutdown();gate.set();self.assertTrue(fixtures.wait_until(lambda:len(calls)==2));self.state(c,"error")
        factory.assert_not_called();exposure.assert_not_called()

    def test_busy_device_prevents_preview_control_changes(self):
        factory=mock.Mock();exposure=mock.Mock()
        c,_=self.controller(real=True,probe=lambda devices:report(),exposure=exposure,preview_factory=factory)
        self.support_ready(c)
        with mock.patch.object(preflight,"find_device_holders",return_value=[12345]):
            c.handle_command("preview/start");self.state(c,"error")
        factory.assert_not_called();exposure.assert_not_called();self.assertIn("12345",c.status()["last_error"])

    def test_default_probe_uses_configured_v4l2_path(self):
        with mock.patch.object(preflight,"probe_modes",return_value=report()) as probe:
            c,_=self.controller(real=True,v4l2_ctl="/custom/v4l2-ctl");self.support_ready(c)
            probe.assert_called_once_with(["/dev/video2","/dev/video4"],profiles.MODES,v4l2_ctl="/custom/v4l2-ctl")


class HttpProfileTests(ProfileCase):
    def test_apply_returns_200_and_status_contains_profile(self):
        c,_=self.controller();port=self.server(c);code,data=self.request(port)
        self.assertEqual(code,200);self.assertEqual(data["profile"],selected())
        code,data=self.request(port,"/api/status",method="GET");self.assertEqual(data["profile"],selected())

    def test_missing_wrong_token_and_cross_origin_rejected(self):
        c,_=self.controller();port=self.server(c)
        for token,headers,expected in ((None,{},401),("bad",{},401),
            ("test-token",{"Origin":"http://evil.example"},403),
            ("test-token",{"Sec-Fetch-Site":"cross-site"},403)):
            with self.subTest(headers=headers,token=token):
                self.assertEqual(self.request(port,token=token,headers=headers)[0],expected)
        self.assertEqual(c.status()["profile"]["mode"],"1280x720@60")

    def test_invalid_json_and_invalid_values_return_400(self):
        c,_=self.controller();port=self.server(c)
        for raw in ("","{","[]","{}","null",json.dumps(dict(selected(),mode=[])),
            json.dumps(dict(selected(),exposure_dynamic_framerate=True)),
            json.dumps(dict(selected(),duration_seconds=float("nan"))),json.dumps(dict(selected(),extra=1))):
            with self.subTest(raw=raw):self.assertEqual(self.request(port,raw=raw)[0],400)

    def test_content_type_transfer_encoding_and_large_body(self):
        c,_=self.controller();port=self.server(c)
        self.assertEqual(self.request(port,headers={"Content-Type":"text/plain"})[0],400)
        self.assertEqual(self.request(port,headers={"Transfer-Encoding":"chunked"})[0],400)
        self.assertEqual(self.request(port,raw=" "*(panel.MAX_REQUEST_BODY+1))[0],413)

    def test_busy_apply_returns_409(self):
        c,_=self.controller();port=self.server(c);c.handle_command("preview/start");self.state(c,"preview")
        self.assertEqual(self.request(port)[0],409)
        self.assertEqual(self.request(port,"/api/profile/check",payload={})[0],409)

    def test_check_requires_token_and_returns_202(self):
        c,_=self.controller();port=self.server(c)
        self.assertEqual(self.request(port,"/api/profile/check",payload={},token=None)[0],401)
        self.assertEqual(self.request(port,"/api/profile/check",payload={})[0],202)

    def test_profile_http_to_runner_argv_stub(self):
        c,runners=self.controller();port=self.server(c);self.assertEqual(self.request(port)[0],200)
        self.assertEqual(self.request(port,"/api/record/start",payload={})[0],202);self.state(c,"recording")
        argv=runners[0].argv
        self.assertEqual(argv[argv.index("--fps")+1],"30");self.assertEqual(argv[argv.index("--seconds")+1],"20")
        self.assertEqual(self.request(port,"/api/record/stop",payload={})[0],202);self.state(c,"idle")

    def test_slow_probe_http_status_and_busy_response(self):
        gate=threading.Event();entered=threading.Event()
        def probe(devices):entered.set();gate.wait(2);return report()
        c,_=self.controller(real=True,probe=probe);port=self.server(c)
        try:
            self.assertTrue(entered.wait(1));start=time.monotonic()
            code,data=self.request(port,"/api/status",method="GET")
            self.assertEqual(code,200);self.assertLess(time.monotonic()-start,0.5)
            self.assertEqual(data["profile_support"]["state"],"checking");self.assertEqual(self.request(port)[0],409)
        finally:gate.set()
        self.support_ready(c)

    def test_page_contains_controls_without_token(self):
        c,_=self.controller();port=self.server(c)
        conn=http.client.HTTPConnection("127.0.0.1",port,timeout=2)
        conn.request("GET","/");res=conn.getresponse();text=res.read().decode();conn.close()
        self.assertEqual(res.status,200)
        for word in ('id="profile-mode"','id="profile-dynamic"','id="profile-duration"',
                     '/api/profile/apply','Применить профиль'):self.assertIn(word,text)
        self.assertNotIn('test-token',text)


class ConfigurationTests(unittest.TestCase):
    def test_custom_v4l2_cli_reaches_config(self):
        config=panel.config_from_args(panel.build_parser().parse_args(["--v4l2-ctl","/custom/v4l2-ctl"]))
        self.assertEqual(config.v4l2_ctl,"/custom/v4l2-ctl")

    def test_nonfinite_initial_duration_rejected(self):
        for value in (float("nan"),float("inf"),-1):
            with self.subTest(value=value),self.assertRaises(panel.PanelError):
                panel.validate_config(panel.PanelConfig(record_seconds=value))


if __name__=="__main__":unittest.main()
