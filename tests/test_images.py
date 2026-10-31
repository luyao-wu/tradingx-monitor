import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from tradingx_monitor.client import FetchError
from tradingx_monitor.config import Config
from tradingx_monitor.images import ImageArchive, original_url
from tradingx_monitor.qq_push import QQAPI, QQError, QQQueue, save_settings
from tradingx_monitor.store import Store, now
from tradingx_monitor.web import make_server
from test_monitor import post, snapshot


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')


def pictured(text='完整正文'):
    return post(1, text, images=[{'id':'image-a','url':'/api/tradingx-images?post=1&id=image-a'}])


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), port=0)
        self.store = Store(self.config)
        self.config.token_file.write_text('fake-member-token');self.config.token_file.chmod(0o600)
        self.opener = MagicMock()
        self.opener.open.return_value.__enter__.return_value.read.return_value = PNG
        self.archive = ImageArchive(self.store, self.opener)

    def tearDown(self):
        self.temp.cleanup()

    def test_original_bytes_cached_with_local_permissions_and_tampering_is_detected(self):
        self.assertEqual(self.archive.sync([pictured()])['downloaded'],1)
        self.assertEqual(self.archive.sync([pictured()])['cached'],1)
        self.opener.open.assert_called_once()
        request=self.opener.open.call_args.args[0]
        self.assertEqual(request.headers['Authorization'],'Bearer fake-member-token')
        record=self.archive.decorate([pictured()])[0]['images'][0]
        asset=self.archive.read(record['localUrl'].split('/')[-1])
        self.assertEqual(asset[0],PNG)
        filename=self.archive.folder/asset[2]
        self.assertEqual(filename.stat().st_mode&0o777,0o600)
        filename.write_bytes(b'changed')
        self.assertIsNone(self.archive.read(record['localUrl'].split('/')[-1]))
        self.assertIsNone(self.archive.read('../../secrets/token'))

    def test_off_origin_and_mismatched_images_rejected_before_attaching_credentials(self):
        for url in ('https://example.com/api/tradingx-images?post=1&id=image-a',
                    '//example.com/api/tradingx-images?post=1&id=image-a',
                    '/api/tradingx-images?post=2&id=image-a',
                    '/api/auth/profile?post=1&id=image-a'):
            with self.assertRaises(FetchError):original_url(1,{'id':'image-a','url':url})
        self.archive.sync([pictured(None)])
        self.opener.open.assert_not_called()

    def test_html_and_oversized_payloads_are_not_saved_and_auth_failures_back_off(self):
        self.opener.open.return_value.__enter__.return_value.read.return_value=b'<html>login page</html>'
        self.assertEqual(self.archive.sync([pictured()])['failed'],1)
        self.assertEqual(list(self.archive.folder.iterdir()),[])
        self.assertEqual(self.archive.sync([pictured()])['deferred'],1)
        with self.store.connect() as db:db.execute('UPDATE image_assets SET retry_at=0')
        self.opener.open.return_value.__enter__.return_value.read.return_value=PNG
        with patch('tradingx_monitor.images.MAX_IMAGE_BYTES',16):
            self.assertEqual(self.archive.sync([pictured()])['failed'],1)
        with self.store.connect() as db:db.execute('UPDATE image_assets SET retry_at=0')
        self.opener.open.side_effect=HTTPError('https://wall-street-skill.com/api/tradingx-images',401,'private',{},BytesIO(b'secret'))
        self.assertEqual(self.archive.sync([pictured()])['failed'],1)
        self.assertNotIn('secret',json.dumps(self.archive.status()))

    def test_local_image_endpoint_serves_original_and_blocks_path_traversal(self):
        self.store.success(snapshot([pictured()],viewer='member'),now())
        self.archive.sync([pictured()])
        server=make_server(self.config,self.store)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(base+'/api/posts') as response:
                image=json.load(response)['posts'][0]['images'][0]
            with urlopen(base+image['localUrl']) as response:
                self.assertEqual(response.headers['Content-Type'],'image/png')
                self.assertEqual(response.read(),PNG)
            with self.assertRaises(HTTPError) as error:urlopen(base+'/api/images/../../secrets/token')
            self.assertEqual(error.exception.code,404)
        finally:server.shutdown();server.server_close();thread.join()

    def test_new_image_waits_for_download_and_unchanged_image_is_not_reposted_on_edit(self):
        save_settings(self.config,{'appid':'123456','target_group_number':'123456789','target_group_name':'测试群',
            'enabled':False,'group_openid':'','events':['new_post','updated']})
        self.store.success(snapshot([],viewer='member'),now())
        queue=QQQueue(self.store)
        queue.bind('opaque_group',queue.binding_command())
        self.store.success(snapshot([pictured()],viewer='member'),now())
        queue.enqueue();queue.enqueue()
        self.assertEqual(queue.status()['outbox'],{'pending':2})
        api=Mock();api.send.return_value='text-ack';api.send_image.return_value='image-ack'
        self.assertTrue(queue.deliver_one(api))
        self.assertFalse(queue.deliver_one(api))
        api.send_image.assert_not_called()
        self.archive.sync([pictured()])
        self.assertTrue(queue.deliver_one(api))
        self.assertEqual(api.send_image.call_args.args[2],PNG)
        self.store.success(snapshot([pictured('编辑正文')],viewer='member'),now())
        queue.enqueue()
        self.assertEqual(queue.status()['outbox'],{'sent':2,'pending':1})
        self.assertEqual(queue.enqueue_images(1),0)

    def test_qq_upload_is_separate_from_send_and_contains_bytes_without_member_credentials(self):
        settings={'appid':'123456','target_group_number':'123456789'}
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.side_effect=[b'{"file_info":"uploaded-info"}',b'{"id":"image-message-ack"}']
        api=QQAPI(self.config,settings,opener);api.token='fake-qq-token';api.token_until=10**12
        self.assertEqual(api.send_image('opaque_group','image.png',PNG),'image-message-ack')
        requests=[c.args[0] for c in opener.open.call_args_list]
        upload=json.loads(requests[0].data);send=json.loads(requests[1].data)
        self.assertFalse(upload['srv_send_msg'])
        self.assertEqual(base64.b64decode(upload['file_data']),PNG)
        self.assertEqual(send['msg_type'],7)
        self.assertEqual(send['media'],{'file_info':'uploaded-info'})
        self.assertNotIn('fake-member-token',str([r.headers for r in requests]))

    def test_large_image_chunks_have_no_authorization_header_and_reject_untrusted_upload_host(self):
        settings={'appid':'123456','target_group_number':'123456789'}
        prepared={'upload_id':'upload-test','block_size':'2000000','parts':[
            {'index':0,'presigned_url':'https://bucket.cos.ap-guangzhou.myqcloud.com/part0'},
            {'index':1,'presigned_url':'https://bucket.cos.ap-guangzhou.myqcloud.com/part1'}]}
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.side_effect=[json.dumps(prepared).encode(),b'',b'{}',b'',b'{}',b'{"file_info":"large-info"}',b'{"id":"large-ack"}']
        api=QQAPI(self.config,settings,opener);api.token='fake-qq-token';api.token_until=10**12
        image=PNG+b'x'*3_500_000
        self.assertEqual(api.send_image('opaque_group','large.png',image),'large-ack')
        puts=[c.args[0] for c in opener.open.call_args_list if c.args[0].method=='PUT']
        self.assertEqual(b''.join(r.data for r in puts),image)
        self.assertTrue(all('Authorization' not in r.headers for r in puts))
        prepared['parts'][0]['presigned_url']='https://127.0.0.1/private'
        opener=MagicMock();opener.open.return_value.__enter__.return_value.read.return_value=json.dumps(prepared).encode()
        api=QQAPI(self.config,settings,opener);api.token='fake';api.token_until=10**12
        with self.assertRaises(QQError):api.send_image('opaque_group','large.png',image)
        self.assertEqual(opener.open.call_count,1)


if __name__=='__main__':unittest.main()
