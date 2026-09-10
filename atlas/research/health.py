"""Descriptive feature drift and durable, sticky-disable research health.

This is an operational observation, not an alpha test or a broker control.
No network, broker, strategy activation or automatic reactivation is possible.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import statistics
import threading
import time

from atlas.research.market import DatasetError, identifier, timestamp

MIN_REFERENCE = 100
MIN_CURRENT = 30
STATUSES = {"HEALTHY","WATCH","DEGRADED","DISABLED"}
EVIDENCE = {"SYNTHETIC","TEST","HISTORICAL_UNVERIFIED","HISTORICAL_OUT_OF_SAMPLE","PAPER","SHADOW"}
THRESHOLDS = {"watch_standardized_mean_shift":1.0,"degraded_standardized_mean_shift":3.0,
              "watch_ks_distance":0.2,"degraded_ks_distance":0.5}
_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE health (strategy_id TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL, report_hash TEXT NOT NULL)",
    "CREATE TABLE events (strategy_id TEXT NOT NULL, observed_at INTEGER NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL, report_hash TEXT NOT NULL, PRIMARY KEY(strategy_id,observed_at))",
)


def _canonical(value):
    result=json.dumps(value,sort_keys=True,separators=(",", ":"),allow_nan=False)
    if len(result.encode())>1_000_000:raise ValueError("health report exceeds limit")
    return result


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _feature_columns(rows):
    if not isinstance(rows,list) or len(rows)>100_000:
        raise ValueError("feature rows must be a bounded list")
    keys=set()
    for row in rows:
        if not isinstance(row,dict) or len(row)>256:raise ValueError("invalid feature row")
        keys.update(row)
    if len(keys)>256:raise ValueError("too many feature columns")
    for key in keys:identifier(key,"feature_id")
    columns={key:[] for key in sorted(keys)}
    missing={key:0 for key in sorted(keys)}
    for row in rows:
        for key in columns:
            raw=row.get(key)
            if raw is None:
                missing[key]+=1
                continue
            if isinstance(raw,bool):raise ValueError("boolean feature value")
            value=float(raw)
            if not math.isfinite(value) or abs(value)>1e100:raise ValueError("nonfinite or unbounded feature value")
            columns[key].append(value)
    return columns,missing


def _ks_distance(a,b):
    """Empirical two-sample CDF distance; deliberately no IID p-value."""
    a,b=sorted(a),sorted(b)
    i=j=0
    distance=0.0
    while i<len(a) or j<len(b):
        value=min(a[i] if i<len(a) else math.inf,b[j] if j<len(b) else math.inf)
        while i<len(a) and a[i]<=value:i+=1
        while j<len(b) and b[j]<=value:j+=1
        distance=max(distance,abs(i/len(a)-j/len(b)))
    return distance


def assess_health(reference_features,current_features,*,evidence_class,reference_schema_hash,
                  current_schema_hash,data_quality_ok=True,stale=False):
    """Observe finite feature shifts with explicit small-sample limitations.

    Thresholds are conservative engineering rules, not optimized statistical
    rejection levels. Serial dependence and multiplicity are not converted into
    spurious significance. HEALTHY means no configured drift alert, not alpha.
    """
    reasons=[]
    report={"schema_version":"1.0","status":"HEALTHY","evidence_class":evidence_class,
            "reference_schema_hash":reference_schema_hash,"current_schema_hash":current_schema_hash,
            "thresholds":dict(THRESHOLDS),"sample_floors":{"reference":MIN_REFERENCE,"current":MIN_CURRENT},
            "features":{},"reasons":reasons,"live_approved":False,"statistical_significance":None,
            "warnings":["Drift thresholds are engineering rules, not IID hypothesis tests or proof of alpha.",
                        "Serial dependence, overlapping samples and multiple features limit interpretation.",
                        "A healthy status cannot activate trading; a disabled status requires explicit external recovery."]}
    if not isinstance(evidence_class,str) or evidence_class not in EVIDENCE:
        report.update(status="DISABLED",evidence_class="UNKNOWN");reasons.append("invalid_evidence_class")
    valid_hashes=all(isinstance(h,str) and re.fullmatch(r"[0-9a-f]{64}",h) for h in (reference_schema_hash,current_schema_hash))
    if not valid_hashes or reference_schema_hash!=current_schema_hash:
        report["status"]="DISABLED";reasons.append("feature_schema_mismatch_or_missing_identity")
    if not isinstance(data_quality_ok,bool) or not isinstance(stale,bool) or not data_quality_ok or stale:
        report["status"]="DISABLED";reasons.append("stale_or_invalid_data_quality")
    try:
        reference,reference_missing=_feature_columns(reference_features)
        current,current_missing=_feature_columns(current_features)
    except (ValueError,TypeError,OverflowError) as exc:
        report["status"]="DISABLED";reasons.append("invalid_feature_data");report["data_error"]=str(exc)
        return report
    if set(reference)!=set(current):
        report["status"]="DISABLED";reasons.append("feature_columns_mismatch")
    elif not reference:
        if report["status"]!="DISABLED":report["status"]="WATCH"
        reasons.append("no_feature_observations")
    for feature in sorted(set(reference)&set(current)):
        a,b=reference[feature],current[feature]
        result={"reference_count":len(a),"current_count":len(b),"reference_missing":reference_missing[feature],
                "current_missing":current_missing[feature],"standardized_mean_shift":None,"ks_distance":None,
                "p_value":None,"status":"WATCH"}
        if len(a)<MIN_REFERENCE or len(b)<MIN_CURRENT:
            result["reason"]="insufficient_observations"
        else:
            reference_mean,current_mean=statistics.mean(a),statistics.mean(b)
            sd=statistics.stdev(a)
            constant_shift=sd==0 and any(value!=a[0] for value in b)
            shift=(current_mean-reference_mean)/sd if sd else (None if constant_shift else 0.0)
            shift_overflow=shift is not None and not math.isfinite(shift)
            if shift_overflow:shift=None
            distance=_ks_distance(a,b)
            result.update(reference_mean=reference_mean,current_mean=current_mean,reference_std=sd,
                          standardized_mean_shift=shift,ks_distance=distance,constant_reference_changed=constant_shift,
                          standardized_shift_overflow=shift_overflow)
            if shift_overflow or constant_shift or (shift is not None and abs(shift)>=THRESHOLDS["degraded_standardized_mean_shift"]) or distance>=THRESHOLDS["degraded_ks_distance"]:
                result["status"]="DEGRADED"
            elif (shift is not None and abs(shift)>=THRESHOLDS["watch_standardized_mean_shift"]) or distance>=THRESHOLDS["watch_ks_distance"] or reference_missing[feature] or current_missing[feature]:
                result["status"]="WATCH"
            else:result["status"]="HEALTHY"
        report["features"][feature]=result
        if report["status"]!="DISABLED":
            severity={"HEALTHY":0,"WATCH":1,"DEGRADED":2}
            if severity[result["status"]]>severity[report["status"]]:report["status"]=result["status"]
    if report["status"]=="HEALTHY":reasons.append("no_configured_drift_alert")
    elif report["status"] in {"WATCH","DEGRADED"}:reasons.append("feature_drift_or_insufficient_observations")
    return report


def historical_tail_risk(daily_returns,*,confidence=0.95):
    """Descriptive historical daily VaR/ES; not a portfolio loss guarantee."""
    if isinstance(confidence,bool) or confidence not in {0.95,0.99}:
        raise ValueError("supported confidence is 0.95 or 0.99")
    if not isinstance(daily_returns,list) or len(daily_returns)>100_000:
        raise ValueError("daily returns must be a bounded list")
    values=[]
    for raw in daily_returns:
        if isinstance(raw,bool):raise ValueError("boolean return")
        value=float(raw)
        if not math.isfinite(value) or value < -1 or abs(value)>1e6:raise ValueError("invalid simple daily return")
        values.append(value)
    result={"method":"historical_empirical_loss_quantile","confidence":confidence,"daily_observations":len(values),
            "var":None,"expected_shortfall":None,"independence_verified":False,
            "warnings":["Caller must supply daily portfolio returns after cash-flow/cost adjustment.",
                        "Historical tails are descriptive; regime shifts and serial dependence remain unmodeled."]}
    if len(values)<250:
        result["reason"]="requires_at_least_250_daily_observations"
        return result
    losses=sorted(-value for value in values)
    index=math.ceil(confidence*len(losses))-1
    threshold=losses[index]
    tail=losses[index:]
    result.update(var=threshold,expected_shortfall=statistics.mean(tail),tail_observations=len(tail),
                  quantile_convention="nearest_rank; ES mean from quantile order statistic through maximum loss")
    return result


class HealthStoreError(ValueError):
    pass


class HealthStore:
    """Owned external SQLite file; DISABLED survives restart and better reports.

    There is intentionally no reactivate method. Explicit reviewed recovery must
    be implemented separately; deleting/replacing state is not a supported reset.
    """
    def __init__(self,path,*,clock=time.time):
        original=Path(path).expanduser()
        if not original.is_absolute() or original.is_symlink():raise HealthStoreError("health path must be absolute and not a symlink")
        self.path=original.resolve()
        if self.path.is_relative_to(Path(__file__).resolve().parents[2]):raise HealthStoreError("health state must be outside the repository")
        self.clock=clock;self._conn=None;self._failed=False;self._lock=threading.RLock()
        missing=[];parent=self.path.parent
        while not parent.exists():missing.append(parent);parent=parent.parent
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        for directory in reversed(missing):self._sync(directory.parent)
        info=self.path.parent.stat()
        if info.st_uid!=os.getuid() or info.st_mode&0o022:raise HealthStoreError("health parent must be owned without group/other writes")
        new=False
        try:fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
        except FileExistsError:
            info=self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size==0:
                raise HealthStoreError("existing health state is unsafe or empty; recovery required")
        else:os.close(fd);new=True
        try:
            self._conn=sqlite3.connect(self.path,isolation_level=None,check_same_thread=False,timeout=10)
            self._conn.row_factory=sqlite3.Row
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA journal_mode=DELETE")
            tables={r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if new and not tables:
                self._conn.execute("BEGIN IMMEDIATE")
                for sql in _SCHEMA:self._conn.execute(sql)
                self._conn.execute("INSERT INTO metadata VALUES ('schema','atlas-health-v1')")
                self._conn.execute("INSERT INTO metadata VALUES ('event_count','0')")
                self._conn.execute("COMMIT");self._sync(self.path.parent)
            elif tables!={"metadata","health","events"}:raise HealthStoreError("invalid health schema")
            expected={sql.split()[2]:sql for sql in _SCHEMA}
            actual={r[0]:r[1] for r in self._conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
            if actual!=expected:raise HealthStoreError("health schema changed")
            if self._conn.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise HealthStoreError("corrupt health state")
            metadata=dict(self._conn.execute("SELECT key,value FROM metadata"))
            if set(metadata)!={"schema","event_count"} or metadata["schema"]!="atlas-health-v1":raise HealthStoreError("unknown health schema version")
            if int(metadata["event_count"])!=self._conn.execute("SELECT count(*) FROM events").fetchone()[0]:raise HealthStoreError("health event history is incomplete")
            latest,disabled={},set()
            for row in self._conn.execute("SELECT * FROM events ORDER BY strategy_id,observed_at"):
                self._decode(row)
                if row["strategy_id"] in disabled and row["status"]!="DISABLED":raise HealthStoreError("disabled health was reactivated")
                if row["status"]=="DISABLED":disabled.add(row["strategy_id"])
                latest[row["strategy_id"]]=dict(row)
            states={}
            for row in self._conn.execute("SELECT * FROM health"):
                self._decode(row);states[row["strategy_id"]]=dict(row)
            if latest!=states:raise HealthStoreError("latest health state does not match durable history")
        except Exception:
            self._failed=True;self.close();raise HealthStoreError("health state cannot be opened safely") from None

    @staticmethod
    def _sync(directory):
        fd=os.open(directory,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def close(self):
        with self._lock:
            if self._conn is not None:self._conn.close();self._conn=None

    def __enter__(self):return self

    def __exit__(self,*_):self.close()

    @staticmethod
    def _decode(row):
        if row["status"] not in STATUSES or _hash(row["report"])!=row["report_hash"]:raise HealthStoreError("corrupt health report")
        report=json.loads(row["report"])
        if report.get("status") not in STATUSES or report.get("schema_version")!="1.0":raise HealthStoreError("invalid health report")
        identifier(row["strategy_id"],"strategy_id");timestamp(row["observed_at"])
        return {"strategy_id":row["strategy_id"],"observed_at":row["observed_at"],"status":row["status"],
                "assessed_status":report["status"],"report_hash":row["report_hash"],"report":report,
                "sticky_disabled":row["status"]=="DISABLED","live_approved":False}

    def get(self,strategy_id):
        identifier(strategy_id,"strategy_id")
        with self._lock:
            if self._failed or self._conn is None:raise HealthStoreError("health store unavailable")
            try:
                row=self._conn.execute("SELECT * FROM health WHERE strategy_id=?",(strategy_id,)).fetchone()
                return self._decode(row) if row else None
            except (sqlite3.Error,ValueError,TypeError):
                self._failed=True;raise HealthStoreError("health state unreadable; recovery required") from None

    def record(self,strategy_id,report,observed_at):
        identifier(strategy_id,"strategy_id");timestamp(observed_at,"observed_at")
        if observed_at>int(self.clock()):raise HealthStoreError("health observation is in the future")
        if not isinstance(report,dict) or report.get("schema_version")!="1.0" or report.get("status") not in STATUSES or report.get("live_approved") is not False:
            raise HealthStoreError("invalid assessed health report")
        encoded=_canonical(report);digest=_hash(encoded)
        with self._lock:
            if self._failed or self._conn is None:raise HealthStoreError("health store unavailable")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                old=self._conn.execute("SELECT * FROM health WHERE strategy_id=?",(strategy_id,)).fetchone()
                if old:
                    self._decode(old)
                    if observed_at<old["observed_at"] or (observed_at==old["observed_at"] and digest!=old["report_hash"]):
                        raise HealthStoreError("health observation must advance monotonically; equal time only permits exact retry")
                    if observed_at==old["observed_at"]:
                        self._conn.execute("ROLLBACK")
                        return self._decode(old)
                status="DISABLED" if old and old["status"]=="DISABLED" else report["status"]
                args=(strategy_id,observed_at,status,encoded,digest)
                self._conn.execute("INSERT OR REPLACE INTO health VALUES (?,?,?,?,?)",args)
                self._conn.execute("INSERT INTO events VALUES (?,?,?,?,?)",args)
                self._conn.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='event_count'")
                self._conn.execute("COMMIT")
                self._sync(self.path.parent)
                return self._decode(self._conn.execute("SELECT * FROM health WHERE strategy_id=?",(strategy_id,)).fetchone())
            except (sqlite3.Error,OSError):
                self._failed=True
                if self._conn.in_transaction:self._conn.execute("ROLLBACK")
                raise HealthStoreError("health persistence uncertain; reopen and verify before retry") from None
            except BaseException:
                if self._conn.in_transaction:self._conn.execute("ROLLBACK")
                raise
