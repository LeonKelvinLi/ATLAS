"""Data-only partitioning and durable outage classification, entirely offline."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from atlas.collection import archive
from atlas.collection.primitives import HTTPResponse, _encoded
from atlas.research import data_archive as old
from atlas.research.data_contract_v1 import default_contract

NOW = 1789430700


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(old.time, "time", lambda: NOW)
    from atlas.research import data_collector
    monkeypatch.setattr(data_collector.time, "time", lambda: NOW)
    root = tmp_path / "generation"
    archive.init_collection_archive(root, default_contract("infra-test", NOW // 86400 * 86400, NOW))
    return root


def transport(rows, quote_time=None):
    def get(url, headers, **limits):
        if '/candles?' in url:
            return HTTPResponse(200,_encoded(rows))
        return HTTPResponse(200,_encoded({"sequence":1,"bids":[["100","1",1]],"asks":[["101","1",1]],
            **({"time":datetime.fromtimestamp(quote_time,timezone.utc).isoformat()} if quote_time else {})}))
    return get


def collect(root, *, clock=NOW, event=NOW-300, price=100, mode="prospective"):
    return archive.acquire_collection_once(root,provider='coinbase',start=event-300,end=event,
        transport=transport([[event-300,price,price,price,price,1]]),clock=lambda:clock,acquisition_mode=mode)


def test_registration_does_not_backdate_or_overwrite(root,tmp_path):
    with pytest.raises(ValueError):
        archive.init_collection_archive(tmp_path/'other',default_contract('older',NOW//86400*86400,NOW-1))
    with pytest.raises(ValueError):
        archive.init_collection_archive(root,default_contract('older',NOW//86400*86400,NOW))
    assert archive.verify_collection_archive(root)['record_count']==0


def test_repeated_restart_reads_do_not_duplicate_observations(root):
    first=collect(root)
    assert first['appended_records']==2
    again=collect(root)
    assert again['appended_records']==0
    assert again['unchanged_observations']==2
    summary=archive.verify_collection_archive(root)
    assert summary['record_count']==2 and summary['batch_count']==2
    assert summary['acquisition_kinds']['test']==2


def test_cross_midnight_routes_event_not_receipt_into_one_chain(root,monkeypatch):
    midnight=NOW//86400*86400
    event=midnight-300
    collect(root,event=event,clock=NOW)
    collect(root,event=event,clock=NOW+60,price=101)
    monkeypatch.setattr(old.time,'time',lambda:NOW+60)
    summary=archive.verify_collection_archive(root)
    assert summary['partition_count']==2
    day=archive._day(event)
    with old._connection(root/'days'/day) as db:
        rows=old._scan(db,NOW+60)['records']
    assert [row['revision'] for row in rows]==[0,1]
    assert rows[1]['previous_observation_hash']


@pytest.mark.parametrize('kind',['outage_backfill','stale'])
def test_builtin_outage_and_stale_never_become_prospective(root,monkeypatch,kind):
    from atlas.research import data_collector
    event=NOW if kind=='outage_backfill' else NOW-1200
    monkeypatch.setattr(data_collector,'_exchange_get',transport([[event-300,100,100,100,100,1]],NOW))
    mode='outage_backfill' if kind=='outage_backfill' else 'prospective'
    result=archive.acquire_collection_once(root,provider='coinbase',start=event-300,end=event,acquisition_mode=mode)
    summary=archive.verify_collection_archive(root)
    assert result['collection_complete']
    assert summary['acquisition_kinds']['historical_backfill']>=1
    # Revisions cannot promote the old bar even during a later ordinary poll.
    monkeypatch.setattr(data_collector,'_exchange_get',transport([[event-300,101,101,101,101,1]],NOW))
    archive.acquire_collection_once(root,provider='coinbase',start=event-300,end=event)
    with old._connection(root/'days'/archive._day(event)) as db:
        bars=[r for r in old._scan(db,NOW)['records'] if r['record_type']=='bar']
    assert all(r['acquisition_kind']=='historical_backfill' for r in bars)


def test_quick_health_never_claims_hash_verification(root):
    collect(root)
    result=archive.verify_collection_archive(root,full=False)
    assert result['record_count']==2 and not result['integrity_verified']


def test_deleted_partition_detected(root):
    collect(root)
    path=next((root/'days').iterdir())
    moved=path.with_name('moved')
    path.rename(moved)
    with pytest.raises((ValueError,OSError)):
        archive.verify_collection_archive(root)


def test_intact_orphan_after_crash_adopted_but_corrupt_not_recreated(root):
    reg=json.loads((root/'registration.json').read_text())
    day=archive._day(NOW)
    path=root/'days'/day
    old._create_archive(path,{'archive_version':1,'contract':reg['contract'],'clock_basis':reg['clock_basis']})
    with pytest.raises(ValueError):
        archive.verify_collection_archive(root)
    result=archive.verify_collection_archive(root,recover=True)
    assert result['partition_count']==1 and result['record_count']==0
    (path/'archive.sqlite3').write_bytes(b'broken')
    with pytest.raises((ValueError,sqlite3.Error)):
        archive.verify_collection_archive(root,recover=True)
    assert (path/'archive.sqlite3').read_bytes()==b'broken'


def test_provider_failure_quarantines_receipt_without_observations(root):
    def get(url,headers,**limits):
        return HTTPResponse(200,b'provider invalid JSON')
    result=archive.acquire_collection_once(root,provider='coinbase',start=NOW-600,end=NOW-300,transport=get,clock=lambda:NOW)
    assert not result['collection_complete'] and result['captures_retained']==1
    assert archive.verify_collection_archive(root)['record_count']==0


def test_http_rate_limit_metadata_preserved(root):
    result=archive.acquire_collection_once(root,provider='coinbase',start=NOW-600,end=NOW-300,
        transport=lambda *a,**k:HTTPResponse(429,b'',{'Retry-After':'120'}),clock=lambda:NOW)
    assert result['http_status']==429 and result['retry_after_seconds']==120
    assert result['appended_records']==0


def test_repeated_old_quote_receipts_do_not_grow_closed_market_partition(root,monkeypatch):
    event=NOW-300
    getter=transport([[event-300,100,100,100,100,1]],event)
    archive.acquire_collection_once(root,provider='coinbase',start=event-300,end=event,
        transport=getter,clock=lambda:NOW)
    day=archive._day(event)
    initial=old.verify_archive(root/'days'/day)['batch_count']
    later=NOW+86400
    result=archive.acquire_collection_once(root,provider='coinbase',start=event-300,end=event,
        transport=getter,clock=lambda:later)
    assert result['appended_records']==0 and result['unchanged_observations']==2
    assert old.verify_archive(root/'days'/day,clock=lambda:later)['batch_count']==initial
    summary=archive.verify_collection_archive(root,clock=lambda:later)
    assert summary['partition_count']==2 and summary['record_count']==2


@pytest.mark.parametrize('later', [0, 86400])
@pytest.mark.parametrize('changed', ['generation', 'clock'])
def test_replaced_valid_partition_refused_before_append_or_dedup(root, tmp_path, later, changed):
    collect(root)
    path = root/'days'/archive._day(NOW)
    path.rename(tmp_path/'original')
    registration = json.loads((root/'registration.json').read_text())
    if changed == 'generation':
        registration['contract']['generation_id'] = 'different-generation'
    else:
        registration['clock_basis'] = 'injected_test_clock'
    old._create_archive(path, {'archive_version': 1, 'contract': registration['contract'],
                              'clock_basis': registration['clock_basis']})
    before = (path/'archive.sqlite3').read_bytes()
    with pytest.raises(archive.CollectionArchiveError, match='partition_contract_mismatch'):
        collect(root, clock=NOW+later)
    assert (path/'archive.sqlite3').read_bytes() == before


@pytest.mark.parametrize('trigger', ['no_update', 'no_delete'])
def test_missing_append_only_catalog_guard_refused(root, trigger):
    with sqlite3.connect(root/'catalog.sqlite3') as db:
        db.execute('DROP TRIGGER '+trigger)
    with pytest.raises(archive.CollectionArchiveError, match='catalog_guards_invalid'):
        archive.verify_collection_archive(root)
