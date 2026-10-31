import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock,Mock,patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from tradingx_monitor.config import Config
from tradingx_monitor.client import FetchError
from tradingx_monitor.store import Store,now,encode
from tradingx_monitor.qq_push import QQAPI,QQError,QQQueue,save_settings
from tradingx_monitor.video_downloads import VideoDownloads,VideoDownloadError,localize_playlist,prepare_playlists,trusted_stream_url
from tradingx_monitor.web import make_server


BASE='https://customer-example.cloudflarestream.com/fake-playback-token/manifest/video.m3u8'
MEDIA='#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:10.0,\nsegment.m4s\n#EXT-X-ENDLIST\n'
VIDEO={'id':158,'number':106,'title':'测试课程','duration':'0:10','category':'indicator','content_type':'video','url':'https://wall-street-skill.com/video/158'}
CONTENT=b'complete-video-test-content'*200


class VideoDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.config=Config(root=Path(self.temp.name),port=0)
        self.store=Store(self.config)
        save_settings(self.config,{'appid':'123456','target_group_number':'123456789','target_group_name':'测试群',
            'group_openid':'opaque_group','enabled':True,'send_video_files':True,'events':['new_post','updated','video_new','video_updated']})
        self.downloads=VideoDownloads(self.store)
        self.config.token_file.write_text('fake-member-token');self.config.token_file.chmod(0o600)

    def tearDown(self):self.temp.cleanup()

    def add_job(self):
        with self.store.connect() as db:self.store._event(db,now(),'video_new',video=VIDEO)
        self.downloads.enqueue()

    def complete(self):
        def mux(arguments,job,duration,output):output.write_bytes(CONTENT)
        def playlists(master,url,folder,hint):return folder/'master.m3u8',10.0,True
        with patch('tradingx_monitor.video_downloads.Client') as client, \
             patch('tradingx_monitor.video_downloads.stream_manifest',return_value='#EXTM3U'), \
             patch('tradingx_monitor.video_downloads.prepare_playlists',side_effect=playlists), \
             patch.object(self.downloads,'_ffmpeg',side_effect=mux) as ffmpeg, \
             patch('tradingx_monitor.video_downloads.probe_video',return_value={'duration':10.0,'width':1280,'height':720,'audio':True}):
            client.return_value._get.return_value={'cfStreamId':'a'*32,'signedToken':'private-playback-token-for-test'}
            self.assertTrue(self.downloads.download_one())
            return ffmpeg.call_count

    def prepare_file(self):
        self.downloads.enqueue() # first activation skips old history
        self.add_job();self.assertEqual(self.complete(),1)

    def test_private_playlists_keep_audio_choose_quality_and_reject_unsafe_urls_or_drm(self):
        master='#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100000,AVERAGE-BANDWIDTH=80000,RESOLUTION=1280x720,AUDIO="a",CODECS="avc1,mp4a"\n720.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=50000,RESOLUTION=640x360,AUDIO="a"\n360.m3u8\n'
        folder=self.config.root/'playlists';folder.mkdir()
        path,duration,audio=prepare_playlists(master,BASE,folder,10,fetch=lambda _:MEDIA)
        self.assertEqual((duration,audio),(10.0,True))
        self.assertIn('RESOLUTION=1280x720',path.read_text())
        self.assertIn('URI="audio.m3u8"',path.read_text())
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        for url in ('file:///etc/passwd','http://example.com/test','https://127.0.0.1/test','https://user:password@x.cloudflarestream.com/x'):
            with self.assertRaises(VideoDownloadError):trusted_stream_url(url)
        with self.assertRaises(VideoDownloadError):localize_playlist(MEDIA.replace('segment.m4s','file:///etc/passwd'),BASE)
        with self.assertRaises(VideoDownloadError):localize_playlist(MEDIA+'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n',BASE)
        with self.assertRaises(VideoDownloadError):localize_playlist(MEDIA.replace('#EXT-X-ENDLIST',''),BASE)

    def test_baseline_skips_history_jobs_are_deduplicated_and_cached_stream_is_not_downloaded_twice(self):
        with self.store.connect() as db:self.store._event(db,now(),'video_new',video=VIDEO)
        self.downloads.enqueue();self.assertEqual(self.downloads.status()['jobs'],{})
        self.add_job();self.downloads.enqueue()
        self.assertEqual(self.downloads.status()['jobs'],{'pending':1})
        self.assertEqual(self.complete(),1)
        self.add_job()
        self.assertEqual(self.complete(),0)
        status=self.downloads.status()
        self.assertEqual(status['downloaded_files'],1)
        self.assertEqual(status['jobs'],{'ready':2})
        self.assertNotIn('private-playback-token',json.dumps(status))
        self.assertEqual(list(self.downloads.folder.glob('.job-*')),[])

    def test_download_failure_is_sanitized_backed_off_and_does_not_publish_a_ready_file(self):
        self.downloads.enqueue();self.add_job()
        with patch('tradingx_monitor.video_downloads.Client') as client:
            client.return_value._get.side_effect=FetchError('auth_required','private-sensitive-error')
            self.assertFalse(self.downloads.download_one())
        status=self.downloads.status()
        self.assertEqual(status['jobs'],{'failed':1})
        self.assertEqual(status['latest_job']['error_code'],'auth_required')
        self.assertEqual(status['downloaded_files'],0)
        self.assertNotIn('private-sensitive-error',json.dumps(status))
        self.assertFalse(self.downloads.download_one())

    def test_incomplete_audio_or_duration_does_not_become_a_ready_file(self):
        self.downloads.enqueue();self.add_job()
        with patch('tradingx_monitor.video_downloads.Client') as client, \
             patch('tradingx_monitor.video_downloads.stream_manifest',return_value='#EXTM3U'), \
             patch('tradingx_monitor.video_downloads.prepare_playlists',return_value=(self.config.root/'list.m3u8',10,True)), \
             patch.object(self.downloads,'_ffmpeg',side_effect=lambda args,job,duration,output:output.write_bytes(CONTENT)), \
             patch('tradingx_monitor.video_downloads.probe_video',return_value={'duration':1,'width':1280,'height':720,'audio':False}):
            client.return_value._get.return_value={'cfStreamId':'a'*32,'signedToken':'private-playback-token-for-test'}
            self.assertFalse(self.downloads.download_one())
        self.assertEqual(self.downloads.status()['latest_job']['error_code'],'incomplete_video')
        self.assertEqual(self.downloads.status()['downloaded_files'],0)

    def test_ready_file_is_reconciled_after_cursor_advance_and_sent_only_once(self):
        self.prepare_file();queue=QQQueue(self.store)
        with self.store.connect() as db:self.store._update(db,qq_event_cursor=db.execute('SELECT MAX(id) FROM events').fetchone()[0])
        queue.enqueue();queue.enqueue()
        self.assertEqual(queue.status()['video_file_outbox'],{'pending':1})
        api=Mock();api.send_video.return_value='video-file-ack'
        self.assertTrue(queue.deliver_one(api));self.assertFalse(queue.deliver_one(api))
        self.assertEqual(api.send_video.call_count,1)
        self.assertEqual(api.send_video.call_args.args[1],'第106期_测试课程.mp4')
        self.assertEqual(api.send_video.call_args.args[2].read_bytes(),CONTENT)
        self.assertEqual(self.downloads.status()['latest_file']['display_name'],'第106期_测试课程.mp4')
        self.assertEqual(queue.status()['sent_video_files'],1)

    def test_definite_video_upload_failure_does_not_stop_text_but_uncertain_send_stops_queue(self):
        self.prepare_file();queue=QQQueue(self.store)
        with self.store.connect() as db:self.store._update(db,qq_event_cursor=db.execute('SELECT MAX(id) FROM events').fetchone()[0])
        queue.enqueue()
        with self.store.connect() as db:db.execute("INSERT INTO qq_outbox(event_id,part,group_openid,content,updated_at) VALUES(9999,1,'opaque_group','later notice',?)",(now(),))
        api=Mock();api.send_video.side_effect=QQError('video_part_upload_failed');api.send.return_value='text-ack'
        self.assertFalse(queue.deliver_one(api));self.assertTrue(queue.deliver_one(api))
        self.assertEqual(queue.status()['video_file_error'],'video_part_upload_failed')
        with self.store.connect() as db:db.execute("UPDATE qq_outbox SET status='pending' WHERE message_type='video_file'")
        api.send_video.side_effect=QQError('network_error',True)
        self.assertFalse(queue.deliver_one(api));self.assertFalse(queue.deliver_one(api))
        self.assertEqual(queue.status()['video_file_outbox'],{'uncertain':1})

    def test_video_upload_streams_chunks_without_authorization_to_storage_and_checks_ack(self):
        path=self.config.root/'movie.mp4';data=b'x'*3_600_000;path.write_bytes(data)
        prepared={'upload_id':'upload-video','block_size':'2000000','parts':[{'index':0,'presigned_url':'https://bucket.cos.ap-guangzhou.myqcloud.com/one'},{'index':1,'presigned_url':'https://bucket.cos.ap-guangzhou.myqcloud.com/two'}]}
        opener=MagicMock();opener.open.return_value.__enter__.return_value.read.side_effect=[json.dumps(prepared).encode(),b'',b'{}',b'',b'{}',b'{"file_info":"video-info"}',b'{"id":"video-message-ack"}']
        api=QQAPI(self.config,opener=opener);api.token='fake-qq-token';api.token_until=10**12
        self.assertEqual(api.send_video('opaque_group','episode.mp4',path),'video-message-ack')
        requests=[c.args[0] for c in opener.open.call_args_list]
        puts=[r for r in requests if r.method=='PUT']
        self.assertEqual(b''.join(r.data for r in puts),data)
        self.assertTrue(all('Authorization' not in r.headers for r in puts))
        prepare=json.loads(requests[0].data)
        self.assertEqual(prepare['file_type'],2)
        self.assertEqual(prepare['md5'],hashlib.md5(data).hexdigest())
        self.assertEqual(json.loads(requests[-1].data)['media'],{'file_info':'video-info'})

    def test_actual_one_based_upload_parts_are_preserved_in_completion_requests(self):
        path=self.config.root/'movie.mp4';path.write_bytes(b'x'*3_600_000)
        prepared={'upload_id':'upload-video','block_size':'2000000','parts':[{'index':1,'presigned_url':'https://bucket.cos.ap-guangzhou.myqcloud.com/one'},{'index':2,'presigned_url':'https://bucket.cos.ap-guangzhou.myqcloud.com/two'}]}
        opener=MagicMock();opener.open.return_value.__enter__.return_value.read.side_effect=[json.dumps(prepared).encode(),b'',b'{}',b'',b'{}',b'{"file_info":"video-info"}',b'{"id":"one-based-ack"}']
        api=QQAPI(self.config,opener=opener);api.token='fake-qq-token';api.token_until=10**12
        self.assertEqual(api.send_video('opaque_group','episode.mp4',path),'one-based-ack')
        requests=[c.args[0] for c in opener.open.call_args_list]
        self.assertEqual([json.loads(r.data)['part_index'] for r in requests if r.full_url.endswith('/upload_part_finish')],[1,2])

    def test_large_video_is_sent_as_complete_mp4_file_type(self):
        path=self.config.root/'large.mp4'
        with path.open('wb') as handle:handle.truncate(30_000_001)
        api=QQAPI(self.config);api._request=Mock(side_effect=QQError('stop_before_upload'))
        with self.assertRaises(QQError):api.send_video('opaque_group','large.mp4',path)
        self.assertEqual(api._request.call_args.args[1]['file_type'],4)

    def test_local_mp4_endpoint_supports_range_and_blocks_unknown_file_keys(self):
        self.prepare_file();file=self.downloads.status()['latest_file']
        server=make_server(self.config,self.store);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(Request(base+file['local_url'],headers={'Range':'bytes=0-31'})) as response:
                self.assertEqual(response.status,206);self.assertEqual(response.read(),CONTENT[:32])
                self.assertEqual(response.headers['Content-Type'],'video/mp4')
            with urlopen(Request(base+file['local_url'],headers={'Range':'bytes=-4'})) as response:self.assertEqual(response.read(),CONTENT[-4:])
            with self.assertRaises(HTTPError) as e:urlopen(Request(base+file['local_url'],headers={'Range':'bytes=999999999-'}))
            self.assertEqual(e.exception.code,416)
            with self.assertRaises(HTTPError) as e:urlopen(base+'/api/video-files/../../secrets/token')
            self.assertEqual(e.exception.code,404)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
