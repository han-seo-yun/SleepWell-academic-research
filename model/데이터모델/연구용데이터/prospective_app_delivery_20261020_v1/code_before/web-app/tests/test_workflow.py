import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import ZONE, validate_profile, validate_record, parameters, recommend, clock_delta
from server import Store, Application


def profile(now):
    p = validate_profile({"name":"검증용 사용자", "age":17, "occupation":"student", "usual_bed":"23:30",
        "usual_wake":"07:00", "target_wake":"07:00", "weekend_wake":"08:00", "usual_sleep_h":7,
        "desired_sleep_h":8.5, "average_steps":6000, "exercise_min":30, "latency_min":20, "link_history":False})
    p["created_at"] = now.isoformat()
    return p


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026,9,1,12,tzinfo=ZONE)
        self.p = profile(self.start)

    def records(self, count):
        output=[]
        for i in range(count):
            wake=self.start.replace(hour=8)+timedelta(days=i+1)
            output.append(validate_record({"bed":(wake-timedelta(hours=9)).isoformat(), "wake":wake.isoformat(),
                "latency_min":15, "awake_min":5, "rested":5,"fatigue":2,"steps":8000,"exercise_min":45},wake+timedelta(hours=2)))
        return output

    def test_first_day_schedule_uses_user_goal(self):
        r=recommend(self.p,[],self.start)
        self.assertEqual(r["parameters"]["mode"],"initial")
        self.assertTrue(r["feasible"])
        self.assertEqual(datetime.fromisoformat(r["wake"]).hour,7)
        self.assertGreaterEqual(r["sleep_opportunity_min"],r["target_sleep_min"]+20)
        self.assertEqual(len(r["chart"]),145)

    def test_day14_requires14_observations_and_updates_with_feedback(self):
        now=self.start+timedelta(days=14)
        self.assertEqual(parameters(self.p,self.records(13),now)["mode"],"initial")
        q=parameters(self.p,self.records(14),now)
        self.assertEqual(q["mode"],"personalized")
        self.assertEqual(q["valid_days"],14)
        self.assertEqual(q["average_steps"],7000)
        self.assertEqual(q["latency_min"],17.5)
        self.assertNotEqual(q["target_sleep_min"],510)
        self.assertEqual(q["tau_wake_h"],18.2)
        later=parameters(self.p,self.records(21),self.start+timedelta(days=21))
        self.assertGreater(later["average_steps"],q["average_steps"])

    def test_archive_and_future_do_not_unlock_personalization(self):
        records=self.records(20)
        for r in records:r["source"]="archive"
        self.assertEqual(parameters(self.p,records,self.start+timedelta(days=20))["valid_days"],0)
        self.assertEqual(parameters(self.p,self.records(20),self.start+timedelta(days=13))["mode"],"initial")

    def test_clock_wrap_and_after_midnight(self):
        self.assertEqual(clock_delta(5,1435),10)
        now=self.start.replace(hour=1)+timedelta(days=1)
        r=recommend(self.p,[],now)
        self.assertEqual(datetime.fromisoformat(r["wake"]).date(),now.date())
        self.assertFalse(r["feasible"])
        self.assertEqual(r["bed"],now.isoformat())

    def test_invalid_future_and_missing_data(self):
        with self.assertRaises(ValueError):validate_record({"bed":self.start.isoformat(),"wake":(self.start+timedelta(hours=8)).isoformat()},self.start)
        r=validate_record({"bed":(self.start-timedelta(hours=9)).isoformat(),"wake":(self.start-timedelta(hours=1)).isoformat()},self.start)
        self.assertIsNone(r["steps"])
        self.assertIsNone(r["rested"])
        bad=dict(self.p,desired_sleep_h=6)
        with self.assertRaises(ValueError):validate_profile(bad)

    def test_persistence_updates_and_input_snapshots(self):
        class Research:
            summary={};records=[];metrics=None
            def behavioral_forecast(self,*args):return {"available":False}
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/"data.db")
            store.save_profile(self.p,[])
            app=Application(store,Research())
            first=app.generate()
            record=self.records(1)[0]
            store.save_record(record)
            record=dict(record,rested=3);store.save_record(record)
            second=app.generate()
            restored=Store(Path(td)/"data.db")
            self.assertEqual(len(restored.records()),1)
            self.assertEqual(restored.records()[0]["rested"],3)
            self.assertEqual(len(restored.predictions()),2)
            self.assertNotEqual(first["input_hash"],second["input_hash"])
            with restored.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0],3)
            json.dumps(app.state(),allow_nan=False)


if __name__ == "__main__":unittest.main()
