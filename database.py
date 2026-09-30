from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class Config(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(500))


class Keyword(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20))
    content = db.Column(db.String(100), nullable=False)
    enabled = db.Column(db.Boolean, default=True)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))
    filepath = db.Column(db.String(500))
    status = db.Column(db.String(20))
    progress = db.Column(db.Integer, default=0)
    log = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.now)
    finished_at = db.Column(db.DateTime, nullable=True)

    retry_count = db.Column(db.Integer, default=0)
    overrides = db.Column(db.Text, nullable=True)
    upload_speed = db.Column(db.String(20), default="")
    upload_eta = db.Column(db.String(20), default="")


# 🔥 修改：User 现在是数据库模型，不再是简单的类
class User(UserMixin, db.Model):
    id = db.Column(db.String(50), primary_key=True)  # 用户名作为主键
    password_hash = db.Column(db.String(255))  # 存储加密后的哈希