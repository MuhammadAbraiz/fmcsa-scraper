import csv
import io

from flask import Blueprint, Response, g, jsonify, render_template, request

from . import models, scraper
from .auth import api_login_required, login_required

bp = Blueprint('agent', __name__)

_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def _csv_safe(value):
    text = '' if value is None else str(value)
    if text and text[0] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def _resolve_job_filter(job_id_param, persist_for_agent_id=None):
    """job_id_param: the raw ?job_id= value - a job_uuid, the sentinel 'all'
    (explicitly no folder scope), or missing/empty (use the agent's stored
    preference). Returns a job_row_id (int) or None (no scope).

    When persist_for_agent_id is given, an *explicit* choice ('all' or a real
    job_uuid) is saved as that agent's new active folder, so picking a
    folder once means it's still selected on their next visit - a still-
    unset param leaves the stored preference untouched rather than clearing it.
    """
    if job_id_param == 'all':
        if persist_for_agent_id is not None:
            models.set_active_folder(persist_for_agent_id, None)
        return None
    if job_id_param:
        job = models.get_search_job(job_id_param)
        job_row_id = job['id'] if job else None
        if persist_for_agent_id is not None:
            models.set_active_folder(persist_for_agent_id, job_row_id)
        return job_row_id
    return g.user.get('active_folder_id')


@bp.route('/portal')
@login_required
def portal():
    folders = models.list_search_jobs(agent_id=g.user['id'], limit=20)
    return render_template(
        'agent_portal.html', recent_jobs=folders, folders=folders,
        active_folder_id=g.user.get('active_folder_id'), outcomes=models.CALL_OUTCOMES,
    )


@bp.route('/search', methods=['POST'])
@api_login_required
def start_search():
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Folder name is required.'}), 400
    try:
        start_mc = int(request.form.get('start_mc', ''))
        end_mc = int(request.form.get('end_mc', ''))
    except (TypeError, ValueError):
        return jsonify({'error': 'Start and end MC numbers must be integers.'}), 400

    if end_mc < start_mc:
        return jsonify({'error': 'End MC number must be greater than or equal to start MC number.'}), 400

    job_id = scraper.start_scrape_job(start_mc, end_mc, g.user['id'], name=name)
    return jsonify({'job_id': job_id})


@bp.route('/account/active-folder', methods=['POST'])
@api_login_required
def update_active_folder():
    job_id_param = request.form.get('job_id', '')
    job_row_id = _resolve_job_filter(job_id_param, persist_for_agent_id=g.user['id'])
    return jsonify({'ok': True, 'active_folder_id': job_row_id})


@bp.route('/search/<job_id>/status')
@api_login_required
def search_status(job_id):
    data = scraper.read_job(job_id)
    if data is None:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(data)


@bp.route('/search/<job_id>/leads')
@api_login_required
def search_leads(job_id):
    job_row = models.get_search_job(job_id)
    if job_row is None:
        return jsonify({'error': 'Job not found'}), 404
    after_id = request.args.get('after_id', 0, type=int)
    leads = models.list_new_leads_for_job(job_row['id'], after_id)
    return jsonify(leads)


@bp.route('/leads')
@login_required
def leads_page():
    active_folder_id = g.user.get('active_folder_id')
    active_job = models.get_search_job_by_id(active_folder_id) if active_folder_id else None
    return render_template(
        'leads.html', equipment_filters=models.EQUIPMENT_FILTERS, outcomes=models.CALL_OUTCOMES,
        folders=models.list_search_jobs(agent_id=g.user['id'], limit=50),
        active_job=active_job, active_filters=models.get_active_filters(g.user['id']),
    )


PAGE_SIZE = 500


