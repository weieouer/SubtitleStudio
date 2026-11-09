"""隐藏启动后的HTTP日志和日志轮询边界，不加载真实模型。"""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import app
import launch


class StartupTests(unittest.TestCase):
    def test_logging_before_request_line_is_parsed(self):
        handler = object.__new__(app.Handler)
        with patch.object(app.log, 'info') as logger:
            handler.log_message('Request timed out: %r', TimeoutError())
            logger.assert_called_once()

    def test_health_and_log_api_work_without_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            logfile=Path(folder)/'app.log';logfile.write_text('ready\n',encoding='utf-8')
            server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch.object(sys,'stderr',None),patch.object(app,'LOG_FILE',logfile), \
                     patch.object(app,'list_models',return_value=[]),patch.object(app,'list_auxiliary',return_value=[]):
                    for path in ('/api/health','/api/logs?since=0'):
                        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                        connection.request('GET',path,headers={'X-SubtitleStudio-Token':app.SESSION_TOKEN})
                        response=connection.getresponse();data=json.loads(response.read());connection.close()
                        self.assertEqual(response.status,200);self.assertTrue(data['ok'])
            finally:
                server.shutdown();server.server_close();thread.join(3)

    def test_log_tail_incremental_and_file_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'log'
            path.write_text('old\n',encoding='utf-8');first=app.read_log_tail(path)
            with path.open('a',encoding='utf-8') as out:out.write('new\n')
            self.assertEqual(app.read_log_tail(path,first['offset'])['lines'],['new'])
            path.write_text('x\n',encoding='utf-8')
            self.assertEqual(app.read_log_tail(path,first['offset'])['lines'],['x'])
            self.assertEqual(app.read_log_tail(path,path.stat().st_size)['lines'],[])

    def test_log_tail_is_bounded_and_self_polling_does_not_add_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'log';path.write_bytes(b'large-line\n'*100000)
            result=app.read_log_tail(path,0,3)
            self.assertEqual(result['lines'],['large-line']*3)
            self.assertEqual(result['offset'],path.stat().st_size)
        handler=object.__new__(app.Handler);handler.path='/api/logs?since=10';handler.command='GET'
        with patch.object(app.log,'info') as logger:handler.log_message('%s','request');logger.assert_not_called()

    def test_launcher_does_not_probe_arbitrary_ready_urls(self):
        self.assertFalse(launch.verify_ready({'url':'http://example.invalid/'}))


if __name__=='__main__':unittest.main()
