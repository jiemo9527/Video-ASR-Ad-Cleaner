#!/usr/bin/env python3
import argparse
import secrets

from werkzeug.security import generate_password_hash

from app import app
from database import db, User


parser = argparse.ArgumentParser(description='Reset a Scanner Dashboard password.')
parser.add_argument('--username', help='Dashboard username to reset; defaults to the first account.')
args = parser.parse_args()

with app.app_context():
    user = db.session.get(User, args.username) if args.username else User.query.order_by(User.id).first()
    if not user:
        if args.username:
            raise SystemExit(f'Dashboard user not found: {args.username}')
        raise SystemExit('No Dashboard user exists. Start Scanner once before resetting a password.')

    password = secrets.token_urlsafe(18)
    user.password_hash = generate_password_hash(password)
    db.session.commit()

print(f'username={user.id}')
print(f'password={password}')
