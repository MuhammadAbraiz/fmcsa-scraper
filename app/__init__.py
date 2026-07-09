import os

from dotenv import load_dotenv
from flask import Flask, g, redirect, url_for

load_dotenv()

app = Flask(__name__)

secret_key = os.environ.get('FLASK_SECRET_KEY')
if not secret_key:
    raise RuntimeError('FLASK_SECRET_KEY environment variable not set. Please set it in your environment.')
app.secret_key = secret_key

from . import db  # noqa: E402
db.init_db()

from . import auth  # noqa: E402
from . import routes_agent  # noqa: E402
from . import routes_admin  # noqa: E402

app.register_blueprint(auth.bp)
app.register_blueprint(routes_agent.bp)
app.register_blueprint(routes_admin.bp)
auth.register_cli(app)


@app.route('/')
def index():
    if g.user is None:
        return redirect(url_for('auth.login'))
    if g.user['role'] == 'admin':
        return redirect(url_for('admin.dashboard'))
    return redirect(url_for('agent.portal'))
