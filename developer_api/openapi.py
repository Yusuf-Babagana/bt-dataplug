"""OpenAPI 3 description of the developer API. tests.py checks it covers every route, so docs cannot drift silently."""

ERR = {'$ref': '#/components/schemas/Error'}
SECURITY = [{'ApiKey': [], 'SecretKey': []}]


def _resp(desc, schema=None):
    r = {'description': desc}
    if schema:
        r['content'] = {'application/json': {'schema': schema}}
    return r


def _ok(data_schema):
    return {'type': 'object', 'properties': {
        'success': {'type': 'boolean', 'example': True}, 'data': data_schema, 'request_id': {'type': 'string'}}}


COMMON_ERRORS = {
    '401': _resp('Missing or invalid credentials', ERR),
    '403': _resp('Application pending/suspended, IP not allowed, or service not permitted', ERR),
    '429': _resp('Rate limit exceeded (see Retry-After)', ERR),
}
IDEM = {'name': 'Idempotency-Key', 'in': 'header', 'required': True, 'schema': {'type': 'string', 'minLength': 8, 'maxLength': 64},
        'description': 'Unique value per purchase (e.g. a UUID). Retrying with the same key never charges twice.'}


def _purchase(summary, props, required, example):
    return {'post': {
        'summary': summary, 'security': SECURITY, 'parameters': [IDEM],
        'requestBody': {'required': True, 'content': {'application/json': {
            'schema': {'type': 'object', 'required': required, 'properties': {
                **props, 'client_reference': {'type': 'string', 'maxLength': 64, 'description': 'Your own order id (optional).'}}},
            'example': example}}},
        'responses': {
            '201': _resp('Order accepted. Check `status`: successful or pending.', {'$ref': '#/components/schemas/TransactionEnvelope'}),
            '200': _resp('Idempotent replay of an earlier request', {'$ref': '#/components/schemas/TransactionEnvelope'}),
            '402': _resp('insufficient_funds', ERR), '409': _resp('request_in_progress', ERR),
            '422': _resp('validation_error, idempotency_key_reused, or purchase_failed (wallet refunded)', ERR),
            '503': _resp('service_unavailable', ERR), **COMMON_ERRORS}}}


def _get(summary, data_schema, params=None):
    return {'get': {'summary': summary, 'security': SECURITY, 'parameters': params or [],
                    'responses': {'200': _resp('OK', _ok(data_schema)), **COMMON_ERRORS}}}


def _q(name, desc, required=False):
    return {'name': name, 'in': 'query', 'required': required, 'schema': {'type': 'string'}, 'description': desc}


PHONE = {'type': 'string', 'example': '08012345678'}

