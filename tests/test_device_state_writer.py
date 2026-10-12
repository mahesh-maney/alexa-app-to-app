import copy
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

spec=importlib.util.spec_from_file_location('writer',Path(__file__).parents[1]/'lambdas/device_state_writer/lambda_function.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_ping_never_refreshes_endpoint_state():
    item={'subDevices':{'mac':{'endpoints':{'1':{'lastUpdatedTime':'100','state':{'onOff':0}}}}}}
    previous=copy.deepcopy(item['subDevices'])
    assert m._merge(item,{'cmdType':100,'status':'SUCCESS'},200)
    assert item['subDevices']==previous
    assert item['gatewayLastSeenTime']=='200'


def test_partial_endpoint_report_preserves_other_state_and_endpoints():
    item={'subDevices':{'mac':{'endpoints':{'1':{'lastUpdatedTime':'100','state':{'onOff':0,'level':20}},'2':{'state':{'onOff':1}}}}}}
    assert m._merge(item,{'mac':'mac','epid':1,'state':{'onOff':1}},200)
    ep=item['subDevices']['mac']['endpoints']['1']
    assert ep['state']=={'onOff':1,'level':20}
    assert ep['lastUpdatedTime']=='200'
    assert item['subDevices']['mac']['endpoints']['2']['state']=={'onOff':1}


def test_old_or_duplicate_state_does_not_overwrite_newer_state():
    item={'subDevices':{'mac':{'endpoints':{'1':{'lastUpdatedTime':'200','state':{'onOff':1}}}}}}
    before=copy.deepcopy(item)
    assert not m._merge(item,{'mac':'mac','epid':1,'state':{'onOff':0}},100)
    assert not m._merge(item,{'mac':'mac','epid':1,'state':{'onOff':0}},200)
    assert item==before


def test_curtain_state_is_not_coerced_to_boolean():
    item={}
    m._merge(item,{'mac':'mac','epid':1,'epType':5,'state':{'onOff':4}},200)
    assert item['subDevices']['mac']['endpoints']['1']['state']['onOff']==4


def test_configuration_reply_does_not_update_state():
    item={}
    assert not m._merge(item,{'cmdType':115,'status':'SUCCESS','data':{}},200)
    assert item=={}


def test_concurrent_write_reloads_instead_of_losing_other_endpoint():
    table=MagicMock()
    table.get_item.side_effect=[{'Item':{'deviceId':'g'}},{'Item':{'deviceId':'g','_stateVersion':1,'lastUpdatedTime':'190','subDevices':{'other':{'endpoints':{'2':{'state':{'onOff':1}}}}}}}]
    table.put_item.side_effect=[ClientError({'Error':{'Code':'ConditionalCheckFailedException'}},'PutItem'),{}]
    m._process(table,{'body':json.dumps({'topic':'iot/device/g/response','mac':'mac','epid':1,'state':{'onOff':1}}),'attributes':{'SentTimestamp':'200'}})
    assert table.get_item.call_count==2
    item=table.put_item.call_args.kwargs['Item']
    assert 'other' in item['subDevices'] and 'mac' in item['subDevices']
    assert item['_stateVersion']==2
