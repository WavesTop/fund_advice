from copy import deepcopy
from datetime import date
import json
import math
from pathlib import Path
import unittest

import numpy as np

from backend.analysis.fund_allocation import AllocationState
from backend.analysis.matured_trend_forecast import MaturedForecaster
from backend.analysis.portfolio_increment import EconomicLeaseAllocator, economic_features, initial_target, prepare_economic
from backend.core.trading_calendar import TradingCalendar
from scripts.research_local_trend_models import prepare_local
from scripts.research_portfolio_increments import audit_outcome_differences, economic_trial, outcome_records
from scripts.validate_portfolio_increment import audit_economic_account, raw_context, rebuilt_features


class PortfolioIncrementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(__file__).resolve().parents[1]/"config/research-exchange-calendar-2009-2026-v2.json"
        cls.calendar=TradingCalendar(json.loads(path.read_text()))
        days=[d.isoformat() for d in cls.calendar.sessions(date(2020,1,1),date(2025,1,1))][:820]
        funds={}
        for j,code in enumerate(("a","b","000217","bond","000051")):
            prices=[math.exp(.0003*i+.025*math.sin(i/(9+j))) for i in range(len(days))]
            funds[code]={"code":code,"daily_start":days[0],"last_date":days[-1],
                "rows":{d:{"unit_nav":str(p)} for d,p in zip(days,prices)},"distributions":dict.fromkeys(days,0.),
                "theme":"黄金" if code=="000217" else "other","role":"defensive" if code=="bond" else "sector"}
        cls.base=prepare_economic(prepare_local(funds,["a","b","000217","bond"],cls.calendar))

    def data(self):
        return deepcopy(self.base)

    def test_initial_core_satellite_and_cash_risk_caps(self):
        data=self.data()
        for candidate in (None,"a","b","cash"):
            weights,meta=initial_target(data,650,candidate)
            self.assertLessEqual(sum(weights.values()),1+1e-12)
            self.assertLessEqual(max(weights[c] for c in data["class_groups"]["stock"]),.35)
            self.assertLessEqual(meta["unconstrained_projected_volatility"]*meta["risky_scale"],.1+1e-12)
        with self.assertRaises(ValueError):
            initial_target(data,650,"bond")
        data["economic_stats"][650]["risk"]["signal_index"]=651
        with self.assertRaises(ValueError):
            initial_target(data,650)

    def test_past_features_are_independently_rebuilt(self):
        data=self.data();context=raw_context(data,650)
        for mode in ("state_only","trend"):
            for candidate in ("a","b","cash"):
                np.testing.assert_allclose(economic_features(data,candidate,650,mode),rebuilt_features(data,candidate,650,mode,context=context),atol=1e-10)
        changed=self.data()
        for p in changed["marks"].values():
            for i in range(651,len(p)):
                p[i]*=100
        self.assertEqual(economic_features(data,"a",650,"trend"),economic_features(changed,"a",650,"trend"))

    def test_actual_cycle_waits_for_sale_and_settlement(self):
        result=economic_trial(self.data(),self.calendar,"static_core",start=650,end=730)
        entries=[e for e in result["signal_trace"] if e["action"]=="entry"]
        self.assertEqual([e["signal_index"] for e in entries],[650,673,696,719])
        self.assertEqual(result["economic_validation"]["status"],"passed")
        self.assertTrue(any(e["action"]=="wait_cash" for e in result["daily_checks"]))

    def test_delayed_cycle_does_not_spend_receivables(self):
        result=economic_trial(self.data(),self.calendar,"static_core",start=650,end=730,lag=2)
        entries=[e for e in result["signal_trace"] if e["action"]=="entry"]
        self.assertEqual([e["signal_index"] for e in entries],[650,674,698,722])
        self.assertEqual(result["economic_validation"]["status"],"passed")

    def test_each_control_and_fixed_candidate_have_audited_accounts(self):
        for policy in ("core","static_core","inverse_static"):
            result=economic_trial(self.data(),self.calendar,policy,start=650,end=710)
            self.assertEqual(result["ledger_validation"]["status"],"passed")
            self.assertEqual(result["economic_validation"]["status"],"passed")
        for candidate in ("a","cash"):
            result=economic_trial(self.data(),self.calendar,"core",start=650,end=671,fixed_candidate=candidate)
            self.assertEqual(result["economic_validation"]["status"],"passed")

    def test_mutated_checks_state_and_targets_are_rejected(self):
        data=self.data();result=economic_trial(data,self.calendar,"core",start=650,end=710)
        for field in ("next_entry_after","targets","risk_exit_codes"):
            checks=deepcopy(result["daily_checks"])
            applied=next(e for e in checks if e["targets"] is not None)
            if field=="next_entry_after":
                applied[field]-=1
            elif field=="targets":
                applied[field]["a"]+=.1
            else:
                applied[field]=["invented"]
            with self.assertRaises(ValueError):
                audit_economic_account(result["account"],checks,data,policy="core",start=650)

    def test_removed_daily_safety_check_is_rejected(self):
        data=self.data();result=economic_trial(data,self.calendar,"core",start=650,end=710)
        with self.assertRaises(ValueError):
            audit_economic_account(result["account"],result["daily_checks"][1:],data,policy="core",start=650)

    def test_teacher_labels_equal_candidate_minus_core_accounts(self):
        data=self.data()
        data["days"]=data["days"][:310]
        data["prices"]={c:p[:310] for c,p in data["prices"].items()}
        data["marks"]={c:p[:310] for c,p in data["marks"].items()}
        data["distributions"]={c:p[:310] for c,p in data["distributions"].items()}
        rows,labels=outcome_records(data,self.calendar)
        for mode in rows:
            self.assertEqual(audit_outcome_differences(rows[mode],labels,self.calendar)["status"],"passed")
            self.assertEqual(rows[mode][0]["label_end"],data["days"][275])
            damaged=deepcopy(rows[mode]);damaged[0]["outcomes"][0]+=.01
            with self.assertRaises(ValueError):
                audit_outcome_differences(damaged,labels,self.calendar)
            nan=deepcopy(rows[mode]);nan[0]["outcomes"]=[float("nan")]
            with self.assertRaises(ValueError):
                audit_outcome_differences(nan,labels,self.calendar)
            premature=deepcopy(labels);premature[0]["label_end"]=premature[0]["exit_date"]
            with self.assertRaises(ValueError):
                audit_outcome_differences(rows[mode],premature,self.calendar)

    def test_future_teacher_labels_do_not_change_past_fit(self):
        data=self.data();rows=[]
        for i in range(252,770,10):
            for candidate in ("a","b","cash"):
                rows.append({"signal_date":data["days"][i],"label_end":data["days"][i+23],"code":candidate,
                    "features":economic_features(data,candidate,i,"trend"),"outcomes":[.01*math.sin(i/11)]})
        changed=deepcopy(rows);day=data["days"][650]
        for row in changed:
            if row["label_end"]>day:
                row["outcomes"]=[123.]
        self.assertEqual(MaturedForecaster(rows).at(day)[1],MaturedForecaster(changed).at(day)[1])

    def test_actual_fitted_models_and_selection_are_audited(self):
        data=self.data();records,labels=outcome_records(data,self.calendar)
        data["increment_records"]=records
        data["increment_forecasters"]={m:MaturedForecaster(rows) for m,rows in records.items()}
        data["increment_model_audit_cache"]={m:{} for m in records}
        for mode in ("state_only","trend"):
            result=economic_trial(data,self.calendar,mode,start=650,end=710)
            self.assertTrue(all(c["fitted"] for c in result["entry_choices"]))
            self.assertEqual(result["economic_validation"]["status"],"passed")
            checks=deepcopy(result["daily_checks"])
            entry=next(e for e in checks if e["action"]=="entry")
            entry["predictions"]["a"]+=.1
            with self.assertRaises(ValueError):
                audit_economic_account(result["account"],checks,data,policy=mode,mode=mode,records=records[mode],start=650)

    def test_late_risk_action_closes_before_overlapping_expiry(self):
        data=self.data();allocator=EconomicLeaseAllocator(data,start=650,lag=2)
        zero=AllocationState(650,data["days"][650],dict.fromkeys(data["prices"],0.))
        allocator(650,set(),zero)
        at=669
        data["economic_stats"][at]["votes"]=dict.fromkeys(data["prices"],0.)
        current={"a":.2,"b":.2,"000217":.2,"bond":.4}
        target=allocator(at,set(current),AllocationState(at,data["days"][at],current))
        self.assertEqual(allocator.trace[-1]["action"],"early_expiry")
        self.assertEqual(sum(target.weights.values()),0.)
        self.assertEqual(allocator.next_entry,674)

    def test_invalid_actual_state_is_rejected(self):
        data=self.data();allocator=EconomicLeaseAllocator(data,start=650)
        with self.assertRaises(ValueError):
            allocator(650,set(),AllocationState(651,data["days"][650],dict.fromkeys(data["prices"],0.)))


if __name__=="__main__":
    unittest.main()
