"""SQS device-response consumer with ordered, per-endpoint state freshness.

Deployment target: existing write_state_db. No device commands are sent.
Health replies update gateway liveness only, never endpoint freshness.
"""
import json
import logging
import os
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
_TABLE = os.environ.get('DEVICE_STATE_TABLE', 'device_state')
_REGION = os.environ.get('DATA_REGION', os.environ.get('AWS_REGION', 'ap-south-1'))


def _merge(item, body, sent):
    """Return whether a valid newer observation changed the record."""
    mac, epid = body.get('mac'), body.get('epid')
    if mac and epid is not None and isinstance(body.get('state'), dict) and body['state']:
        endpoint = item.setdefault('subDevices', {}).setdefault(mac, {}).setdefault('endpoints', {}).setdefault(str(epid), {})
        if int(endpoint.get('lastUpdatedTime') or 0) >= sent:
            return False
        endpoint['state'] = {**(endpoint.get('state') or {}), **body['state']}
        endpoint['lastUpdatedTime'] = str(sent)
        # A reported state proves this endpoint responded, unless firmware says offline.
        endpoint['status'] = body.get('status') or 'online'
        if body.get('epType', body.get('eptype')) is not None:
            endpoint['epType'] = body.get('epType', body.get('eptype'))
        if body.get('lqi') is not None:
            endpoint['lqi'] = body['lqi']
    elif body.get('cmdType') == 100 and body.get('status') == 'SUCCESS':
        if int(item.get('gatewayLastSeenTime') or 0) >= sent:
            return False
        item['gatewayLastSeenTime'] = str(sent)
        item['gatewayStatus'] = 'online'
    else:
        return False
    item['lastUpdatedTime'] = str(max(sent, int(item.get('lastUpdatedTime') or 0)))
    return True


def _process(table, record):
    body = json.loads(record['body'], parse_float=Decimal)
    parts = str(body.get('topic', '')).split('/')
    if len(parts) != 4 or parts[:2] != ['iot', 'device'] or parts[3] != 'response' or not parts[2]:
        raise ValueError('Unexpected device response topic')
    duid = parts[2]
    sent = int(record['attributes']['SentTimestamp'])
    if sent <= 0:
        raise ValueError('Invalid message timestamp')
    for _ in range(5):
        item = table.get_item(Key={'deviceId': duid}, ConsistentRead=True).get('Item', {'deviceId': duid})
        version, previous_time = item.get('_stateVersion'), item.get('lastUpdatedTime')
        if not _merge(item, body, sent):
            return
        item['_stateVersion'] = int(version or 0) + 1
        names = {'#v': '_stateVersion', '#ts': 'lastUpdatedTime'}
        values = {}
        conditions = []
        for name, old, key in [('#v', version, ':v'), ('#ts', previous_time, ':ts')]:
            if old is None:
                conditions.append(f'attribute_not_exists({name})')
            else:
                conditions.append(f'{name} = {key}')
                values[key] = old
        args = dict(Item=item, ConditionExpression=' AND '.join(conditions), ExpressionAttributeNames=names)
        if values:
            args['ExpressionAttributeValues'] = values
        try:
            table.put_item(**args)
            return
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
    raise RuntimeError('Concurrent state updates exceeded retry budget')


def lambda_handler(event, context):
    table = boto3.resource('dynamodb', region_name=_REGION).Table(_TABLE)
    # Raising retries the batch; timestamp checks make already processed records idempotent.
    for record in event.get('Records', []):
        _process(table, record)
    logger.info('DEVICE_STATE_BATCH_OK count=%d', len(event.get('Records', [])))
    return {'statusCode': 200}