@bp.route('/api/leads')
@api_login_required
def api_leads_list():
    q = request.args.get('q') or None
    equipment = request.args.get('equipment') or None
    mc_min = request.args.get('mc_min', type=int)
    mc_max = request.args.get('mc_max', type=int)
    page = max(1, request.args.get('page', 1, type=int))
    offset = (page - 1) * PAGE_SIZE
    job_row_id = _resolve_job_filter(request.args.get('job_id', ''), persist_for_agent_id=g.user['id'])
    models.set_active_filters(g.user['id'], {'q': q, 'equipment': equipment, 'mc_min': mc_min, 'mc_max': mc_max})

    filters = dict(q=q, equipment=equipment, mc_min=mc_min, mc_max=mc_max, job_row_id=job_row_id)
    leads = models.list_leads(**filters, limit=PAGE_SIZE, offset=offset)
    total = models.count_leads(**filters)
    return jsonify({'leads': leads, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@bp.route('/api/leads/<int:lead_id>')
@api_login_required
def api_lead_detail(lead_id):
    lead = models.get_lead(lead_id)
    if lead is None:
        return jsonify({'error': 'Lead not found'}), 404
    lead['calls'] = models.get_call_logs_for_lead(lead_id)
    return jsonify(lead)


@bp.route('/queue')
@login_required
def call_queue():
    job_row_id = _resolve_job_filter(request.args.get('job_id', ''), persist_for_agent_id=g.user['id'])
    job = models.get_search_job_by_id(job_row_id) if job_row_id else None
    return render_template(
        'queue.html', outcomes=models.CALL_OUTCOMES, equipment_filters=models.EQUIPMENT_FILTERS, job=job,
        folders=models.list_search_jobs(agent_id=g.user['id'], limit=50),
        active_filters=models.get_active_filters(g.user['id']),
    )


@bp.route('/queue/leads')
@api_login_required
def queue_leads():
    q = request.args.get('q') or None
    equipment = request.args.get('equipment') or None
    mc_min = request.args.get('mc_min', type=int)
    mc_max = request.args.get('mc_max', type=int)
    models.set_active_filters(g.user['id'], {'q': q, 'equipment': equipment, 'mc_min': mc_min, 'mc_max': mc_max})

    job_id_param = request.args.get('job_id', '')
    if job_id_param and job_id_param != 'all' and models.get_search_job(job_id_param) is None:
        return jsonify({'error': 'Job not found'}), 404
    job_row_id = _resolve_job_filter(job_id_param, persist_for_agent_id=g.user['id'])

    filters = dict(q=q, equipment=equipment, mc_min=mc_min, mc_max=mc_max, job_row_id=job_row_id)
    return jsonify({
        'leads': models.list_uncalled_leads(**filters),
        'total_remaining': models.count_uncalled_leads(**filters),
    })


@bp.route('/leads/<int:lead_id>')
@login_required
def lead_detail(lead_id):
    lead = models.get_lead(lead_id)
    if lead is None:
        return render_template('error.html', message='Lead not found.'), 404
    calls = models.get_call_logs_for_lead(lead_id)
    return render_template('lead_detail.html', lead=lead, calls=calls, outcomes=models.CALL_OUTCOMES)


@bp.route('/leads/export.csv')
@login_required
def export_csv():
    leads = models.list_all_leads_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    columns = [
        'legal_name', 'usdot', 'mc_number', 'mc_mx_ff_numbers', 'entity_type', 'address',
        'phone', 'email', 'power_units', 'drivers', 'mcs_150_form_date', 'mcs_150_mileage',
        'mcs_150_mileage_year', 'out_of_service_date', 'operating_status',
        'operation_classification', 'carrier_operation', 'cargo_carried', 'likely_equipment',
    ]
    writer.writerow(columns)
    for lead in leads:
        writer.writerow([_csv_safe(lead.get(c, '')) for c in columns])
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=leads_export.csv'},
    )


@bp.route('/leads/<int:lead_id>/call', methods=['POST'])
@api_login_required
def log_call(lead_id):
    lead = models.get_lead(lead_id)
    if lead is None:
        return jsonify({'error': 'Lead not found'}), 404
    call_id = models.create_call_log(lead_id, g.user['id'])
    return jsonify({'call_id': call_id})


@bp.route('/calls/<int:call_id>/outcome', methods=['POST'])
@api_login_required
def set_call_outcome(call_id):
    outcome = request.form.get('outcome', '')
    note = request.form.get('note', '')
    if outcome not in models.CALL_OUTCOMES:
        return jsonify({'error': 'Invalid outcome'}), 400
    ok = models.update_call_outcome(call_id, outcome, note, g.user)
    if not ok:
        return jsonify({'error': 'Call not found or not permitted'}), 403
    return jsonify({'ok': True})
