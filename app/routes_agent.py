import csv
import io

from flask import Blueprint, Response, g, jsonify, render_template, request

from . import models, scraper
from .auth import api_login_required, login_required

bp = Blueprint('agent', __name__)


@bp.route('/portal')
@login_required
def portal():
    recent_jobs = models.list_search_jobs(agent_id=g.user['id'], limit=8)
    return render_template('agent_portal.html', recent_jobs=recent_jobs, outcomes=models.CALL_OUTCOMES)


@bp.route('/search', methods=['POST'])
@api_login_required
def start_search():
    try:
        start_mc = int(request.form.get('start_mc', ''))
        end_mc = int(request.form.get('end_mc', ''))
    except (TypeError, ValueError):
        return jsonify({'error': 'Start and end MC numbers must be integers.'}), 400

    if end_mc < start_mc:
        return jsonify({'error': 'End MC number must be greater than or equal to start MC number.'}), 400

    job_id = scraper.start_scrape_job(start_mc, end_mc, g.user['id'])
    return jsonify({'job_id': job_id})


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
    return render_template('leads.html', equipment_filters=models.EQUIPMENT_FILTERS, outcomes=models.CALL_OUTCOMES)


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

    leads = models.list_leads(q=q, equipment=equipment, mc_min=mc_min, mc_max=mc_max, limit=PAGE_SIZE, offset=offset)
    total = models.count_leads(q=q, equipment=equipment, mc_min=mc_min, mc_max=mc_max)
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
    job = None
    job_id = request.args.get('job_id')
    if job_id:
        job = models.get_search_job(job_id)
    return render_template('queue.html', outcomes=models.CALL_OUTCOMES,
                            equipment_filters=models.EQUIPMENT_FILTERS, job=job)


@bp.route('/queue/leads')
@api_login_required
def queue_leads():
    q = request.args.get('q') or None
    equipment = request.args.get('equipment') or None
    mc_min = request.args.get('mc_min', type=int)
    mc_max = request.args.get('mc_max', type=int)
    job_row_id = None
    job_id = request.args.get('job_id')
    if job_id:
        job = models.get_search_job(job_id)
        if job is None:
            return jsonify({'error': 'Job not found'}), 404
        job_row_id = job['id']

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
        writer.writerow([lead.get(c, '') for c in columns])
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
