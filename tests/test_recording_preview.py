"""Просмотр из рекордера: очереди, IPC, HTTP, изоляция отказов без камер."""
import base64
import contextlib
import http.client
import io
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import recording_preview as live
import control_panel as panel
import record_dual_session as dual
import record_profiles as profiles
import preview_dual_mjpeg as preview
import test_control_panel as helpers
import test_profiles as recorder_helpers
JPEG=base64.b64decode(preview.FAKE_JPEGS['left'])


def wait_for(predicate, seconds=2):
    return helpers.wait_until(predicate,timeout=seconds)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)

    def test_round_trip_and_atomic_file(self):
        pub=live.SnapshotPublisher(self.temp.name,'cam_left');pub.start()
        try:
            mono=time.monotonic_ns();self.assertTrue(pub.offer(JPEG,mono,7))
            path=live.frame_path(self.temp.name,'cam_left')
            self.assertTrue(wait_for(path.exists))
            frame=live.decode_frame(path.read_bytes())
            self.assertEqual((frame.seq,frame.mono_ns,frame.payload),(7,mono,JPEG))
            self.assertFalse(path.with_suffix('.tmp').exists())
        finally:pub.close()
        self.assertFalse(pub.snapshot()['thread_alive'])

    def test_rate_limit_is_independent_of_record_fps(self):
        now=[0.0];pub=live.SnapshotPublisher(self.temp.name,'cam_left',clock=lambda:now[0])
        self.assertTrue(pub.offer(JPEG,1,1));now[0]=0.1
        self.assertFalse(pub.offer(JPEG,2,2));now[0]=0.5
        self.assertTrue(pub.offer(JPEG,3,3))
        self.assertEqual(pub._pending.seq,3);pub.close()

    def test_pending_slot_contains_only_latest(self):
        now=[0.0];pub=live.SnapshotPublisher(self.temp.name,'cam_left',clock=lambda:now[0])
        for n in range(100):
            now[0]=n;pub.offer(JPEG,n,n)
        self.assertEqual(pub._pending.seq,99)
        self.assertEqual(pub.frames_skipped,99);pub.close()

    def test_offer_never_waits_for_busy_slot(self):
        pub=live.SnapshotPublisher(self.temp.name,'cam_left')
        pub._lock.acquire()
        try:
            started=time.monotonic();self.assertFalse(pub.offer(JPEG,1,1))
            self.assertLess(time.monotonic()-started,0.1)
        finally:pub._lock.release();pub.close()

    def test_invalid_and_oversize_preview_rejected(self):
        pub=live.SnapshotPublisher(self.temp.name,'cam_left')
        for data in (b'',b'not-jpeg',b'\xff\xd8'+b'x'*live.MAX_JPEG+b'\xff\xd9'):
            self.assertFalse(pub.offer(data,1,1))
        self.assertIsNone(pub._pending);pub.close()

    def test_write_error_disables_preview_not_caller(self):
        pub=live.SnapshotPublisher(self.temp.name,'cam_left')
        with mock.patch.object(pub,'_write',side_effect=OSError('preview full')):
            pub.start();self.assertTrue(pub.offer(JPEG,1,1))
            self.assertTrue(wait_for(lambda:pub.error is not None))
        self.assertIn('preview full',pub.error)
        self.assertFalse(pub.offer(JPEG,2,2));pub.close()

    def test_bad_header_and_unknown_camera(self):
        with self.assertRaises(ValueError):live.decode_frame(b'garbage')
        with self.assertRaises(ValueError):live.frame_path(self.temp.name,'../x')
        with self.assertRaises(ValueError):live.SnapshotPublisher(self.temp.name,'cam_left',fps=10)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader=live.RecordPreview(max_clients=1);self.addCleanup(self.reader.close)

    def put_frame(self,seq=1,age=0,key='cam_left'):
        mono=time.monotonic_ns()-int(age*1e9)
        live.frame_path(self.reader.directory,key).write_bytes(live.HEADER.pack(live.MAGIC,seq,mono)+JPEG)

    def test_read_and_age_without_camera_capture(self):
        self.put_frame(seq=6)
        with mock.patch.object(preview,'PreviewApp',side_effect=AssertionError('camera opened')):
            self.assertEqual(self.reader.read_frame('left').seq,6)
            snapshot=self.reader.snapshot()
        self.assertTrue(snapshot['cameras']['left']['has_frame'])
        self.assertIsNone(snapshot['cameras']['left']['warning'])
        self.assertEqual(snapshot['source'],'recorder')

    def test_missing_and_stale_are_visible(self):
        self.assertIsNone(self.reader.read_frame('left'))
        self.assertIsNotNone(self.reader.snapshot()['cameras']['left']['warning'])
        self.put_frame(age=10)
        self.assertGreater(self.reader.snapshot()['cameras']['left']['last_frame_age_s'],9)
        self.assertIsNotNone(self.reader.snapshot()['cameras']['left']['warning'])

    def test_invalid_file_is_not_a_recording_error(self):
        live.frame_path(self.reader.directory,'cam_left').write_bytes(b'invalid')
        self.assertIsNone(self.reader.read_frame('left'))
        self.assertIsNotNone(self.reader.snapshot()['cameras']['left']['error'])

    def test_client_limit_and_release(self):
        self.assertTrue(self.reader.acquire_client('left'))
        self.assertFalse(self.reader.acquire_client('right'))
        self.reader.release_client('left')
        self.assertTrue(self.reader.acquire_client('right'))
        self.reader.release_client('right');self.assertEqual(self.reader.clients_total,0)

    def test_new_session_directory_and_cleanup(self):
        other=live.RecordPreview();self.addCleanup(other.close)
        self.assertNotEqual(self.reader.directory,other.directory)
        self.put_frame();self.assertIsNone(other.read_frame('left'))
        path=self.reader.directory;self.reader.close()
        self.assertFalse(path.exists());self.assertFalse(self.reader.acquire_client('left'))

    def test_cross_process_publisher(self):
        import subprocess
        program="""import sys,time
from recording_preview import SnapshotPublisher
p=SnapshotPublisher(sys.argv[1],'cam_left');p.start()
p.offer(bytes.fromhex(sys.argv[2]),time.monotonic_ns(),9)
end=time.monotonic()+2
while p.frames_published<1 and time.monotonic()<end:time.sleep(.01)
p.close()
assert p.frames_published==1
"""
        result=subprocess.run([sys.executable,'-c',program,str(self.reader.directory),JPEG.hex()],
            cwd=ROOT,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.reader.read_frame('left').seq,9)


class PanelLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.c,self.runners=helpers.make_controller(self.temp.name)
        self.c.initialize();self.servers=[]

    def tearDown(self):
        for server,thread in self.servers:
            server.shutdown();server.server_close();thread.join(2)
        self.c.shutdown();self.c.close()

    def start_record(self):
        self.c.handle_command('record/start')
        self.assertTrue(wait_for(lambda:self.c.status()['state']=='recording'))
        return self.c._record_preview

    def server(self):
        server=panel.PanelHTTPServer(('127.0.0.1',0),self.c)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.servers.append((server,thread));return server.server_address[1]

    def request(self,path,token='test-token'):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        hdr={} if token is None else {'X-USV-Token':token}
        conn.request('GET',path,headers=hdr)
        res=conn.getresponse();data=res.read();conn.close();return res.status,data

    def put_frame(self,app,key='cam_left',age=0):
        mono=time.monotonic_ns()-int(age*1e9)
        live.frame_path(app.directory,key).write_bytes(live.HEADER.pack(live.MAGIC,3,mono)+JPEG)

    def test_panel_passes_private_root_and_wrapper_forwards(self):
        app=self.start_record();argv=self.runners[0].argv
        self.assertEqual(argv[argv.index('--preview-root')+1],str(app.directory))
        args=dual.build_parser().parse_args(['--preview-root',str(app.directory)])
        for spec in dual.CAMERAS:
            cmd=dual.build_recorder_command('python','recorder.py',spec,spec['default_device'],'out',args)
            self.assertEqual(cmd[cmd.index('--preview-root')+1],str(app.directory))

    def test_recording_does_not_open_second_preview_camera(self):
        with mock.patch.object(self.c,'preview_factory',side_effect=AssertionError('second capture')):
            app=self.start_record()
        self.assertIsNotNone(app);self.assertIsNone(self.c._preview_app)

    def test_diagnostic_and_token_while_recording(self):
        app=self.start_record();self.put_frame(app);self.port=self.server()
        code,_=self.request('/diagnostics/frame.jpg?camera=left',token=None);self.assertEqual(code,401)
        code,data=self.request('/diagnostics/frame.jpg?camera=left')
        self.assertEqual(code,200);self.assertEqual(data,JPEG)
        self.assertEqual(self.request('/diagnostics/frame.jpg?camera=right')[0],503)
        self.assertEqual(self.request('/diagnostics/frame.jpg?camera=wrong')[0],404)
        self.assertEqual(self.c.status()['state'],'recording')

    def test_stale_diagnostic_refused_without_stopping_record(self):
        app=self.start_record();self.put_frame(app,age=10);self.port=self.server()
        self.assertEqual(self.request('/diagnostics/frame.jpg?camera=left')[0],503)
        self.assertEqual(self.c.status()['state'],'recording')
        self.assertFalse(self.runners[0].terminated)

    def test_stream_has_jpeg_and_disconnect_does_not_stop_record(self):
        app=self.start_record();self.put_frame(app);self.port=self.server()
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        conn.request('GET','/stream/left.mjpg',headers={'X-USV-Token':'test-token'})
        res=conn.getresponse();self.assertEqual(res.status,200)
        data=res.read(1024);self.assertIn(b'--usvframe',data);self.assertIn(b'\xff\xd8',data)
        conn.close();self.assertFalse(self.runners[0].terminated)
        self.assertEqual(self.c.status()['state'],'recording')

    def test_stream_client_limit(self):
        app=self.start_record();app.max_clients=1;self.port=self.server()
        self.assertTrue(app.acquire_client('left'))
        try:self.assertEqual(self.request('/stream/right.mjpg')[0],503)
        finally:app.release_client('left')

    def test_preview_failure_does_not_prevent_record_start(self):
        with mock.patch.object(live,'RecordPreview',side_effect=OSError('no shm')):
            self.c.handle_command('record/start')
            self.assertTrue(wait_for(lambda:self.c.status()['state']=='recording'))
        self.assertIn('no shm',self.c.status()['recording_preview']['error'])
        self.assertNotIn('--preview-root',self.runners[0].argv)

    def test_finalization_removes_side_channel_and_next_run_is_new(self):
        first=self.start_record();directory=first.directory
        self.c.handle_command('record/stop')
        self.assertTrue(wait_for(lambda:self.c.status()['state']=='idle'))
        self.assertFalse(directory.exists());self.assertTrue(first.stop_event.is_set())
        second=self.start_record();self.assertNotEqual(directory,second.directory)

    def test_shutdown_cleans_preview(self):
        app=self.start_record();self.c.shutdown()
        self.assertFalse(app.directory.exists());self.assertTrue(self.runners[0].terminated)

    def test_page_selects_stream_source_not_second_capture(self):
        self.assertIn('wantedSource !== streamSource',panel.PANEL_HTML)
        self.assertIn('status.recording_preview.active',panel.PANEL_HTML)
        self.assertIn('до 2 кадр/с',panel.PANEL_HTML)


