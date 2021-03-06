from datetime import datetime
import time

from pyramid.httpexceptions import (
    HTTPNotFound,
    HTTPOk,
    HTTPServiceUnavailable,
)
from pytz import utc
from redis import ConnectionError

from ichnaea.customjson import dumps
from ichnaea.data.tasks import insert_measures
from ichnaea.service.base import check_api_key
from ichnaea.service.error import (
    JSONParseError,
    preprocess_request,
    verify_schema,
)
from ichnaea.service.geolocate.schema import GeoLocateSchema
from ichnaea.service.geolocate.views import NOT_FOUND
from ichnaea.service.geosubmit.schema import (
    GeoSubmitBatchSchema,
    GeoSubmitSchema,
)
from ichnaea.service.locate import (
    search_all_sources,
    map_data,
)
from ichnaea.service.submit.schema import SubmitSchema

SENTINEL = object()


def process_upload(nickname, email, items, stats_client,
                   api_key_log=False, api_key_name=None):
    if isinstance(nickname, str):  # pragma: no cover
        nickname = nickname.decode('utf-8', 'ignore')

    if isinstance(email, str):  # pragma: no cover
        email = email.decode('utf-8', 'ignore')

    batch_list = []
    for batch in items:
        normalized_cells = []
        for c in batch['cellTowers']:
            cell = {}
            cell['radio'] = batch['radioType']
            cell['mcc'] = c['mobileCountryCode']
            cell['mnc'] = c['mobileNetworkCode']
            cell['lac'] = c['locationAreaCode']
            cell['cid'] = c['cellId']
            cell['psc'] = c['psc']
            cell['asu'] = c['asu']
            cell['signal'] = c['signalStrength']
            cell['ta'] = c['timingAdvance']

            normalized_cells.append(cell)

        normalized_wifi = []
        for w in batch['wifiAccessPoints']:
            wifi = {}
            wifi['key'] = w['macAddress']
            wifi['frequency'] = w['frequency']
            wifi['channel'] = w['channel']
            wifi['signal'] = w['signalStrength']
            wifi['signalToNoiseRatio'] = w['signalToNoiseRatio']
            normalized_wifi.append(wifi)

        if batch['timestamp'] == 0:
            batch['timestamp'] = time.time() * 1000.0

        dt = utc.fromutc(datetime.utcfromtimestamp(
                         batch['timestamp'] / 1000.0).replace(tzinfo=utc))
        ts = dt.isoformat()

        normalized_batch = {'lat': batch['latitude'],
                            'lon': batch['longitude'],
                            'time': ts,
                            'accuracy': batch['accuracy'],
                            'altitude': batch['altitude'],
                            'altitude_accuracy': batch['altitudeAccuracy'],
                            'radio': batch['radioType'],
                            'heading': batch['heading'],
                            'speed': batch['speed'],
                            'cell': normalized_cells,
                            'wifi': normalized_wifi,
                            }
        batch_list.append(normalized_batch)

    # Run the SubmitSchema validator against the normalized submit
    # data.
    schema = SubmitSchema()
    body = {'items': batch_list}
    errors = []
    validated = {}
    verify_schema(schema, body, errors, validated)

    if errors:  # pragma: no cover
        # Short circuit on any error in schema validation
        return errors

    # count the number of batches and emit a pseudo-timer to capture
    # the number of reports per batch
    length = len(batch_list)
    stats_client.incr('items.uploaded.batches')
    stats_client.timing('items.uploaded.batch_size', length)

    if api_key_log:
        stats_client.incr(
            'items.api_log.%s.uploaded.batches' % api_key_name)
        stats_client.timing(
            'items.api_log.%s.uploaded.batch_size' % api_key_name, length)

    for i in range(0, length, 100):
        batch_items = dumps(batch_list[i:i + 100])
        # insert measures, expire the task if it wasn't processed
        # after six hours to avoid queue overload
        try:
            insert_measures.apply_async(
                kwargs={
                    'email': email,
                    'items': batch_items,
                    'nickname': nickname,
                    'api_key_log': api_key_log,
                    'api_key_name': api_key_name,
                },
                expires=21600)
        except ConnectionError:  # pragma: no cover
            return SENTINEL
    return errors


def configure_geosubmit(config):
    config.add_route('v1_geosubmit', '/v1/geosubmit')
    config.add_view(geosubmit_view, route_name='v1_geosubmit', renderer='json')


def flatten_items(data):
    if any(data.get('items', ())):
        items = data['items']
    else:  # pragma: no cover
        items = [data]

    return items


@check_api_key('geosubmit')
def geosubmit_view(request):
    # Order matters here.  We need to try the batch mode *before* the
    # single upload mode as classic w3c geolocate calls should behave
    # identically using either geosubmit or geolocate
    data, errors = preprocess_request(
        request,
        schema=GeoSubmitBatchSchema(),
        response=None,
    )

    if any(data.get('items', ())):
        return process_batch(request, data, errors)
    else:
        return process_single(request)


def process_batch(request, data, errors):
    stats_client = request.registry.stats_client

    nickname = request.headers.get('X-Nickname', u'')
    email = request.headers.get('X-Email', u'')
    upload_items = flatten_items(data)
    errors = process_upload(
        nickname, email, upload_items, stats_client,
        api_key_log=getattr(request, 'api_key_log', False),
        api_key_name=getattr(request, 'api_key_name', None),
    )

    if errors is SENTINEL:  # pragma: no cover
        return HTTPServiceUnavailable()

    if errors:  # pragma: no cover
        stats_client.incr('geosubmit.upload.errors', len(errors))

    result = HTTPOk()
    result.content_type = 'application/json'
    result.body = '{}'
    return result


def process_single(request):
    stats_client = request.registry.stats_client

    locate_data, locate_errors = preprocess_request(
        request,
        schema=GeoLocateSchema(),
        response=JSONParseError,
        accept_empty=True,
    )

    data, errors = preprocess_request(
        request,
        schema=GeoSubmitSchema(),
        response=None,
    )
    data = {'items': [data]}

    nickname = request.headers.get('X-Nickname', u'')
    email = request.headers.get('X-Email', u'')
    upload_items = flatten_items(data)
    errors = process_upload(
        nickname, email, upload_items, stats_client,
        api_key_log=getattr(request, 'api_key_log', False),
        api_key_name=getattr(request, 'api_key_name', None),
    )

    if errors is not SENTINEL and errors:  # pragma: no cover
        stats_client.incr('geosubmit.upload.errors', len(errors))

    first_item = data['items'][0]
    if first_item['latitude'] == -255 or first_item['longitude'] == -255:
        data = map_data(data['items'][0])
        session = request.db_slave_session
        result = search_all_sources(
            session, 'geosubmit', data,
            client_addr=request.client_addr,
            geoip_db=request.registry.geoip_db,
            api_key_log=getattr(request, 'api_key_log', False),
            api_key_name=getattr(request, 'api_key_name', None))
    else:
        result = {'lat': first_item['latitude'],
                  'lon': first_item['longitude'],
                  'accuracy': first_item['accuracy']}

    if result is None:
        stats_client.incr('geosubmit.miss')
        result = HTTPNotFound()
        result.content_type = 'application/json'
        result.body = NOT_FOUND
        return result

    return {
        "location": {
            "lat": result['lat'],
            "lng": result['lon'],
        },
        "accuracy": float(result['accuracy']),
    }
