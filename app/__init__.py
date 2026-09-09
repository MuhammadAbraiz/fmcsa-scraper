import os

from dotenv import load_dotenv
from flask import Flask, g, redirect, url_for

load_dotenv()

app = Flask(__name__)

# When deployed behind a reverse proxy that mounts this app under a sub-path
# and strips that prefix before forwarding (e.g. `tailscale serve --set-path`),
# url_for()/redirects need to know the prefix to generate links the proxy can
# route back correctly. Set URL_PREFIX (e.g. "/fmcsa") only on deployments
# that need it; unset (the common case) leaves the app mounted at root.
url_prefix = os.environ.get('URL_PREFIX', '').rstrip('/')
if url_prefix:
    _wsgi_app = app.wsgi_app

    def _prefix_script_name(environ, start_response):
        environ['SCRIPT_NAME'] = url_prefix
        return _wsgi_app(environ, start_response)

    app.wsgi_app = _prefix_script_name


@app.context_processor
def inject_url_prefix():
    # Exposed as window.URL_PREFIX (see base.html) so client-side fetch()
    # calls can route through the same proxy prefix as server-rendered links.
    return {'url_prefix': url_prefix}


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
