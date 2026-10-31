import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tradingx_monitor.client import FetchError
from tradingx_monitor.config import Config
from tradingx_monitor.qq_push import QQQueue, load_settings, save_settings
from tradingx_monitor.runner import run
from tradingx_monitor.store import Store, now
from tradingx_monitor.videos import VideoClient, VideoMonitor, format_video


def course(course_id=157, **changes):
    value={'id':course_id,'number':105,'title':'比特币思路更新','description':'26年10月22日 · 第一百零五期',
        'category':'indicator','contentType':'video','duration':'6:09','youtubeId':'','hasStreamVideo':True,
        'status':'published','createdAt':'2026-10-22 02:51:52','updatedAt':'2026-10-22 03:10:33'}
    value.update(changes)
    return value


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=Config(root=Path(self.temp.name), interval_seconds=60, jitter_ratio=0)
        self.store=Store(self.config)
        self.videos=VideoMonitor(self.store)

    def tearDown(self):self.temp.cleanup()

    def normalize(self, raw=None, streams=None):
        client=Mock();client._get.side_effect=[{'ok':True,'courses':raw or [course()]},{'episodes':streams or []}]
        return VideoClient(self.config,client).fetch()

    def test_catalog_matches_playable_video_and_companion_rules_and_uses_real_course_id(self):
        raw=[course(),course(153,number=0,category='kline',hasStreamVideo=False,youtubeId='public-youtube-id'),
            course(100,contentType='article',hasStreamVideo=False),course(101,hasStreamVideo=False),
            course(102,status='draft')]
        rows=self.normalize(raw,[{'id':100,'access_level':'plus_pro'}])
        self.assertEqual({v['id'] for v in rows},{157,153,100})
        self.assertEqual(rows[0]['url'],'https://wall-street-skill.com/video/157')
        self.assertEqual(rows[-1]['url'],'https://wall-street-skill.com/article/100')
        self.assertEqual(rows[0]['published_date'],'2026-10-22')
        message=format_video(rows[0],test=True)
        self.assertIn('测试（现有视频）',message)
        self.assertIn('第 105 期',message)
        self.assertIn('/video/157',message)

    def test_initial_baseline_is_quiet_and_only_content_changes_generate_events(self):
        rows=self.normalize()
        self.assertTrue(self.videos.apply(rows)['baseline'])
        self.assertEqual([e['type'] for e in self.store.events()],['video_baseline'])
        self.assertEqual(self.videos.apply(rows)['new'],0)
        changed=copy.deepcopy(rows);changed[0]['updated_at']='2026-10-23 12:00:00'
        self.assertEqual(self.videos.apply(changed)['updated'],0)
        changed[0]['title']='修改后的视频标题'
        self.assertEqual(self.videos.apply(changed)['updated'],1)
        self.assertEqual(self.videos.apply(changed)['updated'],0)
        self.assertEqual([e['type'] for e in self.store.events()],['video_updated','video_baseline'])

    def test_failed_second_endpoint_keeps_previous_snapshot_and_baseline(self):
        self.videos.apply(self.normalize())
        client=Mock();client._get.side_effect=[{'ok':True,'courses':[course(158)]},FetchError('network_error','无法连接')]
        result,error=self.videos.poll(VideoClient(self.config,client))
        self.assertIsNone(result)
        self.assertEqual(error.code,'network_error')
        self.assertEqual(self.videos.status()['stored_videos'],1)
        self.assertEqual(self.videos.listing()[0]['id'],157)
        self.assertFalse(self.videos.status()['healthy'])
        self.assertEqual([e['type'] for e in self.store.events()],['video_baseline'])

    def test_duplicate_ids_are_rejected_and_empty_baseline_does_not_hide_first_video(self):
        with self.assertRaises(FetchError):self.normalize([course(),course()])
        self.videos.apply([])
        self.assertEqual(self.videos.apply(self.normalize())['new'],1)
        self.assertEqual(self.store.events()[0]['type'],'video_new')

    def test_new_and_edited_video_queue_is_durable_deduplicated_and_skips_baseline(self):
        self.videos.apply(self.normalize())
        save_settings(self.config,{'appid':'123456','target_group_number':'123456789','target_group_name':'测试群',
            'group_openid':'','enabled':False,'events':['new_post','updated','video_new','video_updated']})
        queue=QQQueue(self.store);queue.bind('opaque_group',queue.binding_command())
        queue.enqueue();self.assertEqual(queue.status()['outbox'],{})
        rows=self.normalize([course(),course(158,number=106,title='最新视频')])
        self.videos.apply(rows);queue.enqueue();queue.enqueue()
        self.assertEqual(queue.status()['outbox'],{'pending':1})
        api=Mock();api.send.return_value='video-ack'
        self.assertTrue(queue.deliver_one(api))
        self.assertIn('【新视频上线】',api.send.call_args.args[1])
        self.assertIn('第 106 期',api.send.call_args.args[1])
        self.assertEqual(queue.status()['sent_videos'],1)
        self.assertEqual(queue.status()['sent_text'],0)
        self.assertFalse(queue.deliver_one(api))
        rows[1]['duration']='7:00';self.videos.apply(rows);queue.enqueue()
        self.assertEqual(queue.status()['outbox'],{'sent':1,'pending':1})
        self.assertTrue(queue.deliver_one(api))
        self.assertIn('【视频信息更新】',api.send.call_args.args[1])

    def test_video_test_is_marked_as_test_and_does_not_create_a_new_video_event(self):
        self.videos.apply(self.normalize())
        save_settings(self.config,{'appid':'123456','target_group_number':'123456789','target_group_name':'测试群',
            'group_openid':'opaque_group','enabled':True,'events':['video_new']})
        queue=QQQueue(self.store);api=Mock();api.send.return_value='test-ack'
        self.assertEqual(queue.send_video_test(api),'test-ack')
        self.assertIn('视频通知测试（现有视频）',api.send.call_args.args[1])
        self.assertIsNotNone(queue.status()['video_last_test'])
        self.assertEqual([e['type'] for e in self.store.events()],['video_baseline'])

    def test_tradingx_backoff_does_not_starve_independent_video_checks(self):
        clock=[time.time()];due=[clock[0]]
        class StopAfterTwoWaits:
            stopped=False
            waits=0
            def is_set(self):return self.stopped
            def set(self):self.stopped=True
            def wait(self,seconds):
                clock[0]+=seconds;self.waits+=1
                if self.waits>=2:self.stopped=True
        event=StopAfterTwoWaits()
        video_monitor=Mock();video_monitor.status.side_effect=lambda:{'next_check':due[0]}
        def poll_video():due[0]=clock[0]+60;return {},None
        video_monitor.poll.side_effect=poll_video
        self.store.failure(FetchError('network_error','无法连接'),now())
        with patch('tradingx_monitor.videos.VideoMonitor',return_value=video_monitor), \
             patch('tradingx_monitor.runner.threading.Event',return_value=event), \
             patch('tradingx_monitor.runner.signal.signal'), \
             patch('tradingx_monitor.runner.time.time',side_effect=lambda:clock[0]), \
             patch('tradingx_monitor.runner.collect',return_value=(None,FetchError('network_error','无法连接'))) as collect:
            run(self.config,self.store,no_dashboard=True)
        self.assertEqual(collect.call_count,1)
        self.assertEqual(video_monitor.poll.call_count,2)


if __name__=='__main__':unittest.main()