class RecorderIsolationTests(unittest.TestCase):
    def test_publisher_exception_does_not_change_verified_result(self):
        helper=recorder_helpers.RecorderProfileTests();helper.setUp();rec=helper.rec
        rec.stop_requested=False
        mode=profiles.require_mode('1280x720@30')
        stream=SimpleNamespace(codec_context=SimpleNamespace(name='mjpeg',width=1280,height=720),time_base=Fraction(1,30))
        def packets():
            for _ in range(3):yield recorder_helpers.Packet(JPEG)
            rec.stop_requested=True;yield recorder_helpers.Packet(JPEG)
        camera=mock.MagicMock();camera.__enter__.return_value=camera
        camera.streams.video=[stream];camera.demux.side_effect=lambda st:packets()
        def writer(buffer,done,failed,stream,folder,args,result):
            while True:
                try:buffer.get();result['written_packets']+=1
                except queue.Empty:
                    if done.is_set():return
        class BrokenPublisher:
            error=None
            def __init__(self,*args):pass
            def start(self):pass
            def offer(self,*args):raise OSError('preview-only failure')
            def close(self):pass
            def snapshot(self):return {'enabled':False,'error':self.error}
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'camera'
            args=['recorder','--output-dir',str(folder),'--seconds','0','--fps','30','--preview-root',temp]
            with mock.patch.object(sys,'argv',args),mock.patch.object(rec.signal,'signal'), \
                 mock.patch.object(rec.preflight,'verify_exposure',return_value={}), \
                 mock.patch.object(rec.av,'open',create=True,return_value=camera), \
                 mock.patch.object(rec,'write_packets',side_effect=writer), \
                 mock.patch.object(rec,'verify',return_value=(3,[{'payload_match':True,'pts_match':True,'keyflags_match':True,'counts_match':True}])), \
                 mock.patch.object(rec.recording_preview,'SnapshotPublisher',BrokenPublisher), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rec.main(),0)
            data=json.loads((folder/'session_metadata.json').read_text())
            self.assertEqual(data['status'],'verified');self.assertEqual(data['accepted_packets'],3)
            self.assertEqual(data['queue_overflows'],0)
            self.assertIn('preview-only failure',data['recording_preview']['error'])


