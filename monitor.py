"""Login-authorized, read-only monitoring for cross-site static dashboards.

Credentials/cookies stay on each Scanner origin. A popup grants a short-lived,
origin-bound capability to a trusted overview page, held only in that page's memory.
"""
import os
import hashlib
import hmac
import re
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, make_response, render_template, request
from flask_login import current_user, login_required
from itsdangerous import URLSafeTimedSerializer

MONITOR_TTL = 8 * 60 * 60


def allowed_monitor_origins():
    raw = os.environ.get('SCANNER_MONITOR_ORIGINS', '')
    return {item.strip().rstrip('/') for item in raw.split(',') if item.strip()}


def valid_connect_request():
    origin = request.args.get('origin', '')
    state = request.args.get('state', '')
    return origin in allowed_monitor_origins() and bool(re.fullmatch(r'[A-Za-z0-9_-]{24,128}', state))


def monitor_login_return():
    """Only allow the known monitoring auth route as a login continuation."""
    target = request.args.get('next', '')
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.path != '/monitor/connect' or target.startswith('//'):
        return None
    # The connect route validates the origin/state again before issuing anything.
    return target


def register_monitor(app, user_lookup, snapshot):
    bp = Blueprint('monitor', __name__)

    def signer():
        return URLSafeTimedSerializer(app.secret_key, salt='scanner-readonly-monitor-v1')

    @bp.after_request
    def response_policy(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        origin = request.headers.get('Origin', '')
        if origin in allowed_monitor_origins():
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Vary'] = 'Origin'
            response.headers['Access-Control-Allow-Headers'] = 'Authorization'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        return response

    @bp.route('/monitor/connect')
    @login_required
    def connect():
        if not valid_connect_request():
            return '监控页面来源或请求无效，请检查 SCANNER_MONITOR_ORIGINS', 400
        origin = request.args['origin']
        state = request.args['state']
        user = user_lookup(current_user.get_id())
        fingerprint = hashlib.sha256(user.password_hash.encode()).hexdigest()
        token = signer().dumps({'user': current_user.get_id(), 'origin': origin, 'scope': 'monitor:read',
                                'auth_version': fingerprint})
        response = make_response(render_template('monitor_connect.html', origin=origin, state=state,
                                                 token=token, ttl=MONITOR_TTL))
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @bp.route('/api/monitor/snapshot', methods=['GET', 'OPTIONS'])
    def get_snapshot():
        origin = request.headers.get('Origin', '')
        if origin not in allowed_monitor_origins():
            return jsonify({'code': 403, 'msg': '监控页面来源未授权'}), 403
        if request.method == 'OPTIONS':
            return '', 204
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({'code': 401, 'msg': '请登录节点'}), 401
        try:
            grant = signer().loads(auth[7:], max_age=MONITOR_TTL)
            user = user_lookup(grant.get('user'))
            if (grant.get('scope') != 'monitor:read' or grant.get('origin') != origin or not user
                    or not hmac.compare_digest(grant.get('auth_version', ''),
                                               hashlib.sha256(user.password_hash.encode()).hexdigest())):
                raise ValueError('Invalid monitor grant')
        except Exception:
            return jsonify({'code': 401, 'msg': '节点授权已过期，请重新登录'}), 401
        try:
            return jsonify(snapshot())
        except Exception:
            return jsonify({'code': 503, 'msg': '节点数据暂不可用'}), 503

    app.register_blueprint(bp)
