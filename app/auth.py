from functools import wraps

import click
from flask import Blueprint, g, jsonify, redirect, render_template, request, session, url_for

from . import models

bp = Blueprint('auth', __name__)


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get('user_id')
    g.user = models.get_user_by_id(user_id) if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for('auth.login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for('auth.login', next=request.path))
        if g.user['role'] != 'admin':
            return render_template('error.html', message='Admins only.'), 403
        return view(*args, **kwargs)
    return wrapped


def api_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return jsonify({'error': 'Not logged in'}), 401
        return view(*args, **kwargs)
    return wrapped


def api_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return jsonify({'error': 'Not logged in'}), 401
        if g.user['role'] != 'admin':
            return jsonify({'error': 'Admins only'}), 403
        return view(*args, **kwargs)
    return wrapped


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = models.verify_login(username, password)
        if user is None:
            return render_template('login.html', error='Invalid username or password.')
        if not user['is_active']:
            return render_template('login.html', error='This account has been deactivated.')
        session.clear()
        session['user_id'] = user['id']
        next_path = request.args.get('next') or request.form.get('next')
        if next_path and next_path.startswith('/'):
            return redirect(next_path)
        return redirect(url_for('index'))
    return render_template('login.html', next=request.args.get('next', ''))


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/account/password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        user = models.verify_login(g.user['username'], current_password)
        if user is None:
            return render_template('change_password.html', error='Current password is incorrect.')
        if len(new_password) < 6:
            return render_template('change_password.html', error='New password must be at least 6 characters.')
        models.set_user_password(g.user['id'], new_password)
        return render_template('change_password.html', success='Password updated.')
    return render_template('change_password.html')


def register_cli(app):
    @app.cli.command('create-admin')
    @click.argument('username')
    @click.argument('password')
    def create_admin(username, password):
        """Create an admin user: flask --app app create-admin <username> <password>"""
        existing = models.get_user_by_username(username)
        if existing:
            click.echo(f'User "{username}" already exists.')
            return
        models.create_user(username, password, role='admin', full_name='Admin')
        click.echo(f'Admin user "{username}" created.')