@unittest.skipUnless(shutil.which('node'), 'Node.js отсутствует: проверка JS переходов пропущена')
class BrowserTransitionTests(unittest.TestCase):
    def test_stream_is_reopened_for_recording_and_stopped_at_end(self):
        import subprocess
        script=panel.PANEL_HTML.split('<script>',1)[1].split('</script>',1)[0]
        script=script.replace('__CAMERAS_JSON__',json.dumps([
            {'key':'left','label':'left','device':'/dev/video2'},
            {'key':'right','label':'right','device':'/dev/video4'}]))
        harness=r"""
const vm=require('vm'),assert=require('assert/strict');
class E {
 constructor(){this.children=[];this.style={};this.dataset={};this.value='';
 this.classList={toggle:()=>{},contains:()=>false,add:()=>{}};}
 set textContent(v){this.text=v;this.children=[];} get textContent(){return this.text||'';}
 append(...n){this.children.push(...n);} addEventListener(){} focus(){} removeAttribute(){}
}
const elements={};const document={getElementById:k=>elements[k]||(elements[k]=new E()),createElement:()=>new E()};
const ctx=vm.createContext({document,console,localStorage:{getItem:()=>''},
 setInterval:()=>{},window:{addEventListener:()=>{}},Uint8Array,AbortController,URL,Blob});
vm.runInContext(JSON.parse(require('fs').readFileSync(0,'utf8')).script,ctx);
ctx.calls=[];
ctx.fixture={state:'preview',message:'ok',profile_locked:true,
 profile:{mode:'1280x720@30',exposure_dynamic_framerate:0,duration_seconds:10},
 profile_support:{state:'fake'},profile_options:[],profile_durations:[10],conflicts:[],
 recording:{active:false,session:null,cameras:{},elapsed_s:null,exit_code:null},
 preview:{cameras:{}},disk:{free_mib:10000},limits:{min_free_mib:4096,critical_free_mib:1024,preview_publish_fps:8}};
vm.runInContext("startStream=k=>calls.push('start:'+k);stopStream=k=>calls.push('stop:'+k);lastStatus=fixture;render();",ctx);
assert.equal(vm.runInContext('streamSource',ctx),'preview');
ctx.calls.length=0;ctx.fixture.state='recording';ctx.fixture.recording.active=true;
ctx.fixture.recording_preview={active:true,cameras:{}};
vm.runInContext('render();',ctx);
assert.equal(vm.runInContext('streamSource',ctx),'recorder');
assert.deepEqual(Array.from(ctx.calls),['stop:left','stop:right','start:left','start:right']);
ctx.calls.length=0;ctx.fixture.state='idle';ctx.fixture.recording.active=false;
ctx.fixture.recording_preview.active=false;
vm.runInContext('render();',ctx);
assert.equal(vm.runInContext('streamSource',ctx),'');
assert.ok(!ctx.calls.some(x=>x.startsWith('start:')));
"""
        result=subprocess.run([shutil.which('node'),'-e',harness],input=json.dumps({'script':script}),
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
