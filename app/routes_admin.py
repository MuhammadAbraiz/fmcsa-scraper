import os
import re
from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, send_from_directory, url_for

from . import models, scraper
from .auth import admin_required, api_admin_required

bp = Blueprint('admin', __name__, url_prefix='/admin')

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _valid_date_param():
    """Reads ?date=YYYY-MM-DD from the query string; returns None if absent/malformed."""
    value = request.args.get('date')
    return value if value and DATE_RE.match(value) else None


@bp.route('')
@admin_required
def dashboard():
    summary = models.dashboard_summary()
    recent_jobs = models.list_search_jobs(limit=15)
    recent_calls = models.list_call_logs(limit=15)
    return render_template('admin_dashboard.html', summary=summary, recent_jobs=recent_jobs, recent_calls=recent_calls)


@bp.route('/agents', methods=['GET', 'POST'])
@admin_required
def agents():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        full_name = request.form.get('full_name', '').strip()
        error = None
        if not username or not password:
            error = 'Username and password are required.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        elif models.get_user_by_username(username):
            error = 'That username is already taken.'
        if error:
            return render_template('admin_agents.html', agents=models.list_agents(), error=error)
        models.create_user(username, password, role='agent', full_name=full_name)
        return redirect(url_for('admin.agents'))
    return render_template('admin_agents.html', agents=models.list_agents())


@bp.route('/agents/<int:user_id>')
@admin_required
def agent_detail(user_id):
    agent = models.get_user_by_id(user_id)
    if agent is None or agent['role'] != 'agent':
        return render_template('error.html', message='Agent not found.'), 404
    jobs = models.list_search_jobs(agent_id=user_id)
    calls = models.list_call_logs(agent_id=user_id)
    call_stats = {p: models.call_outcome_breakdown(period=p, agent_id=user_id) for p in models.CALL_STAT_PERIODS}
    return render_template('admin_agent_detail.html', agent=agent, jobs=jobs, calls=calls, call_stats=call_stats)


@bp.route('/agents/<int:user_id>/deactivate', methods=['POST'])
@admin_required
def deactivate_agent(user_id):
    models.set_user_active(user_id, False)
    return redirect(url_for('admin.agents'))


@bp.route('/agents/<int:user_id>/activate', methods=['POST'])
@admin_required
def activate_agent(user_id):
    models.set_user_active(user_id, True)
    return redirect(url_for('admin.agents'))


@bp.route('/agents/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def reset_agent_password(user_id):
    new_password = request.form.get('new_password', '')
    if len(new_password) < 6:
        agent = models.get_user_by_id(user_id)
        if agent is None or agent['role'] != 'agent':
            return render_template('error.html', message='Agent not found.'), 404
        jobs = models.list_search_jobs(agent_id=user_id)
        calls = models.list_call_logs(agent_id=user_id)
        call_stats = {p: models.call_outcome_breakdown(period=p, agent_id=user_id) for p in models.CALL_STAT_PERIODS}
        return render_template(
            'admin_agent_detail.html', agent=agent, jobs=jobs, calls=calls, call_stats=call_stats,
            password_error='Password must be at least 6 characters.',
        )
    models.set_user_password(user_id, new_password)
    return redirect(url_for('admin.agent_detail', user_id=user_id))


PAGE_SIZE = 500


@bp.route('/call-stats')
@admin_required
def call_stats():
    return render_template(
        'admin_call_stats.html', outcomes=models.CALL_OUTCOMES,
        current_shift_date=models.current_shift_date(),
    )


@bp.route('/api/call-stats')
@api_admin_required
def api_call_stats():
    custom_date = _valid_date_param()
    period = request.args.get('period', 'today')
    if not custom_date and period not in models.CALL_STAT_PERIODS:
        return jsonify({'error': 'Invalid period'}), 400
    return jsonify({
        'team_total': models.call_outcome_breakdown(period=period, custom_date=custom_date),
        'agents': models.agent_call_stats(period=period, custom_date=custom_date),
    })


@bp.route('/calls')
@admin_required
def calls():
    agent_id = request.args.get('agent_id', type=int)
    shift_date = _valid_date_param()
    page = max(1, request.args.get('page', 1, type=int))
    offset = (page - 1) * PAGE_SIZE

    call_logs = models.list_call_logs(agent_id=agent_id, limit=PAGE_SIZE, offset=offset, shift_date=shift_date)
    total = models.count_call_logs(agent_id=agent_id, shift_date=shift_date)
    return render_template(
        'admin_calls.html', calls=call_logs, agents=models.list_agents(), selected_agent_id=agent_id,
        page=page, page_size=PAGE_SIZE, total=total, selected_date=shift_date,
        current_shift_date=models.current_shift_date(),
    )


# --- legacy routes for CSVs generated before the DB-backed lead pool existed ---

@bp.route('/files')
@admin_required
def list_files():
    files = []
    for filename in os.listdir(scraper.OUTPUT_DIR):
        if not (filename.startswith('output_') and filename.endswith('.csv')):
            continue
        path = os.path.join(scraper.OUTPUT_DIR, filename)
        files.append({
            'filename': filename,
            'size_bytes': os.path.getsize(path),
            'modified_at': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S'),
            'download_url': f'/admin/download/{filename}',
        })
    files.sort(key=lambda f: f['modified_at'], reverse=True)
    return render_template('admin_legacy_files.html', files=files)


@bp.route('/download/<path:filename>')
@admin_required
def download_file(filename):
    safe_filename = os.path.basename(filename)
    if not safe_filename.startswith('output_') or not safe_filename.endswith('.csv'):
        return "Invalid file", 400
    if not os.path.exists(os.path.join(scraper.OUTPUT_DIR, safe_filename)):
        return "File not found", 404
    return send_from_directory(scraper.OUTPUT_DIR, safe_filename, as_attachment=True)
