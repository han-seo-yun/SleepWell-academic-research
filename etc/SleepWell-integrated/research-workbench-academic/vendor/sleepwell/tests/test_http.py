import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Application, Store, Handler
from engine import ZONE


class HTTPTests(unittest.TestCase):
    def test_enrollment_record_feedback_export_and_csrf(self):
        class Research:
            summary={"available":False};records=[];metrics=None
            def behavioral_forecast(self,*args):return {"available":False}
        with tempfile.TemporaryDirectory() as td:
            app=Application(Store(Path(td)/"db.sqlite3"),Research())
            server=ThreadingHTTPServer(("127.0.0.1",0),Handler);server.application=app
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            root=f"http://127.0.0.1:{server.server_port}"
            def call(path,data=None,token=None):
                headers={}
                if data is not None:headers={"Content-Type":"application/json","X-SleepWell-Token":token or app.token}
                with urlopen(Request(root+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)) as r:return json.load(r)
            try:
                self.assertIsNone(call("/api/state")["profile"])
                self.assertEqual(call("/api/state")["model_status"]["status"], "unavailable")
                self.assertEqual(call("/api/state")["data_readiness"]["status"], "not_generated")
                with self.assertRaises(HTTPError) as error:call("/api/recommend",{},"wrong")
                self.assertEqual(error.exception.code,403)
                payload={"name":"TEST", "age":20,"occupation":"student", "usual_bed":"23:30", "usual_wake":"07:00", "target_wake":"07:00","weekend_wake":"08:00","usual_sleep_h":7,"desired_sleep_h":8,"average_steps":6000,"exercise_min":30,"latency_min":20,"link_history":False}
                enrolled=call("/api/profile",payload)
                self.assertEqual(enrolled["prediction_count"],1)
                now=datetime.now(ZONE)
                updated=call("/api/record",{"bed":(now-timedelta(hours=10)).isoformat(),"wake":(now-timedelta(hours=2)).isoformat(),"rested":4,"fatigue":3})
                self.assertEqual(len(updated["records"]),1)
                self.assertEqual(updated["prediction_count"],2)
                self.assertIsNone(updated["feedback"][0]["recommendation_id"])
                backup=call("/api/export")
                self.assertNotIn("csrf_token",backup)
                self.assertEqual(len(backup["predictions"]),2)
                self.assertEqual(len(backup["change_events"]),2)
                with urlopen(root+"/") as r:
                    html=r.read().decode()
                    self.assertIn('id="profile-form"',html)
                    self.assertIn("frame-ancestors 'none'",r.headers["Content-Security-Policy"])
            finally:server.shutdown();server.server_close();worker.join()


if __name__=="__main__":unittest.main()
