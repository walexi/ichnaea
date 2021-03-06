import time

from pyramid.httpexceptions import HTTPServiceUnavailable
from pyramid.view import view_config


def configure_monitor(config):
    config.scan('ichnaea.service.monitor.views')


class Timer(object):

    def __init__(self):
        self.start = None
        self.ms = None

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, typ, value, tb):
        if self.start is not None:
            dt = time.time() - self.start
            self.ms = int(round(1000 * dt))


def _check_timed(ping_function):
    with Timer() as timer:
        success = ping_function()
    if not success:
        return {'up': False, 'time': 0}
    return {'up': True, 'time': timer.ms}


def check_database(request):
    return _check_timed(request.db_slave_session.ping)


def check_geoip(request):
    geoip_db = request.registry.geoip_db
    result = _check_timed(geoip_db.ping)
    result['age_in_days'] = geoip_db.age
    return result


def check_redis(request):
    return _check_timed(request.registry.redis_client.ping)


def check_stats(request):
    return _check_timed(request.registry.stats_client.ping)


@view_config(renderer='json', name="__monitor__")
def monitor_view(request):
    services = {
        'database': check_database,
        'geoip': check_geoip,
        'redis': check_redis,
        'stats': check_stats,
    }
    failed = False
    result = {}
    for name, check in services.items():
        try:
            service_result = check(request)
        except Exception:  # pragma: no cover
            result[name] = {'up': None, 'time': -1}
            failed = True
        else:
            result[name] = service_result
            if not service_result['up']:
                failed = True

    if failed:
        response = HTTPServiceUnavailable()
        response.content_type = 'application/json'
        response.json = result
        return response

    return result