# path -> (url name used in api_urls.py, path item)
PATHS = {
    '/balance/': ('dev_balance', _get('Wallet balance', {'type': 'object', 'properties': {'balance': {'type': 'string', 'example': '12500.00'}, 'currency': {'type': 'string'}}})),
    '/services/': ('dev_services', _get('Services this application may use', {'type': 'object'})),
    '/networks/': ('dev_networks', _get('Mobile networks and availability', {'type': 'object'})),
    '/data-plans/': ('dev_data_plans', _get('Data plans and your prices', {'type': 'object'}, [_q('network', 'MTN, GLO, 9MOBILE, AIRTEL or code 01-04')])),
    '/cable-plans/': ('dev_cable_plans', _get('Cable TV packages and prices', {'type': 'object'}, [_q('cable_tv', 'dstv, gotv or startimes')])),
    '/electricity/discos/': ('dev_discos', _get('Electricity distribution companies', {'type': 'object'})),
    '/verify/meter/': ('dev_verify_meter', _get('Verify a meter number', {'type': 'object'}, [_q('disco', 'Disco code', True), _q('meter_no', 'Meter number', True), _q('meter_type', 'prepaid or postpaid', True)])),
    '/verify/cable/': ('dev_verify_cable', _get('Verify a smartcard / IUC number', {'type': 'object'}, [_q('cable_tv', 'dstv, gotv or startimes', True), _q('smartcard', 'Smartcard number', True)])),
    '/airtime/': ('dev_airtime', _purchase('Buy airtime', {'network': {'type': 'string', 'example': 'MTN'}, 'phone': PHONE, 'amount': {'type': 'number', 'example': 500}},
                                           ['network', 'phone', 'amount'], {'network': 'MTN', 'phone': '08012345678', 'amount': 500})),
    '/data/': ('dev_data', _purchase('Buy a data bundle', {'plan_id': {'type': 'integer', 'example': 12}, 'phone': PHONE},
                                     ['plan_id', 'phone'], {'plan_id': 12, 'phone': '08012345678'})),
    '/electricity/': ('dev_electricity', _purchase('Pay an electricity bill', {
        'disco': {'type': 'string', 'example': '04'}, 'meter_no': {'type': 'string'}, 'meter_type': {'type': 'string', 'enum': ['prepaid', 'postpaid']},
        'amount': {'type': 'number', 'example': 2000}, 'phone': PHONE},
        ['disco', 'meter_no', 'meter_type', 'amount', 'phone'], {'disco': '04', 'meter_no': '45012345678', 'meter_type': 'prepaid', 'amount': 2000, 'phone': '08012345678'})),
    '/cable/': ('dev_cable', _purchase('Subscribe or renew cable TV', {
        'cable_tv': {'type': 'string', 'example': 'gotv'}, 'package_id': {'type': 'string'}, 'smartcard': {'type': 'string'}, 'phone': PHONE},
        ['cable_tv', 'package_id', 'smartcard', 'phone'], {'cable_tv': 'gotv', 'package_id': 'gotv-jolli', 'smartcard': '7012345678', 'phone': '08012345678'})),
    '/transactions/': ('dev_transactions', _get('List your transactions', {'type': 'object'}, [
        _q('status', 'pending, successful, failed, reversed'), _q('service', 'airtime, data, electricity, cable'),
        _q('client_reference', 'Your own order id'), _q('limit', '1-100, default 25'), _q('offset', 'default 0')])),
    '/transactions/{reference}/': ('dev_transaction', {'get': {
        'summary': 'Get one transaction (poll this if you do not use webhooks)', 'security': SECURITY,
        'parameters': [{'name': 'reference', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
        'responses': {'200': _resp('OK', {'$ref': '#/components/schemas/TransactionEnvelope'}), '404': _resp('not_found', ERR), **COMMON_ERRORS}}}),
}


def build_spec(server_url):
    tx = {'type': 'object', 'properties': {
        'reference': {'type': 'string', 'example': 'BTD-3F9A1C2B4D5E6F708192'}, 'client_reference': {'type': 'string', 'nullable': True},
        'service': {'type': 'string'}, 'description': {'type': 'string'}, 'recipient': {'type': 'string'},
        'amount': {'type': 'string', 'example': '495.00'},
        'status': {'type': 'string', 'enum': ['pending', 'successful', 'failed', 'reversed']},
        'failure_reason': {'type': 'string', 'nullable': True}, 'environment': {'type': 'string', 'enum': ['test', 'live']},
        'result': {'type': 'object', 'description': 'Service extras, e.g. {"token": "..."} for prepaid electricity'},
        'created_at': {'type': 'string', 'format': 'date-time'}, 'updated_at': {'type': 'string', 'format': 'date-time'}}}
    return {
        'openapi': '3.0.3',
        'info': {'title': 'BT DataPlug Developer API', 'version': '1.0.0',
                 'description': 'Airtime, data, electricity and cable TV through one API. Use a **test** application (sandbox, no money) '
                                'until you are ready, then a **live** application approved by BT DataPlug. Every purchase needs an `Idempotency-Key` header.'},
        'servers': [{'url': server_url}],
        'paths': {p: item for p, (_, item) in PATHS.items()},
        'components': {
            'securitySchemes': {
                'ApiKey': {'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key', 'description': 'Public key, pk_test_... / pk_live_...'},
                'SecretKey': {'type': 'http', 'scheme': 'bearer', 'description': 'Secret key, sk_test_... / sk_live_...'}},
            'schemas': {
                'Error': {'type': 'object', 'properties': {'success': {'type': 'boolean', 'example': False}, 'error': {'type': 'object', 'properties': {
                    'code': {'type': 'string', 'example': 'insufficient_funds'}, 'message': {'type': 'string'}, 'details': {'type': 'object'}}},
                    'request_id': {'type': 'string'}}},
                'Transaction': tx,
                'TransactionEnvelope': _ok({'$ref': '#/components/schemas/Transaction'}),
            }},
    }
